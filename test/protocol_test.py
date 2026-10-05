#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 协议一致性测试
=====================
服务端和客户端各自实现了一份包头编解码（为了都能单目录部署）。
两份实现如果有一点不一致，就会出现"TCP 好好的、UDP 全丢包"这种难查的问题
—— 之前真的踩过一次（fam 字节被写了两次，MAC 和地址全错位）。

这个测试就是盯着这件事：**两份实现对同一份数据必须产出完全相同的字节**，
并且能互相解析，密钥不对时必须拒绝。

    python test/protocol_test.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

import mclink_client as C          # noqa: E402
import mclink_server as S          # noqa: E402

results = []


def ck(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def main() -> int:
    TOK = "s3cret-token-测试"
    OTHER = "wrong-token"

    print("\n== 1. UDP 数据包 ==")
    cases = [
        (1, ("1.2.3.4", 25565), b"hello", 4),
        (65535, ("10.0.0.1", 1), b"", 4),
        (7, ("2001:db8::1", 19132), b"bedrock" * 20, 6),
        (42, ("127.0.0.1", 65535), bytes(range(256)), 4),
    ]
    all_same = True
    for tid, addr, payload, fam in cases:
        pkt_s = S.pack_udp_data(tid, addr, payload, TOK)
        pkt_c = C.pack_udp_data(tid, addr, payload, TOK)
        same = pkt_s == pkt_c
        rt_s = S.parse_udp_data(pkt_c, TOK) == (tid, addr, payload)
        rt_c = C.parse_udp_data(pkt_s, TOK) == (tid, addr, payload)
        bad_s = S.parse_udp_data(pkt_c, OTHER) is None
        bad_c = C.parse_udp_data(pkt_s, OTHER) is None
        all_same &= same and rt_s and rt_c and bad_s and bad_c
        print(f"    tid={tid:<6} {addr[0]}:{addr[1]:<6} v{fam}  "
              f"{len(payload):>4}B  字节一致={same} 互解={rt_s and rt_c} "
              f"错密钥拒绝={bad_s and bad_c}")
    ck("客户端/服务端打包字节完全一致，且能互相解析", all_same)

    # 篡改任意一个字节都必须被发现
    pkt = S.pack_udp_data(3, ("1.2.3.4", 80), b"payload", TOK)
    tamper_ok = True
    for i in range(len(pkt)):
        bad = bytearray(pkt)
        bad[i] ^= 0x01
        if S.parse_udp_data(bytes(bad), TOK) == (3, ("1.2.3.4", 80), b"payload"):
            tamper_ok = False
            print(f"    ✗ 篡改第 {i} 字节没被发现")
    ck("篡改任意一个字节都会导致校验失败", tamper_ok)

    # 偷换玩家地址（想把流量劫到别处）
    from_addr = ("1.2.3.4", 80)
    to_addr = ("6.6.6.6", 80)
    p1 = S.pack_udp_data(3, from_addr, b"x", TOK)
    # 手工把地址改成 to_addr（保持长度），MAC 应当对不上
    import socket
    tampered = p1[:14] + socket.inet_aton(to_addr[0]) + p1[18:]
    ck("把玩家地址偷换掉会被 MAC 拦下",
       S.parse_udp_data(tampered, TOK) is None)

    print("\n== 2. UDP 注册包 ==")
    r_s = S.pack_udp_reg(9, TOK, ts=1700000000)
    r_c = C.pack_udp_reg(9, TOK, ts=1700000000)
    ck("注册包字节一致", r_s == r_c, f"{len(r_s)} 字节")
    ck("服务端能验客户端发的注册包", S.parse_udp_reg(r_c, TOK) == (9, 1700000000))
    ck("客户端能验服务端发的注册包", C.parse_udp_reg(r_s, TOK) == (9, 1700000000))
    ck("错密钥的注册包被拒",
       S.parse_udp_reg(C.pack_udp_reg(9, OTHER, ts=1700000000), TOK) is None)
    ck("改掉 tunnel_id 会被拒",
       S.parse_udp_reg(S.pack_udp_reg(10, TOK, ts=1700000000), TOK) != (9, 1700000000))
    ck("注册包里不含明文密钥（密钥不上网）",
       TOK.encode() not in r_s)

    print("\n== 3. PONG 回执 ==")
    po_s = S.pack_udp_pong(5, TOK, ts=1700000000)
    ck("客户端能验服务端的 PONG", C.parse_udp_pong(po_s, TOK) == (5, 1700000000))
    ck("错密钥的 PONG 被拒", C.parse_udp_pong(po_s, OTHER) is None)
    ck("PONG 里不含明文密钥", TOK.encode() not in po_s)

    print("\n== 4. 控制通道分帧 ==")
    import asyncio
    import json
    import struct

    async def framing():
        # 用一条内存管道验证 send_msg / read_msg 两边对称
        reader = asyncio.StreamReader()
        got = []

        class W:
            def write(self, b):
                got.append(b)

            async def drain(self):
                pass

        msg = {"t": "hello", "token": TOK, "名字": "中文也要能过"}
        await C.send_msg(W(), msg)
        raw = b"".join(got)
        (n,) = struct.unpack("!I", raw[:4])
        ok = (n == len(raw) - 4 and json.loads(raw[4:].decode("utf-8")) == msg)
        # 服务端的 read_msg 能读客户端写的帧
        reader.feed_data(raw)
        back = await S.read_msg(reader)
        return ok and back == msg

    ck("send_msg / read_msg 跨实现对称（含中文）", asyncio.run(framing()))

    print("\n== 5. 版本号一致 ==")
    ck("两端 VERSION 相同", C.VERSION == S.VERSION, f"{C.VERSION} / {S.VERSION}")
    ck("两端 UDP 包头版本相同", C.UDP_VER == S.UDP_VER and C.MAGIC == S.MAGIC)
    ck("两端 MAC 长度相同", C.UDP_MAC_LEN == S.UDP_MAC_LEN)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

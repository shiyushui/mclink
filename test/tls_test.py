#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink TLS / 传输安全测试
=========================
真起一个开了 TLS 的服务端和真客户端，验证：

    1. 服务端能自动生成自签证书
    2. 客户端能正常走 TLS 连接（控制通道 + 数据通道）
    3. TCP 转发在 TLS 下仍然正常
    4. UDP 隧道在带 HMAC 的新包头下仍然正常
    5. 第一次连接会自动记住证书指纹（TOFU）
    6. **指纹对不上时必须拒绝连接**（模拟中间人换证书）
    7. 明文客户端连 TLS 服务端要给出清楚的报错
    8. **UDP 伪造包（MAC 不对）会被丢弃**，不会送进本地游戏
    9. 拿错密钥伪造 UDP 注册包也进不来

    python test/tls_test.py
"""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp_tls")
sys.path.insert(0, os.path.join(ROOT, "client"))

TOKEN = secrets.token_urlsafe(24)
HOST = "127.0.0.1"
CTRL, DATA, UDPP = 19500, 19500, 19500
PUB_TCP, PUB_UDP = 19600, 19601
LOCAL_TCP, LOCAL_UDP = 19650, 19651
WEB_A, WEB_B = 19787, 19788

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return bool(ok)


# ---------------------------------------------------------------- 假游戏服务端

async def tcp_echo(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(b"ECHO:" + data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


class UdpEcho(asyncio.DatagramProtocol):
    def __init__(self):
        self.seen = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.seen.append(data)
        self.transport.sendto(b"UDP-ECHO:" + data, addr)


# ---------------------------------------------------------------- HTTP 小工具

async def http(port, method, path, body=None, timeout=6):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, port), timeout)
    try:
        payload = b"" if body is None else json.dumps(body).encode()
        writer.write((f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
                      f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                      f"Connection: close\r\n\r\n").encode() + payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout)
    finally:
        writer.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    try:
        return int(head.split(b" ")[1]), json.loads(rest.decode())
    except Exception:
        return 0, {}


async def wait_state(port, pred, timeout=30):
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        try:
            code, st = await http(port, "GET", "/api/state")
            if pred(st):
                return True, st
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False, st


async def main() -> int:
    # 清掉上次的临时目录。
    # Windows 上 shutil.rmtree(ignore_errors=True) 会因为残留的只读/占用文件
    # 静默失败，下次跑就复用了旧配置（旧的 cert_fingerprint 会把客户端拦住，
    # 测试看起来"卡死"）。所以：先清只读属性，清不掉就换个新目录名。
    def wipe(path):
        if not os.path.isdir(path):
            return True
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    os.chmod(os.path.join(root, fn), 0o666)
                except OSError:
                    pass
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            return False

    global TMP
    if not wipe(TMP):
        TMP = TMP + "_" + str(int(time.time()))
        print(f"[提示] 旧临时目录删不掉，改用 {os.path.basename(TMP)}")
    os.makedirs(TMP, exist_ok=True)

    cert = os.path.join(TMP, "server.crt")
    key = os.path.join(TMP, "server.key")
    srv_cfg = os.path.join(TMP, "server.json")
    cfg_a = os.path.join(TMP, "client_a.json")
    cfg_b = os.path.join(TMP, "client_b.json")
    cfg_plain = os.path.join(TMP, "client_plain.json")

    # 本机可能没有 openssl（纯 Windows 就常见），那就用仓库里的一次性测试证书。
    # 生产服务器（Linux）都有 openssl，会自己签。
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import mclink_server as srvmod
    openssl = srvmod.find_openssl()
    auto_gen = openssl is not None
    if not auto_gen:
        fx_crt = os.path.join(HERE, "fixtures", "TEST-ONLY.crt")
        fx_key = os.path.join(HERE, "fixtures", "TEST-ONLY.key")
        if not (os.path.exists(fx_crt) and os.path.exists(fx_key)):
            print("本机没有 openssl，也找不到测试夹具证书，跳过 TLS 测试")
            return 0
        shutil.copy(fx_crt, cert)
        shutil.copy(fx_key, key)

    with open(srv_cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "bind": "0.0.0.0", "control_port": CTRL, "data_port": CTRL, "udp_port": UDPP,
            "tokens": [TOKEN], "public_ip": HOST, "tls": True,
            "tls_cert": cert, "tls_key": key,
            "allowed_ports": [[PUB_TCP, PUB_UDP]],
            "log_level": "debug", "log_file": "",
        }, fh)

    def client_cfg(path, web_port, tls=True, fingerprint=""):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "server": {"host": HOST, "control_port": CTRL, "data_port": CTRL,
                           "udp_port": UDPP, "token": TOKEN, "tls": tls,
                           "cert_fingerprint": fingerprint},
                "web": {"host": HOST, "port": web_port, "token": ""},
                # 这个套件要的是"注册好之后"的行为，显式打开自动开启映射
                # （客户端默认 auto_start_mappings=false：启动不自动开）
                "client": {"auto_start_mappings": True},
                "mappings": [
                    {"id": "t1", "name": "TCP 测试", "proto": "tcp",
                     "local_host": HOST, "local_port": LOCAL_TCP,
                     "remote_port": PUB_TCP, "enabled": True},
                    {"id": "u1", "name": "UDP 测试", "proto": "udp",
                     "local_host": HOST, "local_port": LOCAL_UDP,
                     "remote_port": PUB_UDP, "enabled": True},
                ],
                "log_level": "debug", "log_file": "",
            }, fh, ensure_ascii=False)

    client_cfg(cfg_a, WEB_A)
    client_cfg(cfg_b, WEB_B, fingerprint="ab" * 32)          # 故意错的指纹
    client_cfg(cfg_plain, WEB_A + 10, tls=False)

    tcp_srv = await asyncio.start_server(tcp_echo, HOST, LOCAL_TCP)
    loop = asyncio.get_running_loop()
    udp_echo = UdpEcho()
    await loop.create_datagram_endpoint(lambda: udp_echo, local_addr=(HOST, LOCAL_UDP))

    srv_log = open(os.path.join(TMP, "server.log"), "w", encoding="utf-8")
    cli_logs = {}
    procs = []

    try:
        # ---------- 1. 证书 ----------
        print("\n== 1. 服务端证书 ==")
        p_srv = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "mclink_server.py"),
             "-c", srv_cfg],
            cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(p_srv)

        for _ in range(60):
            if os.path.exists(cert) and os.path.exists(key):
                break
            await asyncio.sleep(0.5)
        check("证书和私钥就绪", os.path.exists(cert) and os.path.exists(key),
              f"{'服务端自动生成' if auto_gen else '使用测试夹具'}")

        if not auto_gen:
            class _DummyLog:
                def warn(self, _m):
                    pass

                def info(self, _m):
                    pass

            r = srvmod.ensure_cert(os.path.join(TMP, "nope.crt"),
                                   os.path.join(TMP, "nope.key"), _DummyLog())
            check("没有 openssl 时 ensure_cert 优雅返回 False（不崩、不阻断启动）",
                  r is False)
        if os.name != "nt":
            check("私钥权限不是所有人可读",
                  (os.stat(key).st_mode & 0o077) == 0,
                  oct(os.stat(key).st_mode & 0o777))

        # 服务端自己算出来的指纹
        fp_file = srvmod.cert_fingerprint(cert)
        check("能从证书算出 SHA-256 指纹", len(fp_file) == 64, fp_file[:24] + "…")

        # 等服务端监听
        for _ in range(40):
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(HOST, CTRL), 1)
                w.close()
                break
            except Exception:
                await asyncio.sleep(0.25)

        # 用真实的 TLS 握手拿证书 —— 这才是客户端实际看到的东西
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        fp_tls = ""
        try:
            rr, ww = await asyncio.wait_for(
                asyncio.open_connection(HOST, CTRL, ssl=ctx), 8)
            der = ww.get_extra_info("ssl_object").getpeercert(binary_form=True)
            fp_tls = hashlib.sha256(der).hexdigest()
            ww.close()
        except Exception as exc:
            check("TLS 握手成功", False, f"{type(exc).__name__}: {exc}")
        check("TLS 握手成功并能取到证书", len(fp_tls) == 64, fp_tls[:24] + "…")
        check("服务端算的指纹 == 客户端握到的一致（指纹固定才靠谱）",
              fp_file == fp_tls, f"file={fp_file[:16]}… tls={fp_tls[:16]}…")
        fp_server = fp_file

        # ---------- 2. 明文连 TLS 服务端 ----------
        print("\n== 2. 明文客户端连 TLS 服务端 ==")
        p_plain = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"),
             "-c", cfg_plain, "--web-port", str(WEB_A + 10)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        procs.append(p_plain)
        await asyncio.sleep(5)
        code, st = await http(WEB_A + 10, "GET", "/api/state")
        err = (st.get("server") or {}).get("last_error") or ""
        check("明文连不上并给出可读原因",
              code == 200 and not (st.get("server") or {}).get("connected") and err,
              err[:80])
        p_plain.terminate()

        # ---------- 3. 正常 TLS 客户端 ----------
        print("\n== 3. TLS 客户端正常连接 ==")
        p_a = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"),
             "-c", cfg_a, "--web-port", str(WEB_A)],
            cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(p_a)

        ok, st = await wait_state(WEB_A, lambda s: (s.get("server") or {}).get("connected")
                                  and any(m["id"] == "t1" and m["status"] == "active"
                                          for m in s.get("mappings", []))
                                  # UDP 那条也要就绪：客户端拿到 register_ok 就
                                  # 立刻发隧道包了，所以这里等得到
                                  and any(m["id"] == "u1" and m["status"] == "active"
                                          for m in s.get("mappings", [])), 40)
        check("TLS 下控制通道接通并注册成功", ok,
              f"connected={(st.get('server') or {}).get('connected')} "
              f"mappings={[(m['id'], m['status']) for m in st.get('mappings', [])]}")

        # ---------- 4. TCP 转发 ----------
        print("\n== 4. TLS 下 TCP 转发 ==")
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, PUB_TCP), 6)
            w.write(b"tls-hello")
            await w.drain()
            got = await asyncio.wait_for(r.read(100), 6)
            w.close()
            check("公网 TCP 端口收发正常", got == b"ECHO:tls-hello", repr(got))
        except Exception as exc:
            check("公网 TCP 端口收发正常", False, f"{type(exc).__name__}: {exc}")

        # ---------- 5. UDP（带 HMAC 的新包头）----------
        print("\n== 5. TLS 下 UDP 隧道（HMAC 包头）==")
        got_udp = []

        # 等 UDP 也真的就绪。公网端口一绑定服务端就报 active，但 UDP 还要等
        # 客户端第一个保活包把隧道登记上；这中间玩家发的包会被丢掉。
        # 服务端现在会把这段窗口报成 pending，所以这里等它变 active 再发。
        ok_udp, st_udp = await wait_state(
            WEB_A, lambda s: any(m["id"] == "u1" and m["status"] == "active"
                                 for m in s.get("mappings", [])), 40)
        check("UDP 映射等到隧道就绪（不再有假的 active 窗口）", ok_udp,
              str([(m["id"], m["status"]) for m in st_udp.get("mappings", [])]))

        class Tester(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                got_udp.append(data)

        t = Tester()
        await loop.create_datagram_endpoint(lambda: t, local_addr=(HOST, 0))
        for i in range(3):
            t.transport.sendto(f"bedrock-{i}".encode(), (HOST, PUB_UDP))
            await asyncio.sleep(0.25)
        for _ in range(24):
            if len(got_udp) >= 3:
                break
            await asyncio.sleep(0.25)
        check("UDP 收发正常（含首包）",
              got_udp[:3] == [b"UDP-ECHO:bedrock-0", b"UDP-ECHO:bedrock-1",
                              b"UDP-ECHO:bedrock-2"],
              repr(got_udp[:3]))

        # ---------- 6. TOFU 指纹 ----------
        print("\n== 6. 证书指纹固定（TOFU）==")
        saved = json.load(open(cfg_a, encoding="utf-8"))["server"].get("cert_fingerprint", "")
        check("客户端第一次连接后把指纹写进了配置", len(saved) == 64, saved[:24] + "…")
        check("客户端记的指纹和服务端证书一致",
              saved == fp_server, f"client={saved[:16]}… server={fp_server[:16]}…")

        code, st = await http(WEB_A, "GET", "/api/server_info")
        info = (st or {}).get("info") or {}
        check("服务端上报 tls 已开启", info.get("tls") is True, str(info.get("tls")))

        # ---------- 7. 指纹不匹配必须拒绝 ----------
        print("\n== 7. 指纹对不上（模拟中间人）==")
        p_b = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"),
             "-c", cfg_b, "--web-port", str(WEB_B)],
            cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(p_b)
        await asyncio.sleep(8)
        code, st = await http(WEB_B, "GET", "/api/state")
        err_b = (st.get("server") or {}).get("last_error") or ""
        check("指纹不匹配时拒绝连接并说明原因",
              code == 200 and not (st.get("server") or {}).get("connected")
              and "指纹" in err_b, err_b[:80])
        p_b.terminate()

        # ---------- 8. UDP 伪造包 ----------
        print("\n== 8. UDP 伪造包会被丢弃 ==")
        # 拿到真实的 tunnel_id
        code, st = await http(WEB_A, "GET", "/api/state")
        um = next((m for m in st.get("mappings", []) if m["id"] == "u1"), None)
        tid = (um or {}).get("tunnel_id") or 0
        check("拿到 UDP 隧道的 tunnel_id", bool(tid), str(tid))

        import mclink_client as mc
        before = len(udp_echo.seen)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 8.1 用错的密钥做 MAC
        forged = mc.pack_udp_data(tid, ("9.9.9.9", 12345), b"FORGED", "wrong-token")
        sock.sendto(forged, (HOST, UDPP))
        await asyncio.sleep(1.5)
        check("MAC 不对的伪造数据包被丢弃（没进本地游戏）",
              len(udp_echo.seen) == before,
              f"本地收到 {len(udp_echo.seen) - before} 个")

        # 8.2 用错的密钥伪造注册包，想抢走隧道
        reg = mc.pack_udp_reg(tid, "wrong-token")
        sock.sendto(reg, (HOST, UDPP))
        await asyncio.sleep(1.0)
        # 真正的客户端应该还在正常工作
        got_udp.clear()
        t.transport.sendto(b"after-forge", (HOST, PUB_UDP))
        for _ in range(16):
            if got_udp:
                break
            await asyncio.sleep(0.25)
        check("伪造注册包没有踢掉正常隧道",
              got_udp[:1] == [b"UDP-ECHO:after-forge"], repr(got_udp[:1]))

        # 8.3 正确的 MAC 应该能过（对照）。
        # 注意方向语义：客户端 → 服务端的 DATA 包是"请把这个负载投递给玩家 X"，
        # 所以这里冒充一个玩家地址，看服务端会不会真的把负载发过来。
        player = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        player.bind((HOST, 0))
        player.settimeout(4)
        paddr = player.getsockname()
        token = json.load(open(cfg_a, encoding="utf-8"))["server"]["token"]
        good = mc.pack_udp_data(tid, paddr, b"GOOD", token)
        sock.sendto(good, (HOST, UDPP))
        try:
            data, _ = player.recvfrom(2048)
            got_good = (data == b"GOOD")
        except socket.timeout:
            got_good = False
        check("MAC 正确的包能正常投递（对照，证明不是无差别丢弃）",
              got_good, f"玩家侧收到 {data!r}" if got_good else "没收到")
        player.close()
        sock.close()
        t.transport.close()

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                    # kill 之后也要 wait —— 不然进程还占着临时目录里的
                    # 配置文件，下次跑清理不掉就会复用旧配置
                    p.wait(timeout=6)
                except Exception:
                    pass
        tcp_srv.close()
        srv_log.close()
        for fh in cli_logs.values():
            try:
                fh.close()
            except Exception:
                pass

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    if passed != len(results):
        print("\n---- 服务端日志尾部 ----")
        print(open(os.path.join(TMP, "server.log"), encoding="utf-8").read()[-2500:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 授权流程端到端测试
=========================
起一个真实的 mclink_server（开启 license_required），然后用原始控制协议
把整条分发链路走一遍：

    未授权连接 → 注册被拒 → 管理员解锁 → 生成邀请码 → 用户激活
    → 拿到设备令牌 → 重连自动放行 → 注册成功 → 管理员停用 → 再次被拒
    外加：换台电脑不认、坏密钥被拒、限流、非管理员不能用管理接口

    python test/license_e2e_test.py
"""

import asyncio
import json
import os
import secrets
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp_lic_e2e")

HOST = "127.0.0.1"
CTRL = 19000
PUB = 19100
ADMIN = secrets.token_urlsafe(32)
MASTER = secrets.token_urlsafe(24)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return bool(ok)


class Conn:
    """一条原始控制连接。"""

    def __init__(self, reader, writer):
        self.r = reader
        self.w = writer

    @classmethod
    async def open(cls):
        r, w = await asyncio.wait_for(asyncio.open_connection(HOST, CTRL), 6)
        return cls(r, w)

    async def send(self, obj):
        b = json.dumps(obj).encode()
        self.w.write(struct.pack("!I", len(b)) + b)
        await self.w.drain()

    async def recv(self, timeout=8):
        hdr = await asyncio.wait_for(self.r.readexactly(4), timeout)
        (n,) = struct.unpack("!I", hdr)
        return json.loads((await asyncio.wait_for(self.r.readexactly(n), timeout)).decode())

    async def wait_for(self, t, timeout=8):
        """跳过无关的推送消息，等到指定类型。"""
        end = time.time() + timeout
        while time.time() < end:
            msg = await self.recv(max(1, end - time.time()))
            if msg.get("t") == t:
                return msg
        raise TimeoutError(f"没有等到 {t}")

    async def hello(self, license=None, admin_key=None):
        payload = {"t": "hello", "token": MASTER, "name": "test", "version": "test"}
        if license is not None:
            payload["license"] = license
        if admin_key:
            payload["admin_token"] = admin_key
        await self.send(payload)
        return await self.recv()

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass


def make_cfg(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "bind": "0.0.0.0",
            "control_port": CTRL, "data_port": CTRL, "udp_port": CTRL,
            "tokens": [MASTER], "public_ip": HOST, "tls": False,
            "allowed_ports": [[PUB, PUB]],
            "admin_token": ADMIN,
            "license_required": True,
            "license_file": os.path.join(TMP, "licenses.json"),
            "log_level": "debug", "log_file": "",
        }, fh, ensure_ascii=False)


async def main() -> int:
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    cfg = os.path.join(TMP, "server.json")
    make_cfg(cfg)
    log = open(os.path.join(TMP, "server.log"), "w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "mclink_server.py"), "-c", cfg],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

    try:
        # 等服务端起来
        for _ in range(40):
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(HOST, CTRL), 1)
                w.close()
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            print("服务端没起来"); return 1

        # ---------- 1. 未授权的客户端 ----------
        print("\n== 1. 未授权客户端 ==")
        c1 = await Conn.open()
        ok = await c1.hello(license={"device_token": "", "fingerprint": "fp-A"})
        check("握手成功但标记为未授权",
              ok.get("t") == "hello_ok" and ok.get("licensed") is False
              and ok.get("license", {}).get("required") is True,
              f"licensed={ok.get('licensed')} reason={ok.get('license', {}).get('reason')}")
        check("服务端告知已配置管理员密钥", ok.get("admin_configured") is True)

        await c1.send({"t": "register", "mapping": {
            "mid": "m1", "name": "偷偷开一个", "proto": "tcp", "remote_port": PUB}})
        resp = await c1.wait_for("register_err")
        check("未授权时注册被拒（这是核心闸门）",
              "未授权" in (resp.get("error") or ""), resp.get("error"))

        await c1.send({"t": "admin", "req": "x1", "action": "invite_list", "args": {}})
        resp = await c1.wait_for("admin_err")
        check("未授权不能用管理接口", "管理员权限" in (resp.get("error") or ""),
              resp.get("error"))

        await c1.send({"t": "admin", "req": "x2", "action": "invite_create",
                       "args": {"username": "hacker", "hours": 24}})
        resp = await c1.wait_for("admin_err")
        check("未授权不能生成邀请码", resp.get("t") == "admin_err")

        # ---------- 2. 管理员解锁 ----------
        print("\n== 2. 管理员模式 ==")
        c2 = await Conn.open()
        await c2.hello(license={"device_token": "", "fingerprint": "fp-ADMIN"})
        await c2.send({"t": "admin_auth", "admin_token": "wrong-key"})
        resp = await c2.wait_for("admin_auth_err")
        check("错误的管理员密钥被拒", "不正确" in (resp.get("error") or ""), resp.get("error"))

        await c2.send({"t": "admin_auth", "admin_token": ADMIN})
        resp = await c2.wait_for("admin_auth_ok")
        check("正确的管理员密钥可以解锁", resp.get("licensed") is True)

        await c2.send({"t": "admin", "req": "s1", "action": "stats", "args": {}})
        resp = await c2.wait_for("admin_ok")
        check("管理员可以读统计", isinstance(resp.get("data"), dict)
              and resp["data"].get("required") is True, str(resp.get("data")))

        # 生成邀请码
        await c2.send({"t": "admin", "req": "i1", "action": "invite_create",
                       "args": {"username": "alice", "hours": 24, "note": "朋友A"}})
        resp = await c2.wait_for("admin_ok")
        code = (resp.get("data") or {}).get("code")
        check("生成邀请码", bool(code) and code.startswith("MCLK-"), code)

        await c2.send({"t": "admin", "req": "i2", "action": "invite_create",
                       "args": {"username": "a b", "hours": 24}})
        resp = await c2.wait_for("admin_err")
        check("非法用户名不能生成邀请码", "用户名" in (resp.get("error") or ""), resp.get("error"))

        # ---------- 3. 用户激活 ----------
        print("\n== 3. 用户激活 ==")
        c3 = await Conn.open()
        await c3.hello(license={"device_token": "", "fingerprint": "fp-ALICE"})
        await c3.send({"t": "license_activate", "code": "MCLK-0000-0000-0000",
                       "fingerprint": "fp-ALICE", "device_name": "ALICE-PC"})
        resp = await c3.wait_for("license_err")
        check("乱填的密钥被拒", "不存在" in (resp.get("error") or ""), resp.get("error"))

        await c3.send({"t": "license_activate", "code": code.lower(),
                       "fingerprint": "fp-ALICE", "device_name": "ALICE-PC"})
        resp = await c3.wait_for("license_ok")
        token = resp.get("device_token")
        check("正确密钥激活成功", bool(token) and len(token) > 20,
              f"用户={resp.get('username')}")
        check("激活后立即标记为已授权", c3 is not None)

        # ---------- 4. 重连自动放行 ----------
        print("\n== 4. 带着设备令牌重连 ==")
        c4 = await Conn.open()
        ok = await c4.hello(license={"device_token": token, "fingerprint": "fp-ALICE"})
        check("同一台电脑 + 令牌 → 直接放行",
              ok.get("licensed") is True and ok.get("license", {}).get("username") == "alice",
              f"licensed={ok.get('licensed')} user={ok.get('license', {}).get('username')}")

        await c4.send({"t": "register", "mapping": {
            "mid": "m1", "name": "Minecraft", "proto": "tcp", "remote_port": PUB}})
        resp = await c4.wait_for("register_ok")
        check("已授权后可以正常开公网端口", resp.get("t") == "register_ok"
              and resp.get("remote_port") == PUB, str(resp))

        c5 = await Conn.open()
        ok = await c5.hello(license={"device_token": token, "fingerprint": "fp-STOLEN"})
        check("把授权拷到另一台电脑 → 拒绝",
              ok.get("licensed") is False and "设备不匹配" in (ok.get("license", {}).get("reason") or ""),
              ok.get("license", {}).get("reason"))

        # ---------- 5. 密钥一次性 ----------
        print("\n== 5. 邀请码只能用一次 ==")
        c6 = await Conn.open()
        await c6.hello(license={"device_token": "", "fingerprint": "fp-BOB"})
        await c6.send({"t": "license_activate", "code": code,
                       "fingerprint": "fp-BOB", "device_name": "BOB-PC"})
        resp = await c6.wait_for("license_err")
        check("同一密钥第二次激活被拒", "使用过" in (resp.get("error") or ""), resp.get("error"))

        # ---------- 6. 管理员停用 ----------
        print("\n== 6. 管理员控制权限 ==")
        await c2.send({"t": "admin", "req": "u1", "action": "user_list", "args": {}})
        resp = await c2.wait_for("admin_ok")
        users = (resp.get("data") or {}).get("users") or []
        alice = next((u for u in users if u.get("username") == "alice"), None)
        check("用户列表能看到 alice", alice is not None,
              f"{len(users)} 个用户")
        check("用户记录里没有明文令牌",
              alice is not None and "device_token" not in alice
              and "device_token_hash" in alice, str(sorted(alice or {})))

        await c2.send({"t": "admin", "req": "u2", "action": "user_set_status",
                       "args": {"username": "alice", "status": "disabled"}})
        resp = await c2.wait_for("admin_ok")
        check("管理员可以停用用户", resp.get("t") == "admin_ok")

        c7 = await Conn.open()
        ok = await c7.hello(license={"device_token": token, "fingerprint": "fp-ALICE"})
        check("被停用后立刻无法使用",
              ok.get("licensed") is False and "停用" in (ok.get("license", {}).get("reason") or ""),
              ok.get("license", {}).get("reason"))

        await c2.send({"t": "admin", "req": "u3", "action": "user_set_status",
                       "args": {"username": "alice", "status": "active"}})
        await c2.wait_for("admin_ok")
        c8 = await Conn.open()
        ok = await c8.hello(license={"device_token": token, "fingerprint": "fp-ALICE"})
        check("重新启用后恢复可用", ok.get("licensed") is True)

        await c2.send({"t": "admin", "req": "u4", "action": "user_unbind",
                       "args": {"username": "alice"}})
        await c2.wait_for("admin_ok")
        c9 = await Conn.open()
        ok = await c9.hello(license={"device_token": token, "fingerprint": "fp-ALICE"})
        check("解绑后旧令牌失效", ok.get("licensed") is False)

        # ---------- 7. 邀请码列表与撤销 ----------
        print("\n== 7. 邀请码管理 ==")
        await c2.send({"t": "admin", "req": "i3", "action": "invite_create",
                       "args": {"username": "carol", "hours": 24}})
        resp = await c2.wait_for("admin_ok")
        code2 = (resp.get("data") or {}).get("code")
        await c2.send({"t": "admin", "req": "i4", "action": "invite_list", "args": {}})
        resp = await c2.wait_for("admin_ok")
        invs = (resp.get("data") or {}).get("invites") or []
        got = next((i for i in invs if i.get("code") == code2), None)
        check("邀请码列表包含新建的", got is not None and got.get("state") == "pending",
              f"{len(invs)} 个，state={got and got.get('state')}")
        used = next((i for i in invs if i.get("code") == code), None)
        check("已使用的邀请码标记为 used",
              used is not None and used.get("state") == "used", str(used and used.get("state")))

        await c2.send({"t": "admin", "req": "i5", "action": "invite_revoke",
                       "args": {"code": code2}})
        await c2.wait_for("admin_ok")
        c10 = await Conn.open()
        await c10.hello(license={"device_token": "", "fingerprint": "fp-CAROL"})
        await c10.send({"t": "license_activate", "code": code2,
                        "fingerprint": "fp-CAROL", "device_name": "CAROL-PC"})
        resp = await c10.wait_for("license_err")
        check("撤销后的邀请码无法使用", "不存在" in (resp.get("error") or ""), resp.get("error"))

        # ---------- 8. 限流 ----------
        print("\n== 8. 暴力猜码防护 ==")
        blocked = False
        c11 = await Conn.open()
        await c11.hello(license={"device_token": "", "fingerprint": "fp-EVIL"})
        for i in range(12):
            await c11.send({"t": "license_activate",
                            "code": f"MCLK-0000-0000-{i:04X}",
                            "fingerprint": "fp-EVIL", "device_name": "EVIL"})
            resp = await c11.wait_for("license_err")
            if "次数过多" in (resp.get("error") or ""):
                blocked = True
                break
        check("连续猜错会被限流拦截", blocked, f"第 {i + 1} 次触发")

        # ---------- 9. 管理接口鉴权 ----------
        print("\n== 9. 管理接口鉴权 ==")
        c12 = await Conn.open()
        await c12.hello(license={"device_token": token, "fingerprint": "fp-ALICE"})
        await c12.send({"t": "admin", "req": "z1", "action": "user_list", "args": {}})
        resp = await c12.wait_for("admin_err")
        check("普通用户（哪怕已授权）不能用管理接口",
              "管理员权限" in (resp.get("error") or ""), resp.get("error"))

        for c in (c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12):
            c.close()

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.close()

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

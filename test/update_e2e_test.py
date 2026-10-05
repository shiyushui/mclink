#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 昵称 / 客户端列表 / 自动更新 端到端测试
=============================================
起一个真实的 mclink_server，用原始控制协议验证三件事：

  1. **昵称**：客户端在握手和激活时上报昵称，服务端认下来并写进授权库
  2. **客户端列表**：管理员 client_list 能看到昵称、用户名、用的哪张邀请码、
     来源 IP、客户端版本、在线状态（邀请码只给掩码，不泄露完整码）
  3. **自动更新**：没有清单时不提示；发布清单后提示新版本；
     分块下载内容与源文件逐字节一致、sha256 对得上；release 优先比较

顺带守住"激活之后管理员仍然能管理"这条线 —— 这是踩过的坑：
    * 管理员密钥走配置文件时必须被认（is_admin=true）
    * 激活了普通用户之后，管理员的各条管理指令仍然可用
    * 断线重连之后 is_admin 不能掉
    * 普通用户（哪怕已激活）**不能**用管理接口

更新清单要放在仓库里的 server/updates/ 下（服务端默认就读那里），
不能放系统临时目录 —— 有的环境对子进程读临时目录另有权限限制。

    python test/update_e2e_test.py
"""

import asyncio
import base64
import hashlib
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
TMP = os.path.join(HERE, "_tmp_update_e2e")
UPD = os.path.join(ROOT, "server", "updates")     # 服务端默认的清单目录

HOST = "127.0.0.1"
CTRL = 19200
PUB = 19300
ADMIN = secrets.token_urlsafe(32)
MASTER = secrets.token_urlsafe(24)
CUR_VER = "1.0.0"
CUR_REL = "20260101"

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
        return json.loads((await asyncio.wait_for(
            self.r.readexactly(n), timeout)).decode())

    async def wait_for(self, t, timeout=8):
        """跳过周期性的 stats 推送，等到指定类型的消息。"""
        end = time.time() + timeout
        while time.time() < end:
            msg = await self.recv(max(1, end - time.time()))
            if msg.get("t") == t:
                return msg
        raise TimeoutError(f"没有等到 {t}")

    async def hello(self, fingerprint="fp", nickname=None, version=CUR_VER,
                    release=CUR_REL, admin_key=None, device_token=""):
        payload = {"t": "hello", "token": MASTER, "name": "TEST-PC",
                   "version": version, "release": release,
                   "license": {"device_token": device_token,
                               "fingerprint": fingerprint,
                               "device_name": "TEST-PC"}}
        if nickname is not None:
            payload["nickname"] = nickname
        if admin_key:
            payload["admin_token"] = admin_key
        await self.send(payload)
        return await self.recv()

    async def admin(self, req, action, args=None, timeout=8, want="admin_ok"):
        await self.send({"t": "admin", "req": req, "action": action,
                         "args": args or {}})
        return await self.wait_for(want, timeout)

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


def publish_update(blob, ver, rel, notes=""):
    """把清单和包写进 server/updates/（服务端默认读这里）。"""
    os.makedirs(UPD, exist_ok=True)
    zname = "mclink-client-windows.zip"
    with open(os.path.join(UPD, zname), "wb") as fh:
        fh.write(blob)
    man = {
        "version": ver, "release": rel, "notes": notes,
        "packages": {"windows": {
            "file": zname, "version": ver, "release": rel,
            "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}},
    }
    with open(os.path.join(UPD, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=2)


def clean_updates():
    for f in ("manifest.json", "mclink-client-windows.zip"):
        try:
            os.remove(os.path.join(UPD, f))
        except OSError:
            pass
    try:
        os.rmdir(UPD)
    except OSError:
        pass


async def main() -> int:
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    clean_updates()                     # 从干净状态开始（先测"没有清单"）

    cfg = os.path.join(TMP, "server.json")
    make_cfg(cfg)
    log = open(os.path.join(TMP, "server.log"), "w", encoding="utf-8")
    blob = os.urandom(300 * 1024)       # 假的"更新包"，只测传输完整性

    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "server", "mclink_server.py"),
         "-c", cfg], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    conns = []
    try:
        for _ in range(40):
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(HOST, CTRL), 1)
                w.close()
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            print("服务端没起来")
            return 1

        # ---------- 1. 管理员 ----------
        print("\n== 1. 管理员握手（admin_token 写在配置里） ==")
        admin = await Conn.open()
        conns.append(admin)
        ok = await admin.hello(fingerprint="fp-ADMIN", nickname="管理员",
                               admin_key=ADMIN)
        check("配置里的管理员密钥被认成管理员（is_admin=true）",
              ok.get("is_admin") is True,
              f"is_admin={ok.get('is_admin')} licensed={ok.get('licensed')}")

        resp = await admin.admin("a1", "invite_create",
                                 {"username": "alice", "hours": 24})
        code = (resp.get("data") or {}).get("code")
        check("管理员能生成邀请码", bool(code), code)

        # ---------- 2. 普通用户带昵称激活 ----------
        print("\n== 2. 用户激活并带昵称 ==")
        user = await Conn.open()
        conns.append(user)
        ok = await user.hello(fingerprint="fp-ALICE", nickname="小明")
        check("普通用户不是管理员（符合预期）", ok.get("is_admin") is False)

        await user.send({"t": "license_activate", "code": code,
                         "fingerprint": "fp-ALICE", "device_name": "ALICE-PC"})
        resp = await user.wait_for("license_ok")
        check("激活成功", bool(resp.get("device_token")))

        await user.send({"t": "hello_meta", "nickname": "小明", "version": CUR_VER})
        resp = await user.wait_for("hello_meta_ok")
        check("昵称上报被服务端接受", resp.get("nickname") == "小明",
              str(resp.get("nickname")))

        # ---------- 3. 管理员看客户端列表 ----------
        print("\n== 3. 管理员「客户端」列表 ==")
        resp = await admin.admin("c1", "client_list")
        rows = (resp.get("data") or {}).get("clients") or []
        alice = next((r for r in rows if r.get("username") == "alice"), None)
        check("列表里有 alice", alice is not None, f"{len(rows)} 行")
        check("能看到昵称「小明」", alice and alice.get("nickname") == "小明",
              str(alice and alice.get("nickname")))
        check("能看到用的哪张邀请码（掩码）",
              alice and (alice.get("invite") or "").startswith("MCLK-")
              and "****" in (alice.get("invite") or ""),
              str(alice and alice.get("invite")))
        check("能看到来源 IP", alice and alice.get("ip") not in (None, "", "—"),
              str(alice and alice.get("ip")))
        check("能看到客户端版本", alice and alice.get("version") == CUR_VER,
              str(alice and alice.get("version")))
        check("能看到在线状态", bool(alice and alice.get("online")))

        # ---------- 4. 激活之后管理员还能不能管（核心回归点） ----------
        print("\n== 4. 激活之后管理员仍然能管理 ==")
        for i, (action, args) in enumerate((
                ("invite_list", {}), ("user_list", {}), ("client_list", {}),
                ("stats", {}), ("update_status", {}))):
            resp = await admin.admin(f"m{i}", action, args)
            check(f"激活后管理员仍可执行 {action}", resp.get("t") == "admin_ok",
                  str(resp.get("error") or ""))

        resp = await admin.admin("m-rev", "invite_create",
                                 {"username": "bob", "hours": 24})
        bob_code = (resp.get("data") or {}).get("code")
        resp = await admin.admin("m-rev2", "invite_revoke", {"code": bob_code})
        check("激活后管理员仍能撤销邀请码（管理使用权）",
              resp.get("t") == "admin_ok", str(resp.get("error") or ""))

        # ---------- 4b. 删用户时邀请码要留档（不能删） ----------
        print("\n== 4b. 删除用户：邀请码留成历史记录 ==")
        resp = await admin.admin("del-1", "invite_create",
                                 {"username": "dave", "hours": 24})
        dave_code = (resp.get("data") or {}).get("code")
        dconn = await Conn.open()
        conns.append(dconn)
        await dconn.hello(fingerprint="fp-DAVE", nickname="Dave")
        await dconn.send({"t": "license_activate", "code": dave_code,
                          "fingerprint": "fp-DAVE", "device_name": "DAVE-PC"})
        resp = await dconn.wait_for("license_ok")
        check("dave 激活成功", bool(resp.get("device_token")))

        resp = await admin.admin("del-list1", "invite_list")
        invs = (resp.get("data") or {}).get("invites") or []
        check("删除前列表里能看到 dave 的已用密钥",
              any(i.get("username") == "dave" for i in invs))

        resp = await admin.admin("del-2", "user_delete", {"username": "dave"})
        detail = resp.get("data") or {}
        check("删除用户返回了留档明细",
              detail.get("username") == "dave"
              and int(detail.get("invites_kept") or 0) >= 1, str(detail))

        resp = await admin.admin("del-list2", "invite_list")
        data = resp.get("data") or {}
        invs = data.get("invites") or []
        check("默认列表里不再显示（不碍眼）",
              not any(i.get("username") == "dave" for i in invs),
              str([i.get("username") for i in invs]))
        check("但服务端报出还有历史记录（数据没丢）",
              int(data.get("history_count") or 0) >= 1, str(data))

        resp = await admin.admin("del-list3", "invite_list",
                                 {"include_history": True})
        invs = (resp.get("data") or {}).get("invites") or []
        check("勾选显示历史后能查到 dave 的密钥（留档成功）",
              any(i.get("username") == "dave" for i in invs),
              str([i.get("username") for i in invs]))

        resp = await admin.admin("del-list4", "user_list")
        check("dave 不在用户列表里了",
              not any(u.get("username") == "dave"
                      for u in (resp.get("data") or {}).get("users") or []))
        check("别人的邀请码没被影响",
              any(i.get("username") == "alice" for i in invs))

        resp = await admin.admin("del-3", "user_delete", {"username": "nobody"},
                                 want="admin_err")
        check("删除不存在的用户报错", resp.get("t") == "admin_err",
              str(resp.get("error") or ""))

        # 断线重连：旧实现在这里会把 is_admin 抹掉
        admin.close()
        conns.remove(admin)
        admin = await Conn.open()
        conns.append(admin)
        ok = await admin.hello(fingerprint="fp-ADMIN", nickname="管理员",
                               admin_key=ADMIN)
        check("重连后 is_admin 仍然是 true", ok.get("is_admin") is True,
              str(ok.get("is_admin")))
        resp = await admin.admin("r1", "invite_create",
                                 {"username": "carol", "hours": 24})
        check("重连后仍能生成邀请码", resp.get("t") == "admin_ok",
              str(resp.get("error") or ""))

        # 普通用户不能管理 —— 这是对的，不是 bug
        await user.send({"t": "admin", "req": "x1", "action": "invite_list",
                         "args": {}})
        resp = await user.wait_for("admin_err")
        check("普通用户（已激活）没有管理权（符合预期）",
              "管理员权限" in (resp.get("error") or ""), resp.get("error"))

        # ---------- 5. 自动更新：还没发布 ----------
        print("\n== 5. 自动更新（服务端还没发布更新包） ==")
        c = await Conn.open()
        conns.append(c)
        await c.hello(fingerprint="fp-UPD", nickname="更新测试")
        await c.send({"t": "update_check", "version": CUR_VER, "release": CUR_REL,
                      "os": "windows"})
        resp = await c.wait_for("update_info")
        check("没有清单时不提示更新", resp.get("available") is False, str(resp))

        # ---------- 6. 发布了新版本 ----------
        print("\n== 6. 发布新版本之后 ==")
        publish_update(blob, "1.3.0", "20261231", notes="测试更新")
        await c.send({"t": "update_check", "version": CUR_VER, "release": CUR_REL,
                      "os": "windows"})
        resp = await c.wait_for("update_info")
        check("发现新版本", resp.get("available") is True,
              f"latest={resp.get('latest')}")
        check("带上版本号/大小/sha256",
              resp.get("latest") == "1.3.0" and int(resp.get("size") or 0) == len(blob)
              and len(str(resp.get("sha256") or "")) == 64,
              f"size={resp.get('size')}")
        check("带上更新说明", resp.get("notes") == "测试更新", str(resp.get("notes")))

        c3 = await Conn.open()
        conns.append(c3)
        await c3.hello(fingerprint="fp-UPD3", version="1.3.0", release="20261231")
        await c3.send({"t": "update_check", "version": "1.3.0",
                       "release": "20261231", "os": "windows"})
        resp = await c3.wait_for("update_info")
        check("同版本不提示更新", resp.get("available") is False)

        c4 = await Conn.open()
        conns.append(c4)
        await c4.hello(fingerprint="fp-UPD4", version="1.0.0", release="20270101")
        await c4.send({"t": "update_check", "version": "1.0.0",
                       "release": "20270101", "os": "windows"})
        resp = await c4.wait_for("update_info")
        check("客户端批次更新 → 不提示（release 优先）",
              resp.get("available") is False)

        # ---------- 7. 分块下载 ----------
        print("\n== 7. 分块下载更新包 ==")
        got = bytearray()
        rounds = 0
        while True:
            rounds += 1
            await c.send({"t": "update_fetch", "offset": len(got),
                          "chunk": 128 * 1024, "os": "windows"})
            resp = await c.wait_for("update_chunk")
            got.extend(base64.b64decode(resp.get("data") or ""))
            if resp.get("done") or rounds > 40:
                break
        check("分块下载内容与源文件逐字节一致", bytes(got) == blob,
              f"{len(got)} / {len(blob)} 字节，{rounds} 块")
        check("sha256 对得上",
              hashlib.sha256(bytes(got)).hexdigest() == hashlib.sha256(blob).hexdigest())
        check("确实分了多块（不是一次性塞）", rounds >= 2, f"{rounds} 块")

        # ---------- 8. 管理员「更新」页 ----------
        print("\n== 8. 管理员「更新」页 ==")
        resp = await admin.admin("u1", "update_status")
        data = resp.get("data") or {}
        check("管理台能看到已发布的版本",
              (data.get("packages") or {}).get("windows", {}).get("version") == "1.3.0",
              str(data.get("packages")))
        check("管理台能看到在线客户端版本分布",
              isinstance(data.get("clients"), dict) and data["clients"],
              f"（演示机上还有别的客户端在连，所以 clients 可能不止一条）"
              if not data.get("clients") else str(data.get("clients")))

        for c in conns:
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
        clean_updates()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    if passed != len(results):
        print("\n---- 服务端日志尾部 ----")
        print(open(os.path.join(TMP, "server.log"), encoding="utf-8").read()[-3000:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

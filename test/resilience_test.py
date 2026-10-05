#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 容灾测试
===============
验证真实使用中最容易翻车的三种情况能否自愈：

  1. 本地游戏服务端没开 —— 映射应报错，但隧道本身不能崩
  2. 本地游戏服务端重启 —— 应自动恢复转发（无需重启 McLink）
  3. 服务端重启 —— 客户端应自动重连并重新注册所有映射

    python test/resilience_test.py
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp_resilience")

TOKEN = secrets.token_urlsafe(24)
HOST = "127.0.0.1"
CTRL, DATA, UDPP = 18000, 18000, 18000     # 单端口复用模式
PUB_TCP = 18100
LOCAL_TCP = 18150
WEB_PORT = 18887

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return ok


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


async def http(method, path, body=None, timeout=6):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, WEB_PORT), timeout)
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


async def relay_ok(payload=b"hi", timeout=6):
    """从公网端口走一圈，看能否拿到本地回声。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(HOST, PUB_TCP), timeout)
        w.write(payload)
        await w.drain()
        got = await asyncio.wait_for(r.read(100), timeout)
        w.close()
        return got == b"ECHO:" + payload, repr(got)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def wait_state(pred, timeout=25):
    end = time.time() + timeout
    state = {}
    while time.time() < end:
        try:
            st, state = await http("GET", "/api/state")
            if pred(state):
                return True, state
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False, state


async def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    srv_cfg = os.path.join(TMP, "server.json")
    cli_cfg = os.path.join(TMP, "client.json")

    with open(srv_cfg, "w", encoding="utf-8") as fh:
        json.dump({"bind": "0.0.0.0", "control_port": CTRL, "data_port": DATA,
                   "udp_port": UDPP, "tokens": [TOKEN], "public_ip": HOST, "tls": False,
                   "allowed_ports": [[18100, 18110]], "log_level": "debug"}, fh)
    with open(cli_cfg, "w", encoding="utf-8") as fh:
        json.dump({"server": {"host": HOST, "control_port": CTRL, "data_port": DATA,
                              "udp_port": UDPP, "token": TOKEN, "tls": False},
                   "web": {"host": HOST, "port": WEB_PORT, "token": ""},
                   # 这个套件测的是"注册之后"的行为，所以要显式打开自动开启
                   # （客户端默认 auto_start_mappings=false：启动不自动开映射）
                   "client": {"auto_start_mappings": True},
                   "mappings": [{"id": "t1", "name": "MC", "proto": "tcp",
                                 "local_host": HOST, "local_port": LOCAL_TCP,
                                 "remote_port": PUB_TCP, "enabled": True}],
                   "log_level": "debug"}, fh)

    srv_log = open(os.path.join(TMP, "server.log"), "w", encoding="utf-8")
    cli_log = open(os.path.join(TMP, "client.log"), "w", encoding="utf-8")
    procs = []
    echo_srv = None

    def start_server():
        return subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "mclink_server.py"), "-c", srv_cfg],
            cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT)

    def start_client():
        return subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"), "-c", cli_cfg],
            cwd=ROOT, stdout=cli_log, stderr=subprocess.STDOUT)

    try:
        # ---------- 场景 1：本地游戏没开 ----------
        print("\n== 场景 1: 本地游戏服务端未启动 ==")
        p_srv = start_server()
        procs.append(p_srv)
        p_cli = start_client()
        procs.append(p_cli)

        ok, state = await wait_state(
            lambda s: s.get("server", {}).get("connected")
            and any(m["id"] == "t1" for m in s.get("mappings", [])), 25)
        check("客户端已连上服务端并注册映射", ok,
              f"connected={state.get('server', {}).get('connected')}")

        await asyncio.sleep(1)
        good, detail = await relay_ok()
        check("本地游戏未启动时转发被拒绝（不会假装成功）", not good, detail)

        try:
            st, state = await http("GET", "/api/state")
            m = next((x for x in state["mappings"] if x["id"] == "t1"), None)
            check("控制台给出了明确的本机端口错误",
                  m is not None and m["status"] == "error" and m["error"],
                  str(m and m["error"]))
        except Exception as exc:
            check("控制台给出了明确的本机端口错误", False, repr(exc))

        # ---------- 场景 2：本地游戏启动 / 重启 ----------
        print("\n== 场景 2: 本地游戏服务端启动 ==")
        echo_srv = await asyncio.start_server(tcp_echo, HOST, LOCAL_TCP)
        await asyncio.sleep(0.5)
        good, detail = await relay_ok(b"retry")
        check("本地游戏启动后自动恢复转发（无需重启 McLink）", good, detail)

        print("\n== 场景 3: 本地游戏服务端重启 ==")
        echo_srv.close()
        await echo_srv.wait_closed()
        echo_srv = None
        await asyncio.sleep(0.3)
        bad, d1 = await relay_ok(b"down")
        check("关闭本地游戏后转发失败", not bad, d1)
        echo_srv = await asyncio.start_server(tcp_echo, HOST, LOCAL_TCP)
        await asyncio.sleep(0.3)
        good, detail = await relay_ok(b"back")
        check("本地游戏重启后自动恢复转发", good, detail)

        # ---------- 场景 4：服务端重启 ----------
        print("\n== 场景 4: 服务端重启后客户端自动重连 ==")
        p_cli_before = p_cli.pid
        p_srv.terminate()
        try:
            p_srv.wait(timeout=6)
        except subprocess.TimeoutExpired:
            p_srv.kill()
        procs.remove(p_srv)

        ok, state = await wait_state(
            lambda s: not s.get("server", {}).get("connected"), 15)
        check("服务端挂掉后控制台显示已断开", ok,
              f"connected={state.get('server', {}).get('connected')}, "
              f"err={state.get('server', {}).get('last_error')!r}")

        # 客户端会自动退避重连，把服务端拉起来
        await asyncio.sleep(2)
        p_srv = start_server()
        procs.append(p_srv)

        ok, state = await wait_state(
            lambda s: (s.get("server", {}).get("connected")
                       and any(m["id"] == "t1" and m["status"] == "active"
                               for m in s.get("mappings", []))), 60)
        check("客户端自动重连并重新注册映射", ok,
              f"connected={state.get('server', {}).get('connected')}")
        check("客户端进程没有因为断线而重启", p_cli.poll() is None
              and p_cli.pid == p_cli_before, f"pid={p_cli.pid}")

        await asyncio.sleep(1)
        good, detail = await relay_ok(b"after-restart")
        check("服务端重启后转发恢复正常", good, detail)

        # ---------- 场景 5：客户端重启后服务端清理旧映射 ----------
        print("\n== 场景 5: 客户端重启后端口不残留 ==")
        p_cli.terminate()
        try:
            p_cli.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p_cli.kill()
        procs.remove(p_cli)
        await asyncio.sleep(1.0)

        p_cli = start_client()
        procs.append(p_cli)
        ok, state = await wait_state(
            lambda s: (s.get("server", {}).get("connected")
                       and any(m["id"] == "t1" and m["status"] == "active"
                               for m in s.get("mappings", []))), 40)
        check("客户端重启后能重新占用同一公网端口（无残留）", ok,
              str([(m["id"], m["status"]) for m in state.get("mappings", [])]))
        await asyncio.sleep(0.5)
        good, detail = await relay_ok(b"restarted")
        check("客户端重启后转发正常", good, detail)

    finally:
        if echo_srv:
            echo_srv.close()
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        srv_log.close()
        cli_log.close()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    bad = [n for n, ok, _ in results if not ok]
    for n in bad:
        print(f"  - 失败: {n}")
    print("=" * 60)
    if bad:
        print("\n---- 客户端日志尾部 ----")
        print(open(os.path.join(TMP, "client.log"), encoding="utf-8").read()[-2500:])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

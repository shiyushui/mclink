#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 端到端测试
=================
在本机真实启动 服务端 + 客户端（各自独立进程），然后验证：
  1. TCP 端口映射（模拟 Minecraft Java 版）
  2. UDP 端口映射（模拟 Minecraft 基岩版）
  3. 本地控制台 HTTP API（增 / 删 / 改 / 启停 / 状态 / SSE）
跑完自动清理。-v 参数打印子进程日志。

    python test/e2e_test.py
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp")
VERBOSE = "-v" in sys.argv

TOKEN = secrets.token_urlsafe(24)
HOST = "127.0.0.1"
# 默认单端口模式：控制通道与 TCP 数据通道共用 17000，UDP 隧道也用 17000
CTRL, DATA, UDPP = 17000, 17000, 17000
UNUSED_DATA_PORT = 17001          # 不该有人监听
PUB_TCP, PUB_UDP = 17100, 17101
LOCAL_TCP, LOCAL_UDP = 17150, 17151
WEB_PORT = 17887

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""), flush=True)
    return ok


# ---------------------------------------------------------------- 假游戏服务器

async def tcp_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """模拟游戏服务端：收到什么就回 'ECHO:' + 内容。"""
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
    def connection_made(self, transport):
        self.transport = transport
        self.seen = []

    def datagram_received(self, data, addr):
        self.seen.append(data)
        self.transport.sendto(b"UDP-ECHO:" + data, addr)


# ---------------------------------------------------------------- HTTP 小客户端

async def http(method: str, path: str, body: dict | None = None, timeout=5):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(HOST, WEB_PORT), timeout)
    try:
        payload = b"" if body is None else json.dumps(body).encode()
        req = (f"{method} {path} HTTP/1.1\r\nHost: {HOST}:{WEB_PORT}\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode()
        writer.write(req + payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout)
    finally:
        writer.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    try:
        return status, json.loads(rest.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"_raw": rest.decode("utf-8", "ignore")}


async def wait_port(host, port, timeout=15.0, udp=False) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if udp:
            return True
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
            w.close()
            return True
        except Exception:
            await asyncio.sleep(0.25)
    return False


# ---------------------------------------------------------------- 主流程

async def main() -> int:
    for exe in (sys.executable,):
        if not exe:
            print("找不到 Python 解释器")
            return 2

    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---- 公网 IP 判定逻辑（阿里云 EIP 场景的关键） ----
    print("\n== 单元检查: 公网 IP 判定 ==")
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import mclink_server                                    # noqa: E402
    cases = {
        "47.123.45.67": True, "8.8.8.8": True, "223.5.5.5": True,
        "192.168.1.8": False, "10.0.0.5": False, "127.0.0.1": False,
        "172.16.0.1": False, "172.31.255.254": False, "172.32.0.1": True,
        "100.100.100.200": False, "100.64.0.1": False, "169.254.1.1": False,
        "224.0.0.1": False, "": False, "not-an-ip": False,
    }
    wrong = [f"{ip}->{mclink_server.is_public_ip(ip)}" for ip, want in cases.items()
             if mclink_server.is_public_ip(ip) != want]
    check("内网/EIP/保留地址不会被误判成公网 IP", not wrong, ", ".join(wrong) or "15 个用例全对")
    check("UDP 包头魔数与签名格式正确",
          mclink_server.pack_udp_data(1, ("1.2.3.4", 80), b"x", TOKEN)[:2] == b"ML")

    srv_cfg = os.path.join(TMP, "server.json")
    cli_cfg = os.path.join(TMP, "client.json")
    srv_log = open(os.path.join(TMP, "server.log"), "w", encoding="utf-8")
    cli_log = open(os.path.join(TMP, "client.log"), "w", encoding="utf-8")

    with open(srv_cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "bind": "0.0.0.0",
            "control_port": CTRL, "data_port": DATA, "udp_port": UDPP,
            "tokens": [TOKEN], "public_ip": HOST, "tls": False,
            "allowed_ports": [[17100, 17110]],
            "dial_timeout_s": 8, "idle_timeout_s": 60,
            "heartbeat_s": 20, "log_level": "debug", "log_file": "",
        }, fh)

    with open(cli_cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "server": {"host": HOST, "control_port": CTRL,
                       "data_port": DATA, "udp_port": UDPP, "token": TOKEN, "tls": False},
            "web": {"host": HOST, "port": WEB_PORT, "token": ""},
            # 端到端测试要的是"注册好之后能用"，所以显式打开自动开启映射
            # （客户端默认 auto_start_mappings=false：启动不自动开）
            "client": {"auto_start_mappings": True},
            "mappings": [
                {"id": "tcp1", "name": "MC Java", "proto": "tcp",
                 "local_host": HOST, "local_port": LOCAL_TCP,
                 "remote_port": PUB_TCP, "enabled": True},
                {"id": "udp1", "name": "MC Bedrock", "proto": "udp",
                 "local_host": HOST, "local_port": LOCAL_UDP,
                 "remote_port": PUB_UDP, "enabled": True},
            ],
            "log_level": "debug", "log_file": "",
        }, fh, ensure_ascii=False)

    # ---- 启动假游戏服务端 ----
    tcp_srv = await asyncio.start_server(tcp_echo, HOST, LOCAL_TCP)
    loop = asyncio.get_running_loop()
    udp_echo = UdpEcho()
    await loop.create_datagram_endpoint(lambda: udp_echo, local_addr=(HOST, LOCAL_UDP))
    print(f"假游戏服务端就绪: TCP {LOCAL_TCP}, UDP {LOCAL_UDP}")

    # ---- 启动 McLink 服务端与客户端 ----
    procs = []
    try:
        p_srv = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "mclink_server.py"),
             "-c", srv_cfg],
            cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(("server", p_srv))

        if not await wait_port(HOST, CTRL, 15):
            print("服务端控制端口未就绪，测试中止")
            return 1
        print("McLink 服务端已启动")

        p_cli = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"),
             "-c", cli_cfg],
            cwd=ROOT, stdout=cli_log, stderr=subprocess.STDOUT)
        procs.append(("client", p_cli))

        if not await wait_port(HOST, WEB_PORT, 15):
            print("客户端控制台未就绪，测试中止")
            return 1
        print("McLink 客户端已启动")

        # ---- 等待映射注册完成 ----
        print("\n== 等待映射生效 ==")
        ok = False
        state = {}
        for _ in range(40):
            st, state = await http("GET", "/api/state")
            ms = {m["id"]: m for m in state.get("mappings", [])}
            if (state.get("server", {}).get("connected")
                    and ms.get("tcp1", {}).get("status") == "active"):
                ok = True
                break
            await asyncio.sleep(0.5)
        check("客户端成功注册 TCP 映射", ok,
              f"public_ip={state.get('server', {}).get('public_ip')}")
        check("控制台公网 IP 正确", state.get("server", {}).get("public_ip") == HOST,
              str(state.get("server", {}).get("public_ip")))

        # ---- 单端口复用验证 ----
        print("\n== 单端口复用验证 ==")
        st, info = await http("GET", "/api/server_info")
        check("服务端通告数据端口 = 控制端口",
              info.get("info", {}).get("data_port") == CTRL
              and info.get("info", {}).get("shared_data_port") is True,
              f"data_port={info.get('info', {}).get('data_port')}, "
              f"shared={info.get('info', {}).get('shared_data_port')}")
        free = True
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(HOST, UNUSED_DATA_PORT), 1.5)
            w.close()
            free = False
        except Exception:
            pass
        check("不再单独监听第二个 TCP 数据端口", free,
              f"{UNUSED_DATA_PORT} " + ("空闲 ✓" if free else "仍在监听 ✗"))
        check("UDP 隧道端口被服务端正确通告",
              info.get("info", {}).get("udp_port") == UDPP,
              f"udp_port={info.get('info', {}).get('udp_port')}")

        # 给 UDP 保活留出时间
        await asyncio.sleep(2.5)

        # ---- 测试 1: TCP 转发 ----
        print("\n== 测试 TCP 端口映射 ==")
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(HOST, PUB_TCP), 5)
            w.write(b"hello-minecraft")
            await w.drain()
            got = await asyncio.wait_for(r.read(100), 5)
            w.close()
            check("TCP 公网端口可连接并回环", got == b"ECHO:hello-minecraft", repr(got))
        except Exception as exc:
            check("TCP 公网端口可连接并回环", False, f"{type(exc).__name__}: {exc}")

        # 多连接并发
        async def one(i):
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, PUB_TCP), 5)
            w.write(f"conn-{i}".encode())
            await w.drain()
            data = await asyncio.wait_for(r.read(100), 5)
            w.close()
            return data == f"ECHO:conn-{i}".encode()

        try:
            rs = await asyncio.gather(*[one(i) for i in range(8)])
            check("TCP 8 条并发连接全部正确", all(rs), f"{sum(rs)}/8")
        except Exception as exc:
            check("TCP 8 条并发连接全部正确", False, f"{type(exc).__name__}: {exc}")

        # ---- 测试 2: UDP 转发 ----
        print("\n== 测试 UDP 端口映射 ==")
        got_udp = []

        class Tester(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                got_udp.append(data)

        t = Tester()
        await loop.create_datagram_endpoint(lambda: t, local_addr=(HOST, 0))
        for i in range(3):
            t.transport.sendto(f"bedrock-{i}".encode(), (HOST, PUB_UDP))
            await asyncio.sleep(0.2)
        for _ in range(20):
            if len(got_udp) >= 3:
                break
            await asyncio.sleep(0.25)
        check("UDP 公网端口收发正常（含首包）",
              got_udp[:3] == [b"UDP-ECHO:bedrock-0", b"UDP-ECHO:bedrock-1",
                              b"UDP-ECHO:bedrock-2"],
              repr(got_udp[:3]))
        t.transport.close()

        # ---- 测试 3: 控制台 API ----
        print("\n== 测试控制台 API ==")
        st, state = await http("GET", "/api/state")
        check("GET /api/state", st == 200 and "mappings" in state, f"HTTP {st}")

        st, rep = await http("POST", "/api/mappings", {
            "id": "new1", "name": "泰拉瑞亚", "proto": "tcp",
            "local_host": HOST, "local_port": LOCAL_TCP,
            "remote_port": 17102, "enabled": True,
        })
        check("POST /api/mappings 新增映射", st == 200 and rep.get("ok"), str(rep))
        await asyncio.sleep(2.0)

        st, state = await http("GET", "/api/state")
        newm = next((m for m in state["mappings"] if m["id"] == "new1"), None)
        check("新映射被服务端接受并生效",
              newm is not None and newm["status"] == "active",
              str(newm and newm["status"]))
        check("新映射连接地址正确",
              bool(newm) and newm["connect_addr"] == f"{HOST}:17102",
              str(newm and newm["connect_addr"]))

        # 端口冲突校验
        st, rep = await http("POST", "/api/mappings", {
            "id": "dup", "name": "冲突", "proto": "tcp",
            "local_host": HOST, "local_port": LOCAL_TCP,
            "remote_port": 17102, "enabled": True,
        })
        check("重复公网端口被拒绝", st == 400 and not rep.get("ok"), str(rep))

        # 非法端口
        st, rep = await http("POST", "/api/mappings", {
            "id": "bad", "name": "非法", "proto": "tcp",
            "local_host": HOST, "local_port": LOCAL_TCP,
            "remote_port": 99999, "enabled": True,
        })
        check("非法端口被拒绝", st == 400 and not rep.get("ok"), str(rep))

        # ---- 端口池白名单 ----
        print("\n== 端口池白名单 ==")
        st, state = await http("GET", "/api/state")
        check("allowed_ports 随 /api/state 下发给 UI",
              state.get("server", {}).get("allowed_ports") == [[17100, 17110]],
              str(state.get("server", {}).get("allowed_ports")))

        st, rep = await http("POST", "/api/mappings", {
            "id": "pooled", "name": "池内端口", "proto": "tcp",
            "local_host": HOST, "local_port": LOCAL_TCP,
            "remote_port": 17105, "enabled": True,
        })
        check("端口池内的端口可以创建", st == 200 and rep.get("ok"), str(rep)[:120])
        await asyncio.sleep(2.0)
        st, state = await http("GET", "/api/state")
        pm = next((m for m in state["mappings"] if m["id"] == "pooled"), None)
        check("池内端口映射正常生效",
              pm is not None and pm["status"] == "active", str(pm and pm["status"]))

        st, rep = await http("POST", "/api/mappings", {
            "id": "outside", "name": "白名单外", "proto": "tcp",
            "local_host": HOST, "local_port": LOCAL_TCP,
            "remote_port": 30000, "enabled": True,
        })
        check("端口池外的端口被客户端提前拦截",
              st == 400 and not rep.get("ok") and "允许范围" in (rep.get("error") or ""),
              str(rep))
        st, state = await http("GET", "/api/state")
        check("被拦截的映射没有被创建",
              all(m["id"] != "outside" for m in state["mappings"]))

        # 绕过客户端，直接问服务端要一个白名单外的端口 —— 验证服务端自己也把关
        try:
            rr, ww = await asyncio.wait_for(asyncio.open_connection(HOST, CTRL), 5)

            async def raw_send(obj):
                b = json.dumps(obj).encode()
                ww.write(struct.pack("!I", len(b)) + b)
                await ww.drain()

            async def raw_recv():
                hdr = await asyncio.wait_for(rr.readexactly(4), 5)
                (n,) = struct.unpack("!I", hdr)
                return json.loads((await asyncio.wait_for(rr.readexactly(n), 5)).decode())

            await raw_send({"t": "hello", "token": TOKEN, "name": "raw-probe",
                            "version": "test"})
            await raw_recv()
            await raw_send({"t": "register", "mapping": {
                "mid": "raw1", "name": "raw", "proto": "tcp", "remote_port": 30000}})
            resp = await raw_recv()
            ww.close()
            check("服务端自身也拒绝白名单外的端口（纵深防御）",
                  resp.get("t") == "register_err"
                  and "允许范围" in (resp.get("error") or ""), str(resp))
        except Exception as exc:
            check("服务端自身也拒绝白名单外的端口（纵深防御）", False,
                  f"{type(exc).__name__}: {exc}")

        # 关掉再开
        st, rep = await http("POST", "/api/mappings/new1/toggle", {"enabled": False})
        await asyncio.sleep(1.5)
        st, state = await http("GET", "/api/state")
        newm = next((m for m in state["mappings"] if m["id"] == "new1"), None)
        check("停用映射后状态为 inactive",
              newm is not None and newm["status"] == "inactive", str(newm and newm["status"]))
        st, rep = await http("POST", "/api/mappings/new1/toggle", {"enabled": True})
        await asyncio.sleep(2.5)
        st, state = await http("GET", "/api/state")
        newm = next((m for m in state["mappings"] if m["id"] == "new1"), None)
        check("重新启用后恢复 active",
              newm is not None and newm["status"] == "active", str(newm and newm["status"]))

        st, rep = await http("DELETE", "/api/mappings/new1")
        check("DELETE /api/mappings/{id}", st == 200 and rep.get("ok"), str(rep))
        st, rep = await http("DELETE", "/api/mappings/outside")
        await asyncio.sleep(1.5)
        st, state = await http("GET", "/api/state")
        check("删除后映射消失",
              all(m["id"] not in ("new1", "outside") for m in state["mappings"]))

        st, rep = await http("GET", "/api/server_info")
        check("GET /api/server_info", st == 200 and rep.get("ok"), str(rep.get("info")))

        st, rep = await http("GET", "/api/selftest")
        check("GET /api/selftest", st == 200 and rep.get("ok"),
              str([r["msg"] for r in rep.get("results", [])]))

        st, rep = await http("GET", "/api/logs?limit=10")
        check("GET /api/logs", st == 200 and len(rep.get("logs", [])) > 0,
              f"{len(rep.get('logs', []))} 条")

        # 统计是否在累加
        st, state = await http("GET", "/api/state")
        tcp1 = next(m for m in state["mappings"] if m["id"] == "tcp1")
        check("流量统计已累计", tcp1["rx_bytes"] > 0 and tcp1["tx_bytes"] > 0,
              f"rx={tcp1['rx_bytes']} tx={tcp1['tx_bytes']}")
        check("连接计数已累计", tcp1["total_conns"] >= 9,
              f"total={tcp1['total_conns']}")

        # ---- 测试 4: SSE ----
        print("\n== 测试 SSE 实时推送 ==")
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, WEB_PORT), 5)
            w.write(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
            got_state = False
            deadline = time.time() + 8
            while time.time() < deadline:
                line = await asyncio.wait_for(r.readline(), 4)
                if line.startswith(b"event: state"):
                    got_state = True
                    break
            await asyncio.wait_for(r.readline(), 3)
            w.close()
            check("SSE 推送 state 事件", got_state and b"200 OK" in head)
        except Exception as exc:
            check("SSE 推送 state 事件", False, f"{type(exc).__name__}: {exc}")

        # ---- 测试 6: CORS（支持双击 index.html 打开控制台） ----
        print("\n== 测试跨域支持 ==")

        async def raw(method, path, origin=None, extra: bytes = b""):
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, WEB_PORT), 5)
            req = f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
            if origin:
                req += f"Origin: {origin}\r\n"
            if method == "OPTIONS":
                req += ("Access-Control-Request-Method: POST\r\n"
                        "Access-Control-Request-Headers: content-type,x-mclink-token\r\n")
            req += "\r\n"
            w.write(req.encode() + extra)
            await w.drain()
            data = await asyncio.wait_for(r.read(-1), 5)
            w.close()
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin1").split("\r\n")
            hdrs = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    hdrs[k.strip().lower()] = v.strip()
            return int(lines[0].split(" ")[1]), hdrs, body

        st, h, _ = await raw("OPTIONS", "/api/mappings", "http://127.0.0.1:9999")
        check("跨域预检 OPTIONS 返回 204 与放行头",
              st == 204 and h.get("access-control-allow-origin") == "http://127.0.0.1:9999"
              and "x-mclink-token" in h.get("access-control-allow-headers", "").lower(),
              f"HTTP {st}, ACAO={h.get('access-control-allow-origin')}")

        st, h, _ = await raw("GET", "/api/state", "http://localhost:9999")
        check("本机来源 GET 带 CORS 头",
              st == 200 and h.get("access-control-allow-origin") == "http://localhost:9999",
              f"ACAO={h.get('access-control-allow-origin')}")

        st, h, _ = await raw("OPTIONS", "/api/mappings", "http://evil.example.com")
        check("非本机来源不下发 CORS 头", "access-control-allow-origin" not in h,
              f"ACAO={h.get('access-control-allow-origin')}")

        st, h, _ = await raw("POST", "/api/mappings", None, b"")
        check("空请求体返回 400 而不是挂死", st == 400, f"HTTP {st}")

        # ---- 测试 7: 静态页面 ----
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, WEB_PORT), 5)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await w.drain()
            raw = await asyncio.wait_for(r.read(-1), 5)
            w.close()
            check("GET / 返回控制台页面",
                  b"200 OK" in raw and b"<html" in raw.lower(), f"{len(raw)} 字节")
        except Exception as exc:
            check("GET / 返回控制台页面", False, f"{type(exc).__name__}: {exc}")

        # ---- 测试 6: 错误密钥 ----
        print("\n== 测试鉴权 ==")
        with open(cli_cfg, "r+", encoding="utf-8") as fh:
            cfg = json.load(fh)
            cfg["server"]["token"] = "wrong-token"
            cfg["web"]["port"] = 17888
            fh.seek(0)
            json.dump(cfg, fh)
            fh.truncate()
        p_bad = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "client", "mclink_client.py"),
             "-c", cli_cfg],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        procs.append(("client-bad", p_bad))
        await asyncio.sleep(4)
        bad_ok = False
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(HOST, 17888), 3)
            w.write(b"GET /api/state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await w.drain()
            raw = await asyncio.wait_for(r.read(-1), 4)
            w.close()
            body = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode())
            bad_ok = (body.get("server", {}).get("connected") is False
                      and "密钥" in (body.get("server", {}).get("last_error") or ""))
        except Exception as exc:
            bad_ok = False
            print(f"    (调试) {type(exc).__name__}: {exc}")
        check("错误密钥被服务端拒绝并提示", bad_ok)
        p_bad.terminate()

    finally:
        for name, p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        tcp_srv.close()
        srv_log.close()
        cli_log.close()

    # ---- 结果 ----
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failed = [n for n, ok, _ in results if not ok]
    print(f"结果: {passed}/{total} 通过")
    if failed:
        print("失败项:")
        for n in failed:
            print(f"  - {n}")
    print("=" * 60)

    if VERBOSE or failed:
        print("\n---- 服务端日志尾部 ----")
        print(open(os.path.join(TMP, "server.log"), encoding="utf-8").read()[-3000:])
        print("\n---- 客户端日志尾部 ----")
        print(open(os.path.join(TMP, "client.log"), encoding="utf-8").read()[-3000:])

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

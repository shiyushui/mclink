#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 服务端  (mclink_server.py)
=================================
运行在具有公网 IP 的服务器上（阿里云 ECS / Ubuntu、Debian、Alibaba Cloud Linux 等）。
纯 Python 3 标准库实现，无需安装任何第三方依赖。

它负责三件事：
  1. 控制通道 (control_port, TCP)  —— 客户端外联到它，完成鉴权、注册端口映射、心跳、统计上报。
  2. TCP 数据通道 (data_port, TCP) —— 有玩家连上公网端口时，服务端通知客户端"回连"，
     客户端主动拨一条 TCP 连接到 data_port，服务端把两条连接对接起来做裸字节转发。
  3. UDP 隧道通道 (udp_port, UDP)  —— 每个 UDP 映射复用这一条 UDP 通道，
     用 10 字节的自定义包头区分映射与玩家地址，实现 UDP 双向转发。

为什么这样设计：家宽通常没有公网 IP、且在 NAT 后面，服务端无法主动连客户端。
所以除 UDP 之外的所有连接都由客户端主动发起，服务端只负责"撮合"。

用法:
    python3 mclink_server.py -c config.server.json
    python3 mclink_server.py --gen-token          # 生成一个随机密钥
    python3 mclink_server.py --check-config       # 只检查配置
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time

try:
    from mclink_license import LicenseStore, RateLimiter, mask_code
except ImportError:                                  # 允许从别的目录直接跑
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mclink_license import LicenseStore, RateLimiter, mask_code

VERSION = "1.2.8"

# ---------------------------------------------------------------- 常量

MAX_JSON = 1 << 20          # 单条控制消息最大 1MB
BUF_SIZE = 65536            # 转发缓冲区
MAGIC = b"ML"               # UDP 包魔数
UDP_REG = 1                 # UDP: 隧道注册/保活
UDP_DATA = 2                # UDP: 数据
UDP_PONG = 3                # UDP: 服务端回执

UDP_VER = 2                 # 包头版本：v2 起带 HMAC，密钥不再明文上路
UDP_MAC_LEN = 8             # 截断到 8 字节，够用又不占带宽
UDP_TS_WINDOW = 300         # 注册包时间戳允许的偏移（秒），防重放
MAC_REG = b"mclink-reg"
MAC_DATA = b"mclink-data"

DEFAULT_CFG = {
    "bind": "0.0.0.0",
    "control_port": 7000,
    "data_port": 7000,          # 与控制端口相同 = 复用同一 TCP 端口
    "udp_port": 7000,           # UDP 命名空间独立，可与 TCP 同号
    "tokens": ["CHANGE-ME-PLEASE"],
    "public_ip": "",
    "allowed_ports": [[25565, 25600], [19132, 19133], [7777, 7800], [10999, 11000]],
    "max_conns_per_mapping": 256,
    "dial_timeout_s": 8,
    "idle_timeout_s": 300,
    "heartbeat_s": 20,

    # ---- 授权 / 分发 ----
    # admin_token 留空表示还没设；设了之后客户端用它可以进"管理员模式"
    "admin_token": "",
    # 是否要求客户端必须激活才能用（分发出去时改成 true）
    "license_required": False,
    # 授权数据存哪；留空 = 脚本同目录的 licenses.json
    "license_file": "",

    # ---- 客户端自动更新 ----
    # 更新清单放哪；留空 = 脚本同目录的 updates/manifest.json
    # 用 tools/publish_update.ps1 发布一个客户端包，它会自动写好清单
    "update_manifest": "",

    # ---- 传输加密 ----
    # true = 控制通道和数据通道都走 TLS（强烈建议开，密钥/邀请码就不会明文上网）
    "tls": True,
    "tls_cert": "",      # 留空 = 同目录 server.crt（不存在会自动 openssl 自签）
    "tls_key": "",       # 留空 = 同目录 server.key

    "log_level": "info",
    "log_file": "",
}


def now() -> float:
    return time.time()


# ---------------------------------------------------------------- 日志

class Logger:
    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, level: str = "info", path: str = ""):
        self.level = self.LEVELS.get(str(level).lower(), 20)
        self._fh = None
        if path:
            try:
                d = os.path.dirname(os.path.abspath(path))
                if d:
                    os.makedirs(d, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as exc:
                print(f"[WARN ] 无法打开日志文件 {path}: {exc}")

    def log(self, level: str, msg: str) -> None:
        if self.LEVELS.get(level, 20) < self.level:
            return
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level.upper():5}] {msg}"
        print(line, flush=True)
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass

    def debug(self, m): self.log("debug", m)
    def info(self, m): self.log("info", m)
    def warn(self, m): self.log("warn", m)
    def error(self, m): self.log("error", m)


LOG = Logger()


# ---------------------------------------------------------------- 控制消息收发

async def send_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    """发送一条 长度前缀(4字节大端) + JSON 的控制消息。"""
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()


async def read_msg(reader: asyncio.StreamReader, timeout: float | None = None) -> dict:
    """读取一条控制消息；timeout 为 None 表示不限时。"""
    if timeout:
        hdr = await asyncio.wait_for(reader.readexactly(4), timeout)
    else:
        hdr = await reader.readexactly(4)
    (n,) = struct.unpack("!I", hdr)
    if n > MAX_JSON:
        raise ValueError(f"控制消息过大: {n}")
    if timeout:
        body = await asyncio.wait_for(reader.readexactly(n), timeout)
    else:
        body = await reader.readexactly(n)
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- UDP 包格式
# REG : MAGIC(2) | 0x01 | TOKEN_LEN(1) | TUNNEL_ID(2) | TOKEN(n)
# DATA: MAGIC(2) | 0x02 | FAMILY(1)   | TUNNEL_ID(2) | ADDR(4/16) | PORT(2) | PAYLOAD
# PONG: MAGIC(2) | 0x03 | 0x00        | TUNNEL_ID(2)
# 包头固定开销 6 字节 + 地址 6/18 字节。

def _mac(key: bytes, tag: bytes, *parts: bytes) -> bytes:
    h = hmac.new(key, digestmod=hashlib.sha256)
    h.update(tag)
    for p in parts:
        h.update(p)
    return h.digest()[:UDP_MAC_LEN]


def pack_udp_reg(tunnel_id: int, token: str, ts: int | None = None) -> bytes:
    """注册/保活包：带时间戳 + HMAC。**密钥本身不再出现在网络上。**"""
    key = token.encode("utf-8")
    ts = int(ts if ts is not None else time.time()) & 0xFFFFFFFF
    tail = struct.pack("!HI", tunnel_id, ts)
    return MAGIC + bytes([UDP_REG, UDP_VER]) + tail + _mac(key, MAC_REG, tail)


def pack_udp_pong(tunnel_id: int, token: str, ts: int | None = None) -> bytes:
    key = token.encode("utf-8")
    ts = int(ts if ts is not None else time.time()) & 0xFFFFFFFF
    tail = struct.pack("!HI", tunnel_id, ts)
    return MAGIC + bytes([UDP_PONG, UDP_VER]) + tail + _mac(key, MAC_REG, tail)


def pack_udp_data(tunnel_id: int, addr, payload: bytes, token: str) -> bytes:
    ip, port = addr[0], addr[1]
    try:
        packed = socket.inet_pton(socket.AF_INET, ip)
        fam = 4
    except OSError:
        packed = socket.inet_pton(socket.AF_INET6, ip)
        fam = 6
    body = (struct.pack("!HB", tunnel_id, fam) + packed
            + struct.pack("!H", port) + payload)
    mac = _mac(token.encode("utf-8"), MAC_DATA, body)
    # 布局: MAGIC(2) | DATA(1) | FAM(1) | TUNNEL_ID(2) | MAC(8) | ADDR | PORT | PAYLOAD
    # 注意 body 前 3 字节是 tid+fam，fam 已经在包头里了，所以要跳过 3 字节而不是 2
    return MAGIC + bytes([UDP_DATA, fam]) + body[:2] + mac + body[3:]


def parse_udp_data(data: bytes, token: str):
    """校验并解析 DATA 包 -> (tunnel_id, addr, payload)；MAC 不对就返回 None。"""
    if len(data) < 6 + UDP_MAC_LEN or data[:2] != MAGIC or data[2] != UDP_DATA:
        return None
    fam = data[3]
    if fam not in (4, 6):
        return None
    (tid,) = struct.unpack_from("!H", data, 4)
    mac = data[6:6 + UDP_MAC_LEN]
    rest = data[6 + UDP_MAC_LEN:]
    body = struct.pack("!HB", tid, fam) + rest
    if not hmac.compare_digest(_mac(token.encode("utf-8"), MAC_DATA, body), mac):
        return None
    alen = 4 if fam == 4 else 16
    if len(rest) < alen + 2:
        return None
    try:
        ip = socket.inet_ntop(socket.AF_INET if fam == 4 else socket.AF_INET6,
                              rest[:alen])
    except OSError:
        return None
    (port,) = struct.unpack_from("!H", rest, alen)
    return tid, (ip, port), rest[alen + 2:]


def parse_udp_reg(data: bytes, token: str):
    """校验注册包 -> (tunnel_id, ts)；不合法返回 None。"""
    if len(data) < 6 + UDP_MAC_LEN or data[:2] != MAGIC or data[2] != UDP_REG:
        return None
    if data[3] != UDP_VER:
        return None
    tid, ts = struct.unpack_from("!HI", data, 4)
    mac = data[10:10 + UDP_MAC_LEN]
    if not hmac.compare_digest(
            _mac(token.encode("utf-8"), MAC_REG, struct.pack("!HI", tid, ts)), mac):
        return None
    return tid, ts


# ---------------------------------------------------------------- 数据结构

class Mapping:
    """服务端侧的一个端口映射。"""

    def __init__(self, client: "ClientSession", mid: str, name: str,
                 proto: str, remote_port: int):
        self.client = client
        self.mid = mid
        self.name = name
        self.proto = proto              # "tcp" | "udp"
        self.remote_port = remote_port
        self.tunnel_id = 0              # UDP 隧道标识（服务端分配）
        self.tcp_server: asyncio.Server | None = None
        self.udp_transport = None
        self.udp_sessions: dict = {}    # 玩家地址 -> 最后活跃时间
        self.rx = 0                     # 玩家 -> 本地游戏
        self.tx = 0                     # 本地游戏 -> 玩家
        self.active = 0
        self.total = 0
        self.status = "pending"         # pending | active | error
        self.error = None
        # UDP 专用：只有确认过"玩家包真的穿过隧道了"才置 True。
        # 光看 udp_tunnels 里有没有登记还不够 —— 登记先到、客户端本地转发
        # 就绪在后，中间那一小段玩家发的包仍然会被丢掉。
        self.udp_verified = False

    def snap(self) -> dict:
        status, error = self.status, self.error
        if self.proto == "udp":
            cutoff = now() - 120
            for k in [k for k, v in self.udp_sessions.items() if v < cutoff]:
                self.udp_sessions.pop(k, None)
            self.active = len(self.udp_sessions)
            # UDP 有个"端口已监听但还不能转发"的窗口：公网端口注册成功时就算
            # active 了，但真正能转发要等客户端的隧道登记 + 本地转发就绪。
            # 这中间玩家发的包会被丢掉（见 PublicUdpProtocol.datagram_received
            # 的 t is None 分支）。所以**收到过至少一个玩家包之后**才报 active，
            # 让界面和自动化测试都能"等它真的能用"，而不是看到一个假的 active。
            if status == "active" and not self.udp_verified:
                status = "pending"
        return {
            "mid": self.mid, "name": self.name, "proto": self.proto,
            "remote_port": self.remote_port, "tunnel_id": self.tunnel_id,
            "rx": self.rx, "tx": self.tx, "active": self.active,
            "total": self.total, "status": status, "error": error,
        }

    def close(self) -> None:
        if self.tcp_server is not None:
            self.tcp_server.close()
            self.tcp_server = None
        if self.udp_transport is not None:
            try:
                self.udp_transport.close()
            except Exception:
                pass
            self.udp_transport = None


class ClientSession:
    """一个已通过鉴权的客户端连接。"""

    _seq = 0

    def __init__(self, server: "McLinkServer", writer: asyncio.StreamWriter,
                 token: str, name: str, version: str, peer):
        ClientSession._seq += 1
        self.cid = f"c{ClientSession._seq}"
        self.server = server
        self.writer = writer
        self.token = token
        self.name = name or "unknown"
        self.version = version or "?"
        self.peer = peer
        self.mappings: dict[str, Mapping] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.udp_addr = None            # 客户端 UDP 隧道源地址
        self.connected_at = now()
        self.alive = True
        # ---- 授权状态 ----
        self.licensed = False           # 是否已通过激活校验
        self.is_admin = False           # 是否已用管理员密钥解锁
        self.username = None            # 绑定的用户名
        # ---- 客户端自报的身份（管理员在控制台里看得到） ----
        self.nickname = name or "unknown"   # 用户自己填的昵称，缺省用机器名
        self.bound_at = None

    async def send(self, obj: dict) -> bool:
        if not self.alive:
            return False
        try:
            await send_msg(self.writer, obj)
            return True
        except Exception:
            return False

    def close(self, reason: str = "") -> None:
        if not self.alive:
            return
        self.alive = False
        self.server.log.info(f"客户端断开 {self.cid} ({self.peer[0]}) {reason}".rstrip())
        for m in list(self.mappings.values()):
            self.server.drop_mapping(m)
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("客户端已断开"))
        self.pending.clear()
        try:
            self.writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------- UDP 协议实现

class UdpTunnelProtocol(asyncio.DatagramProtocol):
    """服务端 udp_port 上的隧道端点：接收客户端的上行包，转发给玩家。"""

    def __init__(self, server: "McLinkServer"):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.server.udp_transport = transport

    def error_received(self, exc):  # Windows 上 UDP 会有连接重置噪声，忽略
        self.server.log.debug(f"UDP 隧道 error_received: {exc!r}")

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 4 or data[:2] != MAGIC:
            return
        typ = data[2]
        if typ == UDP_REG:
            if data[3] != UDP_VER or len(data) < 6 + UDP_MAC_LEN:
                return
            (tid,) = struct.unpack_from("!H", data, 4)
            # 先按 tunnel_id 找到映射，再用它自己的密钥验 MAC（密钥不上网，攻击者伪造不了）
            m = self.server.mapping_by_tunnel.get(tid)
            if m is None:
                self.server.log.debug(f"UDP 注册：未知 tunnel_id={tid}")
                return
            parsed = parse_udp_reg(data, m.client.token)
            if parsed is None:
                self.server.log.debug(f"UDP 注册被拒绝（MAC 不对）tunnel_id={tid}")
                return
            if abs(time.time() - parsed[1]) > UDP_TS_WINDOW:
                self.server.log.debug(f"UDP 注册时间戳超出窗口 tunnel_id={tid}")
                return
            first = m.client.udp_addr is None
            m.client.udp_addr = addr
            self.server.udp_tunnels[tid] = {"addr": addr, "ts": now()}
            if first:
                self.server.log.info(
                    f"UDP 隧道就绪 [{m.client.cid}/{m.mid}] {tid} <- {addr[0]}:{addr[1]}")
            if self.transport:
                self.transport.sendto(pack_udp_pong(tid, m.client.token), addr)
        elif typ == UDP_DATA:
            if len(data) < 6 + UDP_MAC_LEN:
                return
            (tid,) = struct.unpack_from("!H", data, 4)
            m = self.server.mapping_by_tunnel.get(tid)
            if m is None or m.udp_transport is None:
                return
            parsed = parse_udp_data(data, m.client.token)
            if not parsed:
                self.server.log.debug(f"UDP 数据包 MAC 校验失败 tunnel_id={tid}")
                return
            _, paddr, payload = parsed
            m.tx += len(payload)
            try:
                m.udp_transport.sendto(payload, paddr)
            except OSError as exc:
                self.server.log.debug(f"UDP 回发玩家失败 {paddr}: {exc}")


class PublicUdpProtocol(asyncio.DatagramProtocol):
    """某个 UDP 映射在公网端口上的监听端点：收玩家包，封包头后送给客户端。"""

    def __init__(self, server: "McLinkServer", mapping: Mapping):
        self.server = server
        self.mapping = mapping
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.mapping.udp_transport = transport

    def error_received(self, exc):
        self.server.log.debug(f"公网 UDP {self.mapping.remote_port} error: {exc!r}")

    def datagram_received(self, data: bytes, addr) -> None:
        m = self.mapping
        t = self.server.udp_tunnels.get(m.tunnel_id)
        if t is None or self.server.udp_transport is None:
            if m.status != "error":
                m.status = "error"
                m.error = "客户端 UDP 隧道未就绪（等待客户端保活包）"
            return
        if m.status == "error":
            m.status = "active"
            m.error = None
        m.rx += len(data)
        if addr not in m.udp_sessions:
            m.total += 1
        m.udp_sessions[addr] = now()
        try:
            self.server.udp_transport.sendto(
                pack_udp_data(m.tunnel_id, addr, data, m.client.token), t["addr"])
            # 隧道登记 + 公网收包都成功，从这一刻起这条 UDP 映射才算真的能用
            # （snap() 靠它把 pending 变成 active）
            m.udp_verified = True
        except OSError as exc:
            self.server.log.debug(f"UDP 送客户端失败: {exc}")


# ---------------------------------------------------------------- 服务端主体

class McLinkServer:
    def __init__(self, cfg: dict):
        self.log = LOG          # 由 amain() 在创建实例前重新赋值的全局日志器
        self.cfg = {**DEFAULT_CFG, **cfg}
        self.bind = self.cfg["bind"]
        self.control_port = int(self.cfg["control_port"])
        # data_port 为 0 或缺省 = 复用控制端口；也允许显式写成与 control_port 相同
        self.data_port = int(self.cfg.get("data_port") or 0) or self.control_port
        self.udp_port = int(self.cfg["udp_port"])
        self.shared_data_port = (self.data_port == self.control_port)
        self.tokens = set(self.cfg.get("tokens") or [])
        self.allowed = [tuple(p) for p in self.cfg.get("allowed_ports") or []]
        self.max_conns = int(self.cfg["max_conns_per_mapping"])
        self.dial_timeout = float(self.cfg["dial_timeout_s"])
        self.idle_timeout = float(self.cfg["idle_timeout_s"])
        self.heartbeat = float(self.cfg["heartbeat_s"])

        self.clients: dict[str, ClientSession] = {}
        self.port_map: dict[tuple, Mapping] = {}     # (proto, remote_port) -> Mapping
        self.mapping_by_tunnel: dict[int, Mapping] = {}
        self.udp_tunnels: dict[int, dict] = {}
        self._tid = 0

        self.control_srv = None
        self.data_srv = None
        self.udp_transport = None
        self.public_ip = self.cfg.get("public_ip") or detect_public_ip()
        self.started_at = now()
        self._tasks: list[asyncio.Task] = []

        # ---- 授权 ----
        self.admin_token = str(self.cfg.get("admin_token") or "")
        lic_path = self.cfg.get("license_file") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "licenses.json")
        self.license = LicenseStore(lic_path, bool(self.cfg.get("license_required")),
                                    self.log)
        self.limiter = RateLimiter()

        # ---- 传输加密 ----
        self.tls = bool(self.cfg.get("tls", True))
        self.ssl_ctx = None
        self.cert_fp = ""
        if self.tls:
            here = os.path.dirname(os.path.abspath(__file__))
            cert = self.cfg.get("tls_cert") or os.path.join(here, "server.crt")
            key = self.cfg.get("tls_key") or os.path.join(here, "server.key")
            if ensure_cert(cert, key, self.log):
                try:
                    self.ssl_ctx = make_ssl_context(cert, key)
                    self.cert_fp = cert_fingerprint(cert)
                except Exception as exc:             # noqa: BLE001
                    self.log.warn(f"加载证书失败（{exc}）—— 退回明文模式")
                    self.ssl_ctx = None
            if self.ssl_ctx is None:
                self.tls = False

    # -------------------------------------------------- 工具

    def next_tunnel_id(self) -> int:
        for _ in range(65535):
            self._tid = self._tid % 65535 + 1
            if self._tid not in self.mapping_by_tunnel:
                return self._tid
        raise RuntimeError("隧道 ID 耗尽")

    def port_allowed(self, port: int) -> bool:
        if not self.allowed:
            return True
        return any(lo <= port <= hi for lo, hi in self.allowed)

    def drop_mapping(self, m: Mapping) -> None:
        key = (m.proto, m.remote_port)
        if self.port_map.get(key) is m:
            self.port_map.pop(key, None)
        self.mapping_by_tunnel.pop(m.tunnel_id, None)
        self.udp_tunnels.pop(m.tunnel_id, None)
        m.close()
        self.log.info(f"移除映射 {m.client.cid}/{m.mid} {m.proto}:{m.remote_port}")

    # -------------------------------------------------- 启动

    async def start(self) -> None:
        if not self.tokens or self.tokens == {"CHANGE-ME-PLEASE"}:
            self.log.warn("配置里的 tokens 仍是默认值，请务必改成随机密钥（--gen-token）！")

        self.control_srv = await asyncio.start_server(
            self.handle_control, self.bind, self.control_port, ssl=self.ssl_ctx)
        # data_port 与 control_port 相同时，数据通道复用控制端口（只需放行一个 TCP 端口）
        self.shared_data_port = (self.data_port == self.control_port)
        if not self.shared_data_port:
            self.data_srv = await asyncio.start_server(
                self.handle_data, self.bind, self.data_port, ssl=self.ssl_ctx)
        await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: UdpTunnelProtocol(self), local_addr=(self.bind, self.udp_port))

        self.log.info(f"McLink 服务端 v{VERSION} 已启动")
        self.log.info(f"  控制通道  {self.bind}:{self.control_port}/tcp")
        if self.shared_data_port:
            self.log.info(f"  TCP 数据  复用 {self.control_port}/tcp（只需放行这一个 TCP 端口）")
        else:
            self.log.info(f"  TCP 数据  {self.bind}:{self.data_port}/tcp")
        self.log.info(f"  UDP 隧道  {self.bind}:{self.udp_port}/udp"
                      + ("（与 TCP 同号，防火墙按 UDP 再放行一次即可）"
                         if self.udp_port == self.control_port else ""))
        self.log.info(f"  公网 IP   {self.public_ip or '未探测到（将由客户端使用其配置里的 server.host）'}")
        if not self.public_ip:
            self.log.warn("未能自动探测公网 IP。这通常发生在：服务器用的是弹性公网 IP(EIP)，"
                          "或元数据服务被禁用。")
            self.log.warn("不影响使用 —— 客户端会改用 config.client.json 里的 server.host 作为连接地址。"
                          "若显示不对，请在 config.server.json 里手动填写 public_ip。")
        self.log.info(f"  允许端口  {self.allowed or '全部（不推荐）'}")
        if self.ssl_ctx is not None:
            self.log.info("  传输加密  TLS 已开启（TLS 1.2+，密钥与邀请码不再明文上网）")
            if self.cert_fp:
                self.log.info(f"  证书指纹  {format_fingerprint(self.cert_fp)}")
                self.log.info("            客户端第一次连接会自动记住；"
                              "想更严格可以把它填进客户端的 cert_fingerprint")
        else:
            self.log.warn("  传输加密  未开启 —— 明文传输，密钥可能被同一网络的人看到")
        if self.license.required:
            st = self.license.stats()
            self.log.info(f"  授权校验  已开启（{st['users']} 个用户，"
                          f"{st['invites_pending']} 个待用邀请码）")
            if not self.admin_token:
                self.log.warn("授权校验已开启，但 admin_token 是空的！"
                              "你可能会把自己也锁在外面，请立刻配置管理员密钥。")
        else:
            self.log.warn("  授权校验  未开启 —— 谁拿到客户端都能用，"
                          "不需要任何密钥！要分发给别人就用管理员控制台把它打开"
                          "（或把 config.server.json 的 license_required 改成 true 再重启）。")
        # ---- 自动更新：启动时说清楚"现在有没有可下发的更新包" ----
        try:
            entries = []
            for plat in ("windows", "linux", "macos"):
                e = self._update_entry(plat)
                if e:
                    entries.append(f"{plat} {e.get('version')}"
                                   f"（{int(e.get('size') or 0)} 字节）")
            if entries:
                self.log.info("  客户端更新  " + "、".join(entries))
            else:
                self.log.info("  客户端更新  没有已发布的更新包"
                              "（要发布就用 tools/publish_update.ps1）")
        except Exception as exc:                     # noqa: BLE001
            self.log.warn(f"  客户端更新  清单读取异常：{exc!r}")
        self._tasks.append(asyncio.create_task(self._stats_loop()))
        self._tasks.append(asyncio.create_task(self._reaper_loop()))

    async def stop(self) -> None:
        self.log.info("正在关闭…")
        for t in self._tasks:
            t.cancel()
        try:
            self.license.flush()
        except Exception:
            pass
        for c in list(self.clients.values()):
            await c.send({"t": "bye", "reason": "服务端关闭"})
            c.close("服务端关闭")
        for srv in (self.control_srv, self.data_srv):
            if srv:
                srv.close()
                try:
                    await srv.wait_closed()
                except Exception:
                    pass
        if self.udp_transport:
            self.udp_transport.close()

    # -------------------------------------------------- 控制通道

    async def handle_control(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            msg = await read_msg(reader, 15)
        except Exception:
            writer.close()
            return

        if msg.get("t") == "mux":
            # 数据通道复用控制端口：同一种"长度前缀 + JSON"握手，看首条消息类型分流
            return await self._handle_mux(msg, reader, writer)
        if msg.get("t") != "hello":
            await send_msg(writer, {"t": "error", "error": "首条消息必须是 hello"})
            writer.close()
            return

        token = str(msg.get("token") or "")
        if token not in self.tokens:
            self.log.warn(f"鉴权失败，来自 {peer[0]}:{peer[1]}")
            await send_msg(writer, {"t": "error", "error": "密钥无效，请检查 config.client.json 里的 token"})
            writer.close()
            return

        c = ClientSession(self, writer, token, msg.get("name"), msg.get("version"), peer)
        self.clients[c.cid] = c
        nick = str(msg.get("nickname") or "").strip()[:32]
        if nick:
            c.nickname = nick
        self.log.info(f"客户端接入 {c.cid} {c.name} v{c.version} 来自 {peer[0]}:{peer[1]}"
                      + (f" 昵称={c.nickname}" if nick else ""))

        # ---- 授权判定 ----
        # 管理员密钥直接放行（防止管理员把自己锁在门外），其次看设备令牌
        admin_key = str(msg.get("admin_token") or "")
        if self.admin_token and admin_key and hmac.compare_digest(admin_key, self.admin_token):
            c.is_admin = True
        lic = msg.get("license") or {}
        if c.is_admin:
            c.licensed = True
            c.username = c.username or "admin"
            lic_state = {"username": c.username, "status": "active", "reason": None}
        elif not self.license.required:
            c.licensed = True
            lic_state = {"username": None, "status": "active", "reason": None}
        else:
            name, status, reason = self.license.check_device(
                str(lic.get("device_token") or ""),
                str(lic.get("fingerprint") or ""),
                peer[0] if peer else None)
            c.licensed = reason is None
            c.username = name
            lic_state = {"username": name, "status": status, "reason": reason}
        if self.license.required and not c.licensed:
            self.log.info(f"  └ 未授权：{lic_state.get('reason')}")
        # 把客户端自报的昵称 / 版本 / IP 记进授权库，管理员在控制台「客户端」页能看到
        if c.licensed and c.username:
            try:
                u = self.license.users.get(c.username)
                if u is not None:
                    u["nickname"] = c.nickname
                    u["client_version"] = c.version
                    u["machine"] = c.name
                    u["last_ip"] = peer[0] if peer else u.get("last_ip")
                    self.license.touch()
                    c.bound_at = u.get("bound_at")
            except Exception as exc:                 # noqa: BLE001
                self.log.debug(f"记录客户端信息失败（忽略）：{exc!r}")

        await send_msg(writer, {
            "t": "hello_ok", "cid": c.cid, "version": VERSION,
            "public_ip": self.public_ip, "allowed_ports": self.allowed,
            "control_port": self.control_port, "udp_port": self.udp_port,
            "data_port": self.effective_data_port(),
            "licensed": c.licensed, "is_admin": c.is_admin,
            "admin_configured": bool(self.admin_token),
            "tls": self.ssl_ctx is not None,
            "cert_fingerprint": self.cert_fp,
            "license": {**lic_state, "required": self.license.required},
            "ts": now(),
        })

        try:
            while c.alive:
                msg = await read_msg(reader, self.heartbeat * 3)
                t = msg.get("t")
                if t == "ping":
                    await c.send({"t": "pong", "ts": msg.get("ts")})
                elif t == "register":
                    await self.cmd_register(c, msg)
                elif t == "unregister":
                    await self.cmd_unregister(c, msg)
                elif t == "get_info":
                    await c.send({"t": "info", "info": self.info()})
                elif t == "license_activate":
                    await self.cmd_license_activate(c, msg, peer)
                elif t == "hello_meta":
                    await self.cmd_hello_meta(c, msg, peer)
                elif t == "update_check":
                    await self.cmd_update_check(c, msg)
                elif t == "update_fetch":
                    await self.cmd_update_fetch(c, msg)
                elif t == "admin_auth":
                    await self.cmd_admin_auth(c, msg)
                elif t == "admin":
                    await self.cmd_admin(c, msg)
                elif t == "bye":
                    break
                else:
                    self.log.debug(f"未知控制消息: {t}")
        except asyncio.IncompleteReadError:
            c.close("对端关闭")
        except asyncio.TimeoutError:
            c.close("心跳超时")
        except Exception as exc:
            c.close(f"异常 {exc!r}")
        finally:
            self.clients.pop(c.cid, None)
            if c.alive:
                c.close("连接结束")

    # -------------------------------------------------- 控制指令

    async def cmd_register(self, c: ClientSession, msg: dict) -> None:
        spec = msg.get("mapping") or {}
        mid = str(spec.get("mid") or "")
        proto = str(spec.get("proto") or "tcp").lower()
        try:
            remote_port = int(spec.get("remote_port"))
        except (TypeError, ValueError):
            remote_port = 0
        name = str(spec.get("name") or mid)

        async def fail(err: str) -> None:
            self.log.warn(f"注册失败 {c.cid}/{mid}: {err}")
            await c.send({"t": "register_err", "mid": mid, "error": err})

        # 未授权不允许开任何公网端口（这是分发的核心闸门，纵深防御的最后一道）
        if self.license.required and not c.licensed:
            return await fail("客户端未授权：请先在 McLink 里用一次性密钥激活")

        if not mid:
            return await fail("缺少映射 ID")
        if proto not in ("tcp", "udp"):
            return await fail("协议只能是 tcp 或 udp")
        if not (1 <= remote_port <= 65535):
            return await fail("公网端口非法")
        if remote_port in (self.control_port, self.data_port, self.udp_port):
            return await fail(f"端口 {remote_port} 被 McLink 自身占用")
        if not self.port_allowed(remote_port):
            return await fail(f"端口 {remote_port} 不在服务端允许范围内")

        key = (proto, remote_port)
        old = self.port_map.get(key)
        if old is not None and old.client is not c:
            return await fail(f"{proto.upper()} 端口 {remote_port} 已被其他客户端占用")

        # 同一个 mid 重复注册 -> 先清理旧的
        prev = c.mappings.get(mid)
        if prev is not None:
            self.drop_mapping(prev)
            c.mappings.pop(mid, None)

        m = Mapping(c, mid, name, proto, remote_port)
        m.tunnel_id = self.next_tunnel_id()
        try:
            if proto == "tcp":
                m.tcp_server = await asyncio.start_server(
                    lambda r, w, mm=m: self.handle_public_tcp(mm, r, w),
                    self.bind, remote_port)
            else:
                await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda mm=m: PublicUdpProtocol(self, mm),
                    local_addr=(self.bind, remote_port))
                m.status = "active"
        except OSError as exc:
            m.close()
            return await fail(f"公网端口 {remote_port} 监听失败：{exc.strerror or exc}")

        c.mappings[mid] = m
        self.port_map[key] = m
        self.mapping_by_tunnel[m.tunnel_id] = m
        self.log.info(f"注册成功 {c.cid}/{mid} {name} -> {proto}:{remote_port} "
                      f"(tunnel={m.tunnel_id})")
        await c.send({"t": "register_ok", "mid": mid,
                      "tunnel_id": m.tunnel_id, "remote_port": remote_port})

    async def cmd_unregister(self, c: ClientSession, msg: dict) -> None:
        mid = str(msg.get("mid") or "")
        m = c.mappings.pop(mid, None)
        if m:
            self.drop_mapping(m)
        await c.send({"t": "unregister_ok", "mid": mid})

    # -------------------------------------------------- 客户端身份

    async def cmd_hello_meta(self, c: ClientSession, msg: dict, peer) -> None:
        """客户端上报/更新自己的昵称。管理员在控制台看得到。"""
        nick = str(msg.get("nickname") or "").strip()[:32]
        if nick:
            c.nickname = nick
        ver = str(msg.get("version") or "").strip()[:24]
        if ver:
            c.version = ver
        u = self.license.users.get(c.username) if c.username else None
        if u is not None:
            if nick:
                u["nickname"] = nick
            if ver:
                u["client_version"] = ver
            u["machine"] = c.name
            if peer:
                u["last_ip"] = peer[0]
            self.license.touch()
            self.log.info(f"客户端 {c.cid} 昵称更新为「{c.nickname}」")
        await c.send({"t": "hello_meta_ok", "nickname": c.nickname})

    # -------------------------------------------------- 自动更新

    def _update_manifest(self) -> dict:
        """读更新清单。文件不存在就返回一个"没有更新"的空清单。"""
        path = self.cfg.get("update_manifest") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "updates", "manifest.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("packages", {})
                return data
        except FileNotFoundError:
            return {"version": VERSION, "release": "", "notes": "", "packages": {}}
        except Exception as exc:                     # noqa: BLE001
            self.log.warn(f"更新清单读取失败（当作没有更新）: {exc!r}")
        return {"version": VERSION, "release": "", "notes": "", "packages": {}}

    @staticmethod
    def _ver_key(version: str, release: str = "") -> tuple:
        """把 "1.2.0" + "20261005" 转成可比较的元组。"""
        nums = []
        for part in str(version or "").split("."):
            try:
                nums.append(int(part))
            except ValueError:
                nums.append(0)
        while len(nums) < 3:
            nums.append(0)
        rel = str(release or "")
        rel_num = int(rel) if rel.isdigit() else 0
        return (rel_num, nums[0], nums[1], nums[2])

    def _update_entry(self, platform: str) -> dict | None:
        man = self._update_manifest()
        entry = (man.get("packages") or {}).get(platform)
        if not isinstance(entry, dict):
            return None
        fname = os.path.basename(str(entry.get("file") or ""))
        if not fname:
            return None
        base = os.path.dirname(self.cfg.get("update_manifest") or
                               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "updates", "manifest.json"))
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            self.log.warn(f"更新清单里有 {fname}，但文件不在 {base}")
            return None
        out = dict(entry)
        out["file"] = fname
        out["path"] = path
        out["version"] = str(man.get("version") or "")
        out["release"] = str(man.get("release") or "")
        out["notes"] = str(man.get("notes") or "")
        out["size"] = int(entry.get("size") or os.path.getsize(path))
        return out

    async def cmd_update_check(self, c: ClientSession, msg: dict) -> None:
        """回「有没有新版本」。只读，不鉴权 —— 升级前也得让老客户端问得出来。"""
        cur_v = str(msg.get("version") or "")
        cur_r = str(msg.get("release") or "")
        platform = str(msg.get("os") or "windows").lower()
        if platform not in ("windows", "linux", "macos"):
            platform = "windows"
        entry = self._update_entry(platform)
        base = {"t": "update_info", "current": cur_v or VERSION,
                "latest": None, "available": False, "notes": "", "size": 0,
                "sha256": "", "release": ""}
        if entry is None:
            await c.send(base)
            return
        newer = self._ver_key(entry.get("version"), entry.get("release")) > \
            self._ver_key(cur_v, cur_r)
        payload = dict(base)
        payload.update({
            "latest": entry.get("version"),
            "release": entry.get("release"),
            "notes": entry.get("notes"),
            "size": entry.get("size"),
            "sha256": entry.get("sha256") or "",
            "available": bool(newer and entry.get("sha256")),
        })
        if newer:
            self.log.info(f"客户端 {c.cid}（v{cur_v}）可用更新："
                          f"{entry.get('version')}（{entry.get('size')} 字节）")
        await c.send(payload)

    async def cmd_update_fetch(self, c: ClientSession, msg: dict) -> None:
        """分块下发更新包。走的是同一条加密控制通道，不用另开端口。"""
        entry = self._update_entry(str(msg.get("os") or "windows").lower())
        if entry is None:
            await c.send({"t": "update_err", "error": "服务端没有可用的更新包"})
            return
        try:
            offset = max(0, int(msg.get("offset") or 0))
            chunk = int(msg.get("chunk") or 128 * 1024)
        except (TypeError, ValueError):
            await c.send({"t": "update_err", "error": "下载参数不合法"})
            return
        chunk = max(4096, min(chunk, 256 * 1024))
        try:
            with open(entry["path"], "rb") as fh:
                fh.seek(offset)
                data = fh.read(chunk)
        except OSError as exc:
            await c.send({"t": "update_err", "error": f"读取更新包失败：{exc}"})
            return
        end = offset + len(data)
        total = int(entry.get("size") or 0)
        await c.send({
            "t": "update_chunk",
            "offset": offset,
            "size": total,
            "data": base64.b64encode(data).decode("ascii"),
            "done": bool(total and end >= total) or not data,
        })

    # -------------------------------------------------- 授权相关指令

    async def cmd_license_activate(self, c: ClientSession, msg: dict, peer) -> None:
        ip = peer[0] if peer else "?"
        if not self.license.required:
            await c.send({"t": "license_err",
                          "error": "服务端没有开启授权校验，不需要激活"})
            return
        if self.limiter.blocked("act:" + ip):
            await c.send({"t": "license_err",
                          "error": "尝试次数过多，请等一分钟再试"})
            return
        result, err = self.license.activate(
            str(msg.get("code") or ""),
            str(msg.get("fingerprint") or ""),
            str(msg.get("device_name") or ""))
        if err:
            self.limiter.fail("act:" + ip)
            self.log.warn(f"激活失败（{ip}）: {err}")
            await c.send({"t": "license_err", "error": err})
            return
        self.limiter.clear("act:" + ip)
        c.licensed = True
        c.username = result["username"]
        await c.send({"t": "license_ok", "device_token": result["device_token"],
                      "username": result["username"]})
        await c.send({"t": "license_state", "licensed": True,
                      "license": {"username": result["username"],
                                  "status": "active", "reason": None,
                                  "required": True}})

    def _client_versions(self) -> dict:
        """统计在线客户端的版本分布，管理员一眼看出谁还没升级。"""
        out: dict[str, int] = {}
        for c in self.clients.values():
            if c.alive:
                out[c.version] = out.get(c.version, 0) + 1
        return out

    async def cmd_admin_auth(self, c: ClientSession, msg: dict) -> None:
        if not self.admin_token:
            await c.send({"t": "admin_auth_err",
                          "error": "服务端还没有配置管理员密钥"})
            return
        ip = c.peer[0] if c.peer else "?"
        key = str(msg.get("admin_token") or "")
        ok = bool(key) and hmac.compare_digest(key, self.admin_token)
        if not ok:
            if self.limiter.blocked("adm:" + ip):
                await c.send({"t": "admin_auth_err",
                              "error": "尝试次数过多，请等一分钟再试"})
                return
            self.limiter.fail("adm:" + ip)
            self.log.warn(f"管理员密钥错误（来自 {ip}）")
            await c.send({"t": "admin_auth_err", "error": "管理员密钥不正确"})
            return
        self.limiter.clear("adm:" + ip)
        c.is_admin = True
        c.licensed = True
        c.username = c.username or "admin"
        self.log.info(f"客户端 {c.cid} 进入管理员模式")
        await c.send({"t": "admin_auth_ok", "licensed": True})

    async def cmd_admin(self, c: ClientSession, msg: dict) -> None:
        req = msg.get("req")
        action = str(msg.get("action") or "")
        args = msg.get("args") or {}

        async def ok(data=None, **extra):
            payload = {"t": "admin_ok", "req": req, "action": action,
                       "data": data if data is not None else {}}
            payload.update(extra)
            await c.send(payload)

        async def bad(err: str):
            await c.send({"t": "admin_err", "req": req, "error": err})

        if not c.is_admin:
            return await bad("没有管理员权限，请先输入管理员密钥")
        if not self.admin_token:
            return await bad("服务端还没有配置管理员密钥")

        L = self.license
        if action == "invite_create":
            rec, err = L.create_invite(args.get("username"),
                                       args.get("hours", 24),
                                       args.get("note", ""))
            if err:
                return await bad(err)
            return await ok({"code": rec["code"], "username": rec["username"],
                             "expires_at": rec["expires_at"],
                             "masked": mask_code(rec["code"])})
        if action == "invite_list":
            # include_history=true 时把"用户已删除"的历史记录也带出来
            # （记录一直都在，默认只是不显示）
            hist = bool(args.get("include_history"))
            return await ok({"invites": L.list_invites(include_orphans=hist),
                             "history_count": len(L.orphan_invites()),
                             "include_history": hist})
        if action == "invite_revoke":
            done, err = L.revoke_invite(str(args.get("code") or ""))
            return await ok() if done else await bad(err)
        if action == "user_list":
            return await ok({"users": L.list_users()})
        if action == "client_list":
            # 在线客户端 + 授权库里记的身份信息，合成一张给管理员看的表
            users = {u.get("username"): u for u in L.list_users()}
            # 注意：这里**不能**用 c 当循环变量。cmd_admin 的 c 是本连接，
            # 而 ok()/bad() 是闭包，用的是作用域里的 c —— 一旦被循环改写，
            # 回包就会发到最后一个客户端身上（踩过一次，找了很久）。
            online = {}
            for sess in self.clients.values():
                if sess.alive and sess.username:
                    online[sess.username] = sess
            rows = []
            seen = set()
            for name, u in users.items():
                sess = online.get(name)
                seen.add(name)
                rows.append({
                    "username": name,
                    "nickname": ((sess.nickname if sess else None)
                                 or u.get("nickname") or "—"),
                    "status": u.get("status"),
                    "online": bool(sess),
                    "invite": (u.get("invite_masked")
                               or (mask_code(u["invite_code"])
                                   if u.get("invite_code") else "—")),
                    "ip": ((sess.peer[0] if (sess and sess.peer) else None)
                           or u.get("last_ip") or "—"),
                    "version": ((sess.version if sess else None)
                                or u.get("client_version") or "—"),
                    "machine": u.get("machine") or u.get("device_name") or "—",
                    "bound_at": u.get("bound_at"),
                    "last_seen": u.get("last_seen"),
                    "note": u.get("note") or "—",
                })
            # 只在线、还没进授权库的（比如管理员自己）也列出来
            for name, sess in online.items():
                if name in seen:
                    continue
                rows.append({
                    "username": name, "nickname": sess.nickname,
                    "status": "admin" if sess.is_admin else "active",
                    "online": True,
                    "invite": "—",
                    "ip": sess.peer[0] if sess.peer else "—",
                    "version": sess.version,
                    "machine": sess.name,
                    "bound_at": None, "last_seen": int(now()),
                    "note": "管理员" if sess.is_admin else "—",
                })
            rows.sort(key=lambda r: (not r["online"], r["username"] or ""))
            return await ok({"clients": rows, "online": len(online)})
        if action == "user_set_status":
            done, err = L.set_status(str(args.get("username") or ""),
                                     str(args.get("status") or ""))
            return await ok() if done else await bad(err)
        if action == "user_unbind":
            done, err = L.unbind(str(args.get("username") or ""))
            return await ok() if done else await bad(err)
        if action == "user_delete":
            # 删除用户会连他名下的邀请码一起清掉，detail 里告诉界面清了几张
            done, err, detail = L.delete_user(str(args.get("username") or ""))
            return await ok(detail or {}) if done else await bad(err)
        if action == "stats":
            return await ok(L.stats())
        if action == "update_status":
            # 管理员控制台「更新」页：现在下发的更新包是什么
            man = self._update_manifest()
            pkgs = {}
            for plat in ("windows", "linux", "macos"):
                e = self._update_entry(plat)
                if e:
                    pkgs[plat] = {"version": e.get("version"),
                                  "release": e.get("release"),
                                  "size": e.get("size"),
                                  "sha256": (e.get("sha256") or "")[:16],
                                  "file": e.get("file")}
            return await ok({"version": man.get("version") or VERSION,
                             "release": man.get("release") or "",
                             "notes": man.get("notes") or "",
                             "packages": pkgs,
                             "clients": self._client_versions()})
        if action == "set_required":
            val = bool(args.get("required"))
            if val and not self.admin_token:
                return await bad("开启授权校验前必须先配置管理员密钥，否则你会把自己锁在外面")
            if not val and not args.get("confirm_off"):
                # 关掉它 = 谁拿到客户端都能用（不需要密钥）。这是分发时最要命的
                # 一个误操作，所以要求界面明确带一个确认标记才放行。
                return await bad("关闭授权校验会让所有拿到客户端的人都不用密钥就能用。"
                                 "确实要关，请再确认一次。")
            L.set_required(val)
            self.log.warn(f"授权校验已被管理员{'开启' if val else '关闭'}")
            if not val:
                self.log.warn("  └ 注意：现在任何人都能用这个客户端，不需要密钥！")
            return await ok({"required": L.required})
        return await bad(f"未知的管理动作：{action}")

    # -------------------------------------------------- TCP 数据通道

    async def handle_public_tcp(self, m: Mapping, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter) -> None:
        c = m.client
        peer = writer.get_extra_info("peername") or ("?", 0)
        if not c.alive:
            writer.close()
            return
        if m.active >= self.max_conns:
            self.log.warn(f"映射 {m.mid} 连接数超限，拒绝 {peer[0]}")
            writer.close()
            return

        tok = secrets.token_hex(16)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        c.pending[tok] = fut
        if not await c.send({"t": "dial", "tok": tok, "mid": m.mid, "proto": "tcp"}):
            c.pending.pop(tok, None)
            writer.close()
            return

        try:
            d_reader, d_writer = await asyncio.wait_for(fut, self.dial_timeout)
        except (asyncio.TimeoutError, ConnectionError, asyncio.CancelledError) as exc:
            c.pending.pop(tok, None)
            m.status = "error"
            m.error = "客户端回连超时，请检查客户端是否在线"
            self.log.warn(f"映射 {m.mid} 回连失败: {exc!r}")
            writer.close()
            return
        finally:
            c.pending.pop(tok, None)

        m.active += 1
        m.total += 1
        m.status = "active"
        m.error = None
        self.log.debug(f"转发 {m.mid} {peer[0]}:{peer[1]} <-> 客户端")
        try:
            await asyncio.gather(
                self._pipe(reader, d_writer, lambda n: setattr(m, "rx", m.rx + n)),
                self._pipe(d_reader, writer, lambda n: setattr(m, "tx", m.tx + n)),
                return_exceptions=True,
            )
        finally:
            m.active = max(0, m.active - 1)
            for w in (writer, d_writer):
                try:
                    w.close()
                except Exception:
                    pass

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, on_bytes) -> None:
        try:
            while True:
                data = await asyncio.wait_for(reader.read(BUF_SIZE), self.idle_timeout)
                if not data:
                    break
                on_bytes(len(data))
                writer.write(data)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.write_eof()
            except Exception:
                pass

    async def handle_data(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        """独立数据端口上的回连（仅在 data_port != control_port 时启用）。"""
        try:
            msg = await read_msg(reader, 10)
        except Exception:
            writer.close()
            return
        await self._handle_mux(msg, reader, writer)

    async def _handle_mux(self, msg: dict, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        """把客户端回连的数据连接，与之前挂起的公网连接配对。"""
        if msg.get("t") != "mux":
            writer.close()
            return
        tok = str(msg.get("tok") or "")
        # 在所有客户端里找到持有该 token 的挂起请求
        for c in self.clients.values():
            fut = c.pending.get(tok)
            if fut is not None and not fut.done():
                fut.set_result((reader, writer))
                return
        self.log.debug("收到未知 mux 连接，已拒绝")
        writer.close()

    def effective_data_port(self) -> int:
        """对外通告的数据端口（复用控制端口时就是控制端口）。"""
        return self.control_port if self.data_port == self.control_port else self.data_port

    # -------------------------------------------------- 周期任务

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            for c in list(self.clients.values()):
                if not c.alive:
                    continue
                data = {m.mid: m.snap() for m in c.mappings.values()}
                await c.send({"t": "stats", "data": data})

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            # 清理长时间没有保活的 UDP 隧道登记
            cutoff = now() - 150
            for tid in [k for k, v in self.udp_tunnels.items() if v["ts"] < cutoff]:
                self.udp_tunnels.pop(tid, None)
            # 授权数据落盘 + 限流表清理 + 过期邀请码回收
            try:
                self.license.flush()
                self.license.purge_expired()
                self.limiter.sweep()
            except Exception as exc:                 # noqa: BLE001
                self.log.debug(f"授权维护任务出错: {exc!r}")

    # -------------------------------------------------- 信息

    def info(self) -> dict:
        return {
            "version": VERSION,
            "public_ip": self.public_ip,
            "allowed_ports": self.allowed,
            "control_port": self.control_port,
            "data_port": self.effective_data_port(),
            "udp_port": self.udp_port,
            "shared_data_port": self.data_port == self.control_port,
            "tls": self.ssl_ctx is not None,
            "cert_fingerprint": self.cert_fp,
            "os": sys.platform,
            "uptime_s": int(now() - self.started_at),
            "clients": len(self.clients),
            "mappings": len(self.port_map),
        }


# ---------------------------------------------------------------- 辅助

def is_public_ip(ip: str) -> bool:
    """判断是不是真正可以在公网上被人连到的地址。"""
    if not ip:
        return False
    try:
        parts = [int(x) for x in ip.split(".")]
    except ValueError:
        return False          # 不是 IPv4（如 IPv6），保守起见当作不可用
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return False
    a, b = parts[0], parts[1]
    if a in (0, 10, 127):
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    if a == 100 and 64 <= b <= 127:      # 运营商级 NAT / 阿里云内网
        return False
    if a >= 224:                          # 组播 / 保留
        return False
    return True


# ---------------------------------------------------------------- TLS 辅助

def _openssl_works(path: str) -> bool:
    """这个 openssl 到底能不能跑起来？

    光 os.path.exists() 不够 —— 踩过一次：装了 Git for Windows 之后
    `Git\\usr\\bin\\openssl.exe` 存在，但在受限环境（沙箱 / 权限收紧）里
    msys2 程序起不来，直接报
        "fatal error - couldn't create signal pipe, Win32 error 5"。
    这时 find_openssl() 会返回一个"存在但没用"的路径：调用方以为能签证书，
    实际签名失败、静默退回明文；测试里则一直等证书出现直到超时。

    所以这里真的跑一次 `openssl version` 验证 —— **能出结果才算找到**。
    """
    try:
        r = subprocess.run([path, "version"], capture_output=True, timeout=8)
        out = (r.stdout or b"") + (r.stderr or b"")
        return r.returncode == 0 and b"OpenSSL" in out
    except Exception:                                # noqa: BLE001
        return False


def find_openssl() -> str | None:
    """找一个**能真正运行**的 openssl。

    除了 PATH，还会翻几个 Windows 上常见的安装位置
    （装了 Git for Windows / Strawberry Perl 之类都会自带）。
    每个候选都实跑一次验证（见 _openssl_works），跑不起来就跳过。
    """
    import shutil as _shutil
    cands = []
    p = _shutil.which("openssl")
    if p:
        cands.append(p)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        cands += [
            os.path.join(base, "Git", "usr", "bin", "openssl.exe"),
            os.path.join(base, "Git", "mingw64", "bin", "openssl.exe"),
            os.path.join(base, "OpenSSL-Win64", "bin", "openssl.exe"),
            os.path.join(base, "OpenSSL", "bin", "openssl.exe"),
            os.path.join(base, "Programs", "Git", "usr", "bin", "openssl.exe"),
            os.path.join(base, "chocolatey", "bin", "openssl.exe"),
        ]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.exists(c) and _openssl_works(c):
            return c
    return None


def ensure_cert(cert_path: str, key_path: str, log) -> bool:
    """证书不存在就用 openssl 自签一张（10 年）。失败返回 False，调用方退回明文。"""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return True
    d = os.path.dirname(os.path.abspath(cert_path))
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    openssl = find_openssl()
    if not openssl:
        log.warn("系统里找不到 openssl，没法自动生成自签证书 —— 暂时退回明文模式。")
        log.warn("  Linux:  sudo dnf install -y openssl   或   sudo apt install -y openssl")
        log.warn("  Windows: 装个 Git for Windows 就有（会自动去常见目录找）")
        log.warn("  也可以自己准备一对证书，在配置里填 tls_cert / tls_key 的路径。")
        return False

    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path,
        "-days", "3650", "-subj", "/CN=McLink",
        "-addext", "subjectAltName=DNS:mclink,IP:127.0.0.1",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except Exception as exc:                          # noqa: BLE001
        # 老版本 openssl 不认 -addext，去掉再试一次
        try:
            subprocess.run([c for c in cmd if c != "-addext"
                            and not c.startswith("subjectAltName")],
                           check=True, capture_output=True, timeout=120)
        except Exception as exc2:                     # noqa: BLE001
            log.warn(f"生成证书失败（{exc2 or exc}）—— 退回明文模式")
            return False
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    log.info(f"已生成自签证书：{cert_path}")
    return True


def cert_fingerprint(cert_path: str) -> str:
    """算出证书的 SHA-256 指纹（十六进制小写），跟客户端 getpeercert 的结果一致。"""
    try:
        with open(cert_path, "rb") as fh:
            pem = fh.read()
        m = re.search(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----",
                      pem, re.S)
        if not m:
            return ""
        der = base64.b64decode(re.sub(rb"\s+", b"", m.group(1)))
        return hashlib.sha256(der).hexdigest()
    except Exception:                                 # noqa: BLE001
        return ""


def format_fingerprint(fp: str) -> str:
    """转成 xx:xx:xx… 的形式，方便人眼比对。"""
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def make_ssl_context(cert_path: str, key_path: str):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def detect_public_ip() -> str | None:
    """探测本机对外的公网 IP。

    顺序很重要：
      1. 阿里云元数据服务 —— ECS 绑了弹性公网 IP(EIP) 时，网卡上只有内网地址，
         只有元数据服务能拿到真正的 EIP。这是阿里云上最可靠的方式。
      2. UDP connect 技巧 —— 拿到本机出口 IP，适合 IP 直接绑在网卡上的情况。
    两者都拿不到公网地址时返回 None，此时客户端会用 config.client.json 里
    配置的 server.host 作为连接地址（那本来就是你填的公网 IP）。
    """
    # 1) 阿里云 ECS 元数据服务（仅阿里云内网可达，2 秒超时，失败无副作用）
    for path in ("eipv4", "public-ipv4"):
        try:
            import urllib.request
            url = f"http://100.100.100.200/latest/meta-data/{path}"
            with urllib.request.urlopen(url, timeout=2) as resp:
                ip = resp.read(64).decode("utf-8", "ignore").strip()
            if is_public_ip(ip):
                LOG.info(f"从阿里云元数据服务获取到公网 IP: {ip}")
                return ip
        except Exception:
            pass

    # 2) UDP 出口技巧
    for probe in (("8.8.8.8", 80), ("223.5.5.5", 80), ("114.114.114.114", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(probe)
            ip = s.getsockname()[0]
            s.close()
            if is_public_ip(ip):
                return ip
        except OSError:
            continue
    return None


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 入口

async def amain(cfg: dict) -> None:
    global LOG
    LOG = Logger(cfg.get("log_level", "info"), cfg.get("log_file", ""))
    server = McLinkServer(cfg)
    await server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError, ValueError):
            pass  # Windows 不支持，靠 KeyboardInterrupt
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    await server.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="McLink 端口映射服务端")
    ap.add_argument("-c", "--config", default="config.server.json")
    ap.add_argument("--gen-token", action="store_true", help="生成一个随机密钥后退出")
    ap.add_argument("--cert-fingerprint", action="store_true",
                    help="打印证书指纹（填到客户端的 cert_fingerprint 里）")
    ap.add_argument("--check-config", action="store_true", help="检查配置文件后退出")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()

    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return
    if args.version:
        print(f"McLink server {VERSION}")
        return

    cfg = load_config(args.config)
    if args.cert_fingerprint:
        here = os.path.dirname(os.path.abspath(args.config))
        cert = cfg.get("tls_cert") or os.path.join(here, "server.crt")
        fp = cert_fingerprint(cert)
        if not fp:
            print(f"读不到证书：{cert}（服务端跑起来后会自动生成）", file=sys.stderr)
            sys.exit(1)
        print(format_fingerprint(fp))
        print(fp)
        return
    if args.check_config:
        merged = {**DEFAULT_CFG, **cfg}
        print(f"配置 {args.config} 解析正常。")
        print(f"  控制端口 {merged['control_port']} / 数据端口 {merged['data_port']} "
              f"/ UDP {merged['udp_port']}")
        print(f"  允许端口 {merged['allowed_ports']}")
        print(f"  密钥数量 {len(merged.get('tokens') or [])}")
        if not merged.get("tokens") or merged["tokens"] == ["CHANGE-ME-PLEASE"]:
            print("  [警告] 仍在用默认密钥，请执行: python3 mclink_server.py --gen-token")
        return

    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

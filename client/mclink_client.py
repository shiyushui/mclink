#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 客户端  (mclink_client.py)
=================================
运行在玩游戏的那台 Windows 电脑上（Linux 也能跑）。
纯 Python 3 标准库实现，无需 pip install 任何东西。

它做四件事：
  1. 主动连接服务端的控制通道，注册端口映射，保持心跳，断线自动重连。
  2. 收到服务端 dial 指令时，主动回连 TCP 数据通道，并把流量转发到本地游戏端口。
  3. 维护一条 UDP 隧道 socket，把玩家发来的 UDP 包转给本地游戏，并把回包送回去。
  4. 在本机 127.0.0.1:8787 提供一个 HTTP + SSE 接口，供 web/index.html 控制台使用。

用法:
    python mclink_client.py                 # 用同目录的 config.client.json
    python mclink_client.py -c my.json
    python mclink_client.py --web-port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import traceback
import uuid
from urllib.parse import urlsplit, parse_qs

VERSION = "1.2.8"
# 发布批次（年月日）。自动更新比 VERSION 先比 release，再比版本号 ——
# 这样同一天改两版也能分辨出来。version.json 里是权威值，这里只是兜底。
RELEASE = "20261005"
# 每次向服务端要多少字节的更新包（base64 之后约 1.33 倍，
# 要明显小于协议里单条消息的上限，留足余量）
UPDATE_CHUNK = 128 * 1024

# 客户端自己的"客户端"段：昵称（用户自己填，管理员能看到）、是否自动检查更新
DEFAULT_CLIENT_SECTION = {
    "nickname": "",             # 留空则用机器名
    "auto_update": True,        # 连上服务端后自动问一次有没有新版本
    "update_interval_min": 30,  # 自动检查的间隔（分钟），0 = 只在启动时查一次
    # false = 客户端启动后**不自动开启映射**，要用户点一下卡片开关
    # （或点「启动全部映射」）才开始转发。默认 false，避免"一开软件端口就对外开着"。
    "auto_start_mappings": False,
}

MAX_JSON = 1 << 20
BUF_SIZE = 65536
MAGIC = b"ML"
UDP_REG = 1
UDP_DATA = 2
UDP_PONG = 3

# UDP 包头 v2：带 HMAC，主密钥不再明文上路
UDP_VER = 2
UDP_MAC_LEN = 8
MAC_REG = b"mclink-reg"
MAC_DATA = b"mclink-data"

UDP_KEEPALIVE_S = 20        # 客户端 UDP 保活间隔
UDP_SESSION_IDLE_S = 90     # 玩家 UDP 会话空闲回收
MAX_UDP_SESSIONS = 64       # 每个映射最多同时保留的玩家会话

DEFAULT_CFG = {
    "server": {
        "host": "127.0.0.1",
        "control_port": 7000,
        "data_port": 7000,
        "udp_port": 7000,
        "token": "CHANGE-ME-PLEASE",
        "admin_token": "",
        "tls": True,
        "cert_fingerprint": "",
    },
    "web": {"host": "127.0.0.1", "port": 8787, "token": ""},
    "client": dict(DEFAULT_CLIENT_SECTION),
    "mappings": [],
    "log_level": "info",
    "log_file": "",
}

HERE = os.path.dirname(os.path.abspath(__file__))


def now() -> float:
    return time.time()


def device_fingerprint() -> str:
    """算一个稳定的"这台电脑是谁"的指纹，用于把授权绑死在一台机器上。

    用 Windows 安装时生成的 MachineGuid 为主（重装系统才会变），
    再加上机器名和网卡 MAC 兜底。目的是挡住"把整个配置文件夹拷到
    另一台电脑上白嫖"，不是对抗有心破解的人（那需要服务端心跳+行为检测）。
    """
    parts = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            parts.append(str(winreg.QueryValueEx(k, "MachineGuid")[0]))
    except Exception:
        pass
    try:
        parts.append(platform.node())
    except Exception:
        pass
    try:
        parts.append(hex(uuid.getnode()))
    except Exception:
        pass
    parts.append(platform.system())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 日志总线

class LogBus:
    """日志同时输出到控制台、文件，并广播给所有 SSE 订阅者。"""

    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, level: str = "info", path: str = "", keep: int = 500):
        self.level = self.LEVELS.get(str(level).lower(), 20)
        self.history: list[dict] = []
        self.keep = keep
        self.subs: set[asyncio.Queue] = set()
        self._fh = None
        if path:
            try:
                d = os.path.dirname(os.path.abspath(path))
                if d:
                    os.makedirs(d, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as exc:
                print(f"[warn] 无法打开日志文件 {path}: {exc}")

    def log(self, level: str, msg: str) -> None:
        if self.LEVELS.get(level, 20) < self.level:
            return
        rec = {"ts": int(now()), "level": level, "msg": msg}
        self.history.append(rec)
        if len(self.history) > self.keep:
            del self.history[:len(self.history) - self.keep]
        line = f"{time.strftime('%H:%M:%S')} [{level.upper():5}] {msg}"
        print(line, flush=True)
        if self._fh:
            try:
                self._fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level.upper():5}] {msg}\n")
                self._fh.flush()
            except OSError:
                pass
        for q in list(self.subs):
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def debug(self, m): self.log("debug", m)
    def info(self, m): self.log("info", m)
    def warn(self, m): self.log("warn", m)
    def error(self, m): self.log("error", m)


LOG = LogBus()

# ---------------------------------------------------------------- 控制消息

async def send_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()


async def read_msg(reader: asyncio.StreamReader, timeout: float | None = None) -> dict:
    if timeout:
        hdr = await asyncio.wait_for(reader.readexactly(4), timeout)
    else:
        hdr = await reader.readexactly(4)
    (n,) = struct.unpack("!I", hdr)
    if n > MAX_JSON:
        raise ValueError("控制消息过大")
    if timeout:
        body = await asyncio.wait_for(reader.readexactly(n), timeout)
    else:
        body = await reader.readexactly(n)
    return json.loads(body.decode("utf-8"))


def _mac(key: bytes, tag: bytes, *parts: bytes) -> bytes:
    h = hmac.new(key, digestmod=hashlib.sha256)
    h.update(tag)
    for p in parts:
        h.update(p)
    return h.digest()[:UDP_MAC_LEN]


def pack_udp_reg(tunnel_id: int, token: str, ts: int | None = None) -> bytes:
    key = token.encode("utf-8")
    ts = int(ts if ts is not None else time.time()) & 0xFFFFFFFF
    tail = struct.pack("!HI", tunnel_id, ts)
    return MAGIC + bytes([UDP_REG, UDP_VER]) + tail + _mac(key, MAC_REG, tail)


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
    # body 前 3 字节是 tid+fam，fam 已经单独放进包头了，这里必须跳过 3 字节
    return MAGIC + bytes([UDP_DATA, fam]) + body[:2] + mac + body[3:]


def parse_udp_data(data: bytes, token: str):
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
    """校验注册/保活包 -> (tunnel_id, ts)。客户端一般用不到，
    保留它是为了让两端的协议实现完全对称，也方便测试。"""
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


def parse_udp_pong(data: bytes, token: str):
    if len(data) < 6 + UDP_MAC_LEN or data[:2] != MAGIC or data[2] != UDP_PONG:
        return None
    if data[3] != UDP_VER:
        return None
    tid, ts = struct.unpack_from("!HI", data, 4)
    mac = data[10:10 + UDP_MAC_LEN]
    if not hmac.compare_digest(
            _mac(token.encode("utf-8"), MAC_REG, struct.pack("!HI", tid, ts)), mac):
        return None
    return tid, ts


def make_client_ssl_context():
    """TLS 客户端上下文。

    自签证书没有可信 CA 能验，所以关掉 CA 校验、改用**证书指纹固定**：
    第一次连接记住指纹，之后每次都必须一致 —— 这样中间人换不了证书。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ---------------------------------------------------------------- 映射

class Mapping:
    def __init__(self, spec: dict):
        self.id = str(spec.get("id") or ("m" + secrets.token_hex(4)))
        self.name = str(spec.get("name") or "未命名映射")
        self.proto = str(spec.get("proto") or "tcp").lower()
        self.local_host = str(spec.get("local_host") or "127.0.0.1")
        self.local_port = int(spec.get("local_port") or 0)
        self.remote_port = int(spec.get("remote_port") or 0)
        self.enabled = bool(spec.get("enabled", True))
        # 运行时状态（不写回配置文件）
        self.tunnel_id = 0
        self.status = "inactive"
        self.error = None            # 服务端/协议上报的错误
        self.local_error = None      # 本地游戏端口导致的错误
        self.local_error_ts = 0.0
        self.retry_at = 0.0          # 下次允许重新注册的时间
        self.rx = 0
        self.tx = 0
        self.rx_rate = 0.0
        self.tx_rate = 0.0
        self.active_conns = 0
        self.total_conns = 0
        self._last_rx = 0
        self._last_tx = 0
        self._last_t = now()
        # 本次运行是否允许自动注册。
        # client.auto_start_mappings = true 时等于 enabled；
        # 关掉（默认）时启动后**不自动开映射**，要用户点一下卡片开关
        # （或点「启动全部映射」）才 set_enabled 把它放行。
        self._start_gate = False

    def gate(self, on: bool) -> None:
        """放行/拦住这条映射的注册（用户显式操作时调用）。"""
        self._start_gate = bool(on)

    def mark_local_error(self, msg: str) -> None:
        if self.local_error != msg:
            self.local_error = msg
        self.local_error_ts = now()

    def clear_local_error(self) -> None:
        self.local_error = None
        self.local_error_ts = 0.0

    def effective(self) -> tuple[str, str | None]:
        """对外的最终状态：本地端口错误优先且 5 分钟内有效。"""
        if not self.enabled:
            return "inactive", None
        if not self._start_gate:
            # 配置里是启用的，但本次运行还没被放行（启动后不自动开映射）。
            # 对外报 "inactive"，界面卡片显示"已停用"，点一下开关就放行。
            return "inactive", None
        if self.local_error and now() - self.local_error_ts < 300:
            return "error", self.local_error
        if self.error:
            return "error", self.error
        return self.status, None

    def to_spec(self) -> dict:
        return {
            "id": self.id, "name": self.name, "proto": self.proto,
            "local_host": self.local_host, "local_port": self.local_port,
            "remote_port": self.remote_port, "enabled": self.enabled,
        }

    def apply(self, spec: dict) -> None:
        self.name = str(spec.get("name") or self.name)
        self.proto = str(spec.get("proto") or self.proto).lower()
        self.local_host = str(spec.get("local_host") or self.local_host)
        self.local_port = int(spec.get("local_port") or self.local_port)
        self.remote_port = int(spec.get("remote_port") or self.remote_port)
        self.enabled = bool(spec.get("enabled", self.enabled))

    def snapshot(self, public_ip=None) -> dict:
        addr = f"{public_ip}:{self.remote_port}" if public_ip else None
        status, error = self.effective()
        return {
            "id": self.id, "name": self.name, "proto": self.proto,
            "local_host": self.local_host, "local_port": self.local_port,
            "remote_port": self.remote_port, "enabled": self.enabled,
            "tunnel_id": self.tunnel_id,          # UDP 隧道编号，排查问题时有用
            "status": status, "error": error,
            "rx_bytes": self.rx, "tx_bytes": self.tx,
            "rx_rate": round(self.rx_rate, 1), "tx_rate": round(self.tx_rate, 1),
            "active_conns": self.active_conns, "total_conns": self.total_conns,
            "connect_addr": addr,
        }


# ---------------------------------------------------------------- UDP 本地会话

class LocalUdpSession(asyncio.DatagramProtocol):
    """一个玩家对应一个本地 UDP socket（connect 到游戏端口），
    这样从游戏回包的 socket 就能确定该回给哪个玩家。"""

    def __init__(self, agent: "Agent", mapping: Mapping, player_addr):
        self.agent = agent
        self.mapping = mapping
        self.player_addr = player_addr
        self.transport = None
        self.pending: list[bytes] = []   # socket 建好之前先缓存首包
        self.last = now()
        self.closing = False

    def connection_made(self, transport):
        self.transport = transport
        self.mapping.clear_local_error()
        # 补发建 socket 期间到达的包（首包通常是 Bedrock 的 Unconnected Ping）
        while self.pending:
            try:
                transport.sendto(self.pending.pop(0))
            except OSError:
                break

    def datagram_received(self, data: bytes, addr):
        self.last = now()
        self.mapping.clear_local_error()
        self.agent.udp_send_up(self.mapping, self.player_addr, data)

    def error_received(self, exc):
        # Windows 上本地 UDP 端口没人监听会返回 WSAECONNRESET
        self.mapping.mark_local_error(
            f"本地 {self.mapping.local_host}:{self.mapping.local_port} "
            f"UDP 无响应，请确认游戏服务端已启动")
        self.agent.log.warn(f"[{self.mapping.name}] {self.mapping.local_error}")

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass


class UdpTunnelProtocol(asyncio.DatagramProtocol):
    """客户端与服务端 udp_port 之间的隧道 socket。"""

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.agent.udp_transport = transport

    def error_received(self, exc):
        self.agent.log.debug(f"UDP 隧道 error_received: {exc!r}")

    def datagram_received(self, data: bytes, addr):
        token = self.agent.srv.get("token", "")
        if len(data) < 5 or data[:2] != MAGIC:
            return
        if data[2] == UDP_PONG:
            parsed = parse_udp_pong(data, token)
            if not parsed:
                return                            # MAC 不对，可能有人在伪造
            m = self.agent.tunnel_index.get(parsed[0])
            if m:
                m.status = "active"
                m.error = None
            return
        parsed = parse_udp_data(data, token)
        if not parsed:
            self.agent.log.debug("收到 MAC 校验失败的 UDP 包，已丢弃")
            return
        tid, player_addr, payload = parsed
        m = self.agent.tunnel_index.get(tid)
        if m is None or not m.enabled:
            return
        self.agent.udp_deliver_local(m, player_addr, payload)


# ---------------------------------------------------------------- 客户端主体

class Agent:
    def __init__(self, cfg: dict, config_path: str):
        self.cfg = cfg
        self.config_path = config_path
        self.srv = cfg["server"]
        # 数据通道端口：以服务端握手时告知的为准，缺省与控制端口相同（复用同一端口）
        self.data_port = int(self.srv.get("data_port") or 0) or int(self.srv["control_port"])

        # ---- 传输加密 ----
        self.tls = bool(self.srv.get("tls", True))
        self.ssl_ctx = make_client_ssl_context() if self.tls else None
        self.cert_fp = str(self.srv.get("cert_fingerprint") or "").strip().lower()
        self.cert_fp = self.cert_fp.replace(":", "").replace(" ", "")
        self.mappings: dict[str, Mapping] = {}
        # 启动时要不要自动开启映射。默认 **不自动开** ——
        # 否则"打开软件"就等于"把端口对外开着"，用户没点过也生效。
        self.auto_start = bool(self.client_cfg.get("auto_start_mappings", False))
        for spec in cfg.get("mappings") or []:
            m = Mapping(spec)
            # 放行条件：配置里显式要求自动开启。否则等用户操作。
            m.gate(self.auto_start and m.enabled)
            self.mappings[m.id] = m

        self.tunnel_index: dict[int, Mapping] = {}
        self.udp_sessions: dict[tuple, LocalUdpSession] = {}
        self.udp_transport = None
        self.udp_socket_addr = None

        self.control_writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.public_ip = None
        self.server_info: dict = {}
        self.latency_ms = None
        self.last_error: str | None = None
        self.reconnect_in_s: float | None = None

        self.started_at = now()
        self.stats = {"rx_bytes": 0, "tx_bytes": 0}
        self._stat_t = now()
        self._stat_rx = 0
        self._stat_tx = 0
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._sessions_dirty = True
        self._udp_dirty = True
        self._dials = 0

        # ---- 授权 ----
        self.license_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), "license.json")
        self.lic = self._load_license()
        self.fingerprint = device_fingerprint()
        self.admin_key = str(self.srv.get("admin_token") or "")
        self._pending: dict = {}
        self._req_seq = 0
        self.license_state = {
            "licensed": False, "is_admin": False, "username": None,
            "status": None, "reason": "尚未连接", "required": False,
            "admin_configured": False,
        }
        # ---- 客户端身份（昵称由用户自己填，管理员那边能看到） ----
        self.cfg.setdefault("client", dict(DEFAULT_CLIENT_SECTION))
        self._update_buf = bytearray()
        self.update_state: dict = {
            "current": VERSION, "latest": None, "available": False,
            "notes": "", "size": 0, "checked_at": None, "error": None,
            "downloading": False, "progress": 0.0, "ready": None, "applying": False,
        }

    # ------------------------------------------------ 客户端身份 / 昵称

    @property
    def client_cfg(self) -> dict:
        """配置里的 "client" 段，缺字段时补上默认值。"""
        sec = self.cfg.setdefault("client", {})
        for k, v in DEFAULT_CLIENT_SECTION.items():
            sec.setdefault(k, v)
        return sec

    def nickname(self) -> str:
        """要显示给管理员看的昵称。没填就用机器名。"""
        nick = str(self.client_cfg.get("nickname") or "").strip()
        if not nick:
            nick = socket.gethostname()
        return nick[:32]

    def set_nickname(self, nick: str) -> str:
        """保存昵称并立刻告诉服务端（下次心跳也会带上）。"""
        nick = (nick or "").strip()[:32]
        self.client_cfg["nickname"] = nick
        self.save_config()
        return self.nickname()

    async def push_nickname(self) -> None:
        """把当前昵称主动上报一次。没连接就等下次握手。"""
        if not self.connected or self.control_writer is None:
            return
        try:
            await send_msg(self.control_writer,
                           {"t": "hello_meta", "nickname": self.nickname(),
                            "version": VERSION})
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------ 授权：本地存取

    def _load_license(self) -> dict:
        try:
            with open(self.license_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_license(self) -> None:
        try:
            tmp = self.license_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.lic, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.license_path)
        except OSError as exc:
            self.log.warn(f"授权信息保存失败：{exc}")

    def license_info(self) -> dict:
        st = dict(self.license_state)
        st["admin_key_saved"] = bool(self.admin_key)
        st["device_token"] = self.lic.get("device_token")
        st["username"] = st.get("username") or self.lic.get("username")
        st["nickname"] = self.nickname()
        st["client_version"] = VERSION
        return st

    def update_info(self) -> dict:
        return dict(self.update_state)

    def _check_peer_cert(self, writer) -> None:
        """证书指纹固定（TOFU）。

        第一次连接把服务端证书指纹记进配置，之后每次都必须一致。
        这样即使有人在链路上做中间人，也因为拿不到同一张证书而立刻暴露。
        """
        if self.ssl_ctx is None:
            return
        try:
            obj = writer.get_extra_info("ssl_object")
            der = obj.getpeercert(binary_form=True) if obj is not None else None
        except Exception:                            # noqa: BLE001
            der = None
        if not der:
            raise RuntimeError("服务器没有提供 TLS 证书")
        fp = hashlib.sha256(der).hexdigest()
        if not self.cert_fp:
            self.cert_fp = fp
            self.cfg.setdefault("server", {})["cert_fingerprint"] = fp
            self.save_config()
            self.log.warn(f"首次连接，已记住服务器证书指纹 {fp[:16]}…"
                          f"（写进 config.client.json，之后必须一致）")
            return
        if not hmac.compare_digest(fp, self.cert_fp):
            raise RuntimeError(
                "服务器证书指纹和上次不一致！可能是服务器换了证书，"
                "也可能是有人在中间人劫持。确认是你自己换的证书的话，"
                "把 config.client.json 里的 cert_fingerprint 清空再连。")

    async def _open(self, host: str, port: int, timeout: float):
        """建立一条（可能加密的）TCP 连接，并校验服务端证书。"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=MAX_JSON, ssl=self.ssl_ctx),
                timeout)
        except ssl.SSLError as exc:
            raise RuntimeError(
                f"TLS 握手失败：{exc}。"
                f"{'服务端可能没开 tls（把客户端的 tls 改成 false 试试）' if self.tls else ''}")
        self._check_peer_cert(writer)
        return reader, writer

    # ------------------------------------------------ 生命周期

    async def run(self) -> None:
        self._tasks.append(asyncio.create_task(self._udp_loop()))
        self._tasks.append(asyncio.create_task(self._rate_loop()))
        self._tasks.append(asyncio.create_task(self._control_loop()))
        await self._stop.wait()

    async def shutdown(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self.control_writer:
            try:
                await send_msg(self.control_writer, {"t": "bye"})
            except Exception:
                pass
            try:
                self.control_writer.close()
            except Exception:
                pass
        for s in list(self.udp_sessions.values()):
            s.close()
        if self.udp_transport:
            self.udp_transport.close()

    # ------------------------------------------------ 配置读写

    def save_config(self) -> None:
        try:
            self.cfg["mappings"] = [m.to_spec() for m in self.mappings.values()]
            tmp = self.config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.config_path)
        except OSError as exc:
            self.log.warn(f"保存配置失败: {exc}")

    @property
    def log(self) -> LogBus:
        return LOG

    # ------------------------------------------------ 控制通道

    async def _control_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._control_session()
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.connected = False
                self.last_error = self._humanize(exc)
                self.log.warn(f"控制通道断开: {self.last_error}")
            self._on_disconnect()
            if self._stop.is_set():
                return
            self.reconnect_in_s = backoff
            for _ in range(int(backoff * 2)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(0.5)
                self.reconnect_in_s = max(0, self.reconnect_in_s - 0.5)
            self.reconnect_in_s = None
            backoff = min(backoff * 2, 30.0)

    def _humanize(self, exc: Exception) -> str:
        if isinstance(exc, ConnectionRefusedError):
            return "连接被拒绝（服务端未运行或端口不对）"
        if isinstance(exc, asyncio.TimeoutError):
            return "连接超时（检查阿里云安全组是否放行控制端口）"
        if isinstance(exc, socket.gaierror):
            return "无法解析服务器域名"
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (10060, 110):
            return "连接超时（检查阿里云安全组是否放行控制端口）"
        return f"{type(exc).__name__}: {exc}"

    async def _control_session(self) -> None:
        host = self.srv["host"]
        port = int(self.srv["control_port"])
        self.log.info(f"正在连接服务端 {host}:{port} …"
                      + ("  [TLS 加密]" if self.ssl_ctx else "  [明文]"))
        reader, writer = await self._open(host, port, 15)
        self.control_writer = writer

        await send_msg(writer, {
            "t": "hello", "token": self.srv.get("token", ""),
            "name": socket.gethostname(), "version": VERSION,
            "nickname": self.nickname(),
            "admin_token": self.admin_key or "",
            "license": {
                "device_token": self.lic.get("device_token") or "",
                "fingerprint": self.fingerprint,
                "device_name": socket.gethostname(),
            },
        })
        msg = await read_msg(reader, 12)
        if msg.get("t") == "error":
            writer.close()
            raise RuntimeError(msg.get("error") or "服务端拒绝连接")
        if msg.get("t") != "hello_ok":
            writer.close()
            raise RuntimeError(f"意外的握手响应: {msg.get('t')}")

        self.connected = True
        self.last_error = None
        self.public_ip = msg.get("public_ip") or self.public_ip or host
        self.data_port = int(msg.get("data_port") or 0) or self.data_port

        lic = msg.get("license") or {}
        self.license_state = {
            "licensed": bool(msg.get("licensed")),
            "is_admin": bool(msg.get("is_admin")),
            "username": lic.get("username") or self.lic.get("username"),
            "status": lic.get("status"),
            "reason": lic.get("reason"),
            "required": bool(lic.get("required")),
            "admin_configured": bool(msg.get("admin_configured")),
        }
        if self.license_state["required"]:
            if self.license_state["licensed"]:
                who = self.license_state["username"] or "管理员"
                self.log.info(f"授权校验通过（{who}）")
            else:
                self.log.warn(f"未授权：{self.license_state['reason']}")
        self.server_info = {
            "public_ip": msg.get("public_ip"), "allowed_ports": msg.get("allowed_ports"),
            "version": msg.get("version"), "os": "Linux",
            "control_port": msg.get("control_port"),
            "data_port": msg.get("data_port"),
            "udp_port": msg.get("udp_port"),
            "shared_data_port": msg.get("data_port") == msg.get("control_port"),
            "tls": bool(msg.get("tls")),
            "cert_fingerprint": self.cert_fp,
        }
        self.log.info(f"已连接服务端 (公网 IP: {self.public_ip})")

        # 重连后所有映射都要重新注册
        for m in self.mappings.values():
            m.tunnel_id = 0
            m.retry_at = 0.0
            m.status = "pending" if m.enabled else "inactive"
            m.error = None
        self._sessions_dirty = True
        self._udp_dirty = True

        ping_task = asyncio.create_task(self._ping_loop())
        update_task = asyncio.create_task(self._update_loop())
        try:
            while not self._stop.is_set():
                msg = await read_msg(reader, 60)
                await self._on_control(msg)
        finally:
            ping_task.cancel()
            update_task.cancel()
            self.connected = False
            try:
                writer.close()
            except Exception:
                pass
            self.control_writer = None

    async def _update_loop(self) -> None:
        """连上之后问一次有没有新版本，之后按配置的间隔再问。

        只检查、不自动装：发现新版本会在界面上弹一条提示，
        用户点「立即更新」才真的下载安装（下载会瞬断隧道，所以必须由用户点）。
        """
        sec = self.client_cfg
        if not sec.get("auto_update", True):
            return
        try:
            interval = int(sec.get("update_interval_min", 30) or 0)
        except (TypeError, ValueError):
            interval = 30
        while self.connected and not self._stop.is_set():
            if not self.update_state.get("downloading"):
                try:
                    await self.check_update()
                except Exception as exc:             # noqa: BLE001
                    self.log.debug(f"检查更新失败（不影响使用）：{exc}")
            if interval <= 0:
                return
            for _ in range(int(interval) * 4):       # 每 15 秒醒一次，方便退出
                if not self.connected or self._stop.is_set():
                    return
                await asyncio.sleep(15)

    def _on_disconnect(self) -> None:
        self.connected = False
        self.control_writer = None
        for key, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(ConnectionError("与服务端的连接已断开"))
        self._pending.clear()
        # 断开只是"暂时不知道授权状态"，不要把 is_admin 也抹掉：
        # 配置里存着管理员密钥的话，下一次握手服务端还会认你是管理员。
        # 抹掉会让管理员控制台在每次网络抖动后跳出"非管理员遮罩"，
        # 看起来就像"激活之后管理员什么都管不了了"。
        self.license_state.update({
            "licensed": False,
            "reason": "与服务器断开",
            "still_admin": bool(self.admin_key),
        })
        for m in self.mappings.values():
            m.tunnel_id = 0
            m.status = "error" if m.enabled else "inactive"
            if m.enabled:
                m.error = "与服务端断开"
            m.rx_rate = m.tx_rate = 0.0
        self.tunnel_index.clear()
        for s in list(self.udp_sessions.values()):
            s.close()
        self.udp_sessions.clear()

    async def _ping_loop(self) -> None:
        while self.connected and not self._stop.is_set():
            await asyncio.sleep(15)
            if not self.control_writer:
                return
            try:
                await send_msg(self.control_writer, {"t": "ping", "ts": now()})
            except Exception:
                return

    async def _on_control(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "dial":
            asyncio.create_task(self._do_dial(msg))
        elif t == "register_ok":
            mid = msg.get("mid")
            m = self.mappings.get(mid)
            if m:
                m.tunnel_id = int(msg.get("tunnel_id") or 0)
                m.retry_at = 0.0
                m.status = "active" if m.proto == "tcp" else "pending"
                m.error = None
                if m.proto == "udp":
                    self.tunnel_index[m.tunnel_id] = m
                    m.status = "active"
                    self._udp_register_now()
                self.log.info(f"映射已生效 [{m.name}] "
                              f"{m.proto.upper()} {self.public_ip}:{m.remote_port} "
                              f"-> {m.local_host}:{m.local_port}")
        elif t == "register_err":
            m = self.mappings.get(msg.get("mid"))
            if m:
                m.status = "error"
                m.error = msg.get("error") or "注册失败"
                m.retry_at = now() + 10.0   # 10 秒后再试，避免刷屏
                self.log.error(f"映射注册失败 [{m.name}]: {m.error}")
        elif t == "unregister_ok":
            pass
        elif t == "stats":
            self._merge_stats(msg.get("data") or {})
        elif t == "pong":
            ts = msg.get("ts")
            if ts:
                self.latency_ms = max(1, int((now() - ts) * 1000))
        elif t == "bye":
            raise ConnectionError(msg.get("reason") or "服务端主动断开")

        # ---------------- 授权相关 ----------------
        elif t == "license_ok":
            tok = msg.get("device_token")
            if tok:
                self.lic["device_token"] = tok
                self.lic["username"] = msg.get("username")
                self._save_license()
            self.license_state.update({"licensed": True, "status": "active",
                                       "reason": None,
                                       "username": msg.get("username")
                                       or self.license_state.get("username")})
            self.log.info(f"激活成功，用户名：{msg.get('username')}")
            self._sessions_dirty = True          # 让映射重新注册
            self._resolve("license", result=msg)
        elif t == "license_err":
            self.log.warn(f"激活失败：{msg.get('error')}")
            self._resolve("license", error=msg.get("error") or "激活失败")
        elif t == "license_state":
            lic = msg.get("license") or {}
            self.license_state.update({
                "licensed": bool(msg.get("licensed")),
                "username": lic.get("username"),
                "status": lic.get("status"),
                "reason": lic.get("reason"),
                "required": bool(lic.get("required", True)),
            })
            self._sessions_dirty = True
        elif t == "admin_auth_ok":
            self.license_state["is_admin"] = True
            self.license_state["licensed"] = True
            self._resolve("admin_auth", result=True)
        elif t == "admin_auth_err":
            self._resolve("admin_auth", error=msg.get("error") or "管理员密钥不正确")
        elif t == "admin_ok":
            self._resolve(msg.get("req"), result=msg.get("data") or {})
        elif t == "admin_err":
            self._resolve(msg.get("req"), error=msg.get("error") or "操作失败")
        elif t == "info":
            self.server_info.update(msg.get("info") or {})

        # ---------------- 自动更新 ----------------
        elif t == "update_info":
            self._on_update_info(msg)
        elif t == "update_chunk":
            self._on_update_chunk(msg)
        elif t == "update_err":
            self.update_state["downloading"] = False
            self.update_state["error"] = msg.get("error") or "服务端拒绝下载"
            self._resolve("update_dl", error=self.update_state["error"])

    # ------------------------------------------------ 自动更新

    def _on_update_info(self, msg: dict) -> None:
        """服务端回了「有没有新版本」。"""
        self.update_state.update({
            "current": msg.get("current") or VERSION,
            "latest": msg.get("latest"),
            "available": bool(msg.get("available")),
            "notes": msg.get("notes") or "",
            "size": int(msg.get("size") or 0),
            "sha256": msg.get("sha256") or "",
            "release": msg.get("release") or "",
            "checked_at": now(),
            "error": None,
        })
        if self.update_state["available"]:
            self.log.info(f"发现新版本 {self.update_state['latest']}"
                          f"（当前 {self.update_state['current']}）")
        else:
            self.log.debug("已经是最新版本")
        self._resolve("update_check", result=dict(self.update_state))

    def _on_update_chunk(self, msg: dict) -> None:
        """收到一块更新包数据（base64）。"""
        try:
            blob = base64.b64decode(msg.get("data") or "")
        except Exception:                            # noqa: BLE001
            blob = b""
        total = int(msg.get("size") or self.update_state.get("size") or 0)
        # 防御：多收到的字节说明对端/自己算错了偏移，直接判失败，
        # 免得把重复数据拼进包里、最后 sha256 对不上还查不出原因。
        if total and len(self._update_buf) + len(blob) > total:
            self._update_buf = bytearray()
            self.update_state.update({"downloading": False, "progress": 0.0,
                                      "error": "更新包下载越界（偏移算错了）"})
            self.log.error("更新包下载越界，已丢弃")
            self._resolve("update_dl", error=self.update_state["error"])
            return
        self._update_buf.extend(blob)
        if total:
            self.update_state["progress"] = min(1.0, len(self._update_buf) / total)
        # 顺序很重要：最后一块要**先**把 ready 填好、**再** resolve 等待方，
        # 否则下载协程被唤醒时 ready 还是上一轮的 None（踩过）。
        if msg.get("done") or (total and len(self._update_buf) >= total):
            data = bytes(self._update_buf)
            self._update_buf = bytearray()
            want = str(self.update_state.get("sha256") or "").lower()
            got = hashlib.sha256(data).hexdigest()
            if want and got != want:
                self.update_state.update({"downloading": False, "progress": 0.0,
                                          "ready": None,
                                          "error": "更新包校验失败（sha256 不一致）"})
                self.log.error("更新包 sha256 校验失败，已丢弃")
                self._resolve("update_dl", error=self.update_state["error"])
                return
            self.update_state.update({"downloading": False, "progress": 1.0,
                                      "ready": data, "error": None})
            self.log.info(f"更新包下载完成（{len(data)} 字节，校验通过）")
        self._resolve("update_dl", result=len(self._update_buf))

    def _merge_stats(self, data: dict) -> None:
        for mid, st in data.items():
            m = self.mappings.get(mid)
            if not m:
                continue
            m.rx = int(st.get("rx") or 0)
            m.tx = int(st.get("tx") or 0)
            m.active_conns = int(st.get("active") or 0)
            m.total_conns = int(st.get("total") or 0)
            if m.enabled:
                srv_status = st.get("status")
                if srv_status == "error":
                    m.error = st.get("error") or m.error
                elif m.status != "error":
                    m.status = "active"
                    m.error = None
            if st.get("tunnel_id"):
                m.tunnel_id = int(st["tunnel_id"])
                if m.proto == "udp":
                    self.tunnel_index[m.tunnel_id] = m

    # ------------------------------------------------ 授权：请求/应答

    def _resolve(self, key, result=None, error=None) -> None:
        """把服务端的应答交给正在等待的那个 future。"""
        fut = self._pending.pop(key, None) if key else None
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(RuntimeError(error))
        else:
            fut.set_result(result)

    async def activate_license(self, code: str):
        """用一次性邀请码激活。成功返回服务端应答，失败抛异常（异常文本就是原因）。"""
        if not self.connected or self.control_writer is None:
            raise RuntimeError("还没有连接到服务器，请稍后再试")
        fut = asyncio.get_running_loop().create_future()
        self._pending["license"] = fut
        try:
            await send_msg(self.control_writer, {
                "t": "license_activate", "code": code.strip(),
                "fingerprint": self.fingerprint,
                "device_name": socket.gethostname()})
            return await asyncio.wait_for(fut, 15)
        finally:
            self._pending.pop("license", None)

    async def admin_auth(self, key: str):
        """用管理员密钥解锁管理员模式。返回 (ok, error)。"""
        key = (key or "").strip()
        if not key:
            return False, "请输入管理员密钥"
        if not self.connected or self.control_writer is None:
            return False, "还没有连接到服务器"
        fut = asyncio.get_running_loop().create_future()
        self._pending["admin_auth"] = fut
        try:
            await send_msg(self.control_writer, {"t": "admin_auth", "admin_token": key})
            await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError:
            return False, "服务器没有响应，请检查网络"
        except Exception as exc:                     # noqa: BLE001
            return False, str(exc) or "管理员密钥不正确"
        finally:
            self._pending.pop("admin_auth", None)
        self.admin_key = key
        self.license_state["is_admin"] = True
        self.license_state["licensed"] = True
        return True, None

    async def admin_lock_async(self):
        """退出管理员模式（下次重连就不再带管理员密钥）。"""
        self.admin_key = ""
        self.license_state["is_admin"] = False
        cfg_srv = self.cfg.get("server") or {}
        if cfg_srv.get("admin_token"):
            cfg_srv["admin_token"] = ""
            self.save_config()
        return True

    # ------------------------------------------------ 自动更新

    async def check_update(self, force: bool = False) -> dict:
        """问服务端有没有新版本。返回 update_state 的快照。"""
        if not self.connected or self.control_writer is None:
            self.update_state["error"] = "还没有连接到服务器"
            return dict(self.update_state)
        fut = asyncio.get_running_loop().create_future()
        self._pending["update_check"] = fut
        try:
            # 报"我这个客户端"的版本，不是模块常量 —— 以后要支持按渠道/热修
            # 区分版本时，改的是 state 里的值，不该动这里。
            await send_msg(self.control_writer, {
                "t": "update_check",
                "version": self.update_state.get("current") or VERSION,
                "release": RELEASE, "os": "windows", "force": bool(force)})
            await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError:
            self.update_state["error"] = "服务端没有响应"
        except Exception as exc:                     # noqa: BLE001
            self.update_state["error"] = str(exc)
        finally:
            self._pending.pop("update_check", None)
        return dict(self.update_state)

    async def download_update(self) -> bytes | None:
        """把更新包拉下来（分块走控制通道），校验通过后放到 update_state["ready"]。"""
        if not self.update_state.get("available"):
            return None
        if not self.connected or self.control_writer is None:
            self.update_state["error"] = "还没有连接到服务器"
            return None
        self._update_buf = bytearray()
        self.update_state.update({"downloading": True, "progress": 0.0, "error": None})
        total = int(self.update_state.get("size") or 0)
        try:
            while True:
                # 关键：每一块都要一个**新的** future。第一版图省事在循环外建了
                # 一个 fut 反复 await，结果 shield 把它保护住、永远返回第一块的
                # 结果，offset 一直在 0，最后拼出一堆重复数据、循环也退不出来。
                fut = asyncio.get_running_loop().create_future()
                self._pending["update_dl"] = fut
                offset = len(self._update_buf)
                await send_msg(self.control_writer, {
                    "t": "update_fetch", "offset": offset,
                    "chunk": UPDATE_CHUNK, "os": "windows"})
                got = await asyncio.wait_for(fut, 30)
                if got is None:                      # 出错，_on_update_chunk 已经写了原因
                    return None
                # 最后一块收完时 _on_update_chunk 会把缓冲区清空并填好 ready，
                # 所以先看 ready，再看字节数 —— 不能只看 len(_update_buf)。
                if self.update_state.get("ready") is not None:
                    break
                now_len = len(self._update_buf)
                if total and now_len >= total:
                    break
                if now_len <= offset:                # 服务端没给新数据，别死循环
                    self.update_state.update({
                        "downloading": False,
                        "error": f"下载中断（收到 {now_len}/{total} 字节）"})
                    return None
        except asyncio.TimeoutError:
            self.update_state.update({"downloading": False,
                                      "error": "下载更新包超时"})
            return None
        except Exception as exc:                     # noqa: BLE001
            self.update_state.update({"downloading": False, "error": str(exc)})
            return None
        finally:
            self._pending.pop("update_dl", None)
        # 最后一块是在 _on_update_chunk 里先 _resolve、再拼包校验的，
        # 所以这里让出一拍，等它把 ready 填好再取。
        await asyncio.sleep(0)
        return self.update_state.get("ready")

    def apply_update(self, data: bytes | None = None) -> str:
        """把下载好的更新包交给独立的更新助手，然后由它替换文件并重启。

        必须交给**另一个进程**做：McLink.exe 正在运行，Windows 不允许覆盖它。
        所以这里只负责落盘 + 拉起助手，助手等我们退出后再动手。
        """
        blob = data if data is not None else self.update_state.get("ready")
        if not blob:
            raise RuntimeError("还没有下载好更新包")
        base = os.path.dirname(os.path.abspath(self.config_path))
        tmp = os.path.join(base, "update-cache")
        os.makedirs(tmp, exist_ok=True)
        zpath = os.path.join(tmp, "mclink-update.zip")
        with open(zpath, "wb") as fh:
            fh.write(blob)
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "update_helper.py")
        if not os.path.exists(helper):
            raise RuntimeError("缺少 update_helper.py，无法自动更新")
        log = os.path.join(tmp, "update.log")
        # 优先用 pythonw.exe 跑助手：python.exe 会闪一个黑框出来，
        # 虽然只一瞬间，但看起来像出错了。
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        py = pyw if os.path.exists(pyw) else sys.executable
        argv = [py, helper,
                "--pid", str(os.getpid()),
                "--zip", zpath,
                "--dir", base,
                "--log", log,
                "--restart-exe", os.path.join(base, "McLink.exe")]
        creation = 0x00000008 | 0x08000000           # DETACHED | NO_WINDOW
        subprocess.Popen(argv, cwd=base, close_fds=True, creationflags=creation)
        self.update_state["applying"] = True
        return log

    async def admin_request(self, action: str, args: dict | None = None):
        """发一个管理员请求并等应答。失败抛异常。"""
        if not self.license_state.get("is_admin"):
            raise RuntimeError("没有管理员权限，请先输入管理员密钥")
        if not self.connected or self.control_writer is None:
            raise RuntimeError("还没有连接到服务器")
        self._req_seq += 1
        req = f"r{self._req_seq}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        try:
            await send_msg(self.control_writer, {
                "t": "admin", "req": req, "action": action, "args": args or {}})
            return await asyncio.wait_for(fut, 20)
        except asyncio.TimeoutError:
            raise RuntimeError("服务器没有响应，请稍后再试")
        finally:
            self._pending.pop(req, None)

    # ------------------------------------------------ 映射注册同步

    def _udp_register_now(self) -> None:
        """让 UDP 隧道包**立刻**发一次，别等下一轮循环。

        原来这里只置了个 _udp_dirty 标志，而 _udp_loop 是每秒 tick 一次 ——
        于是拿到 register_ok 之后最长有 1 秒的空窗：本地界面已经把映射显示成
        "已生效"，但服务端还没登记隧道地址，玩家这几百毫秒里发的 UDP 包全被丢掉
        （表现为"刚进去卡一下/要重连一次才通"）。现在直接同步发一次。
        """
        self._udp_dirty = True
        self._send_udp_keepalive()

    def _send_udp_keepalive(self) -> int:
        """给所有已注册的 UDP 映射发一次隧道包。返回发出的条数。"""
        if not self.udp_transport or not self.connected:
            return 0
        host = self.srv.get("host")
        port = int(self.srv.get("udp_port") or self.srv.get("control_port") or 0)
        if not host or not port:
            return 0
        sent = 0
        for m in self.mappings.values():
            if m.enabled and m.proto == "udp" and m.tunnel_id:
                try:
                    self.udp_transport.sendto(
                        pack_udp_reg(m.tunnel_id, self.srv.get("token", "")),
                        (host, port))
                    sent += 1
                except OSError as exc:
                    self.log.warn(f"UDP 保活发送失败: {exc}")
        if sent:
            self.log.debug(f"已发送 {sent} 个 UDP 隧道保活包 -> {host}:{port}")
        return sent

    async def _sync_registrations(self) -> None:
        """把启用/禁用的变化同步给服务端。"""
        w = self.control_writer
        if not self.connected or w is None:
            return
        self._sessions_dirty = False
        for m in self.mappings.values():
            # 除了 enabled，还要过 _start_gate：客户端刚起来时默认不放行
            # （client.auto_start_mappings 关着），避免"打开软件就自动开映射"。
            # 用户点一下开关 / 点「启动全部映射」就会放行。
            if m.enabled and m._start_gate and m.tunnel_id == 0 and now() >= m.retry_at:
                try:
                    await send_msg(w, {"t": "register", "mapping": {
                        "mid": m.id, "name": m.name, "proto": m.proto,
                        "remote_port": m.remote_port,
                    }})
                    m.status = "pending"
                    m.error = None
                    m.retry_at = now() + 3.0      # 等 register_ok，期间不重复发
                except Exception as exc:
                    self.log.warn(f"注册 {m.name} 失败: {exc}")
                    m.retry_at = now() + 3.0
            elif (not m.enabled or not m._start_gate) and m.tunnel_id:
                try:
                    await send_msg(w, {"t": "unregister", "mid": m.id})
                except Exception:
                    pass
                self.tunnel_index.pop(m.tunnel_id, None)
                m.tunnel_id = 0
                m.status = "inactive"
                m.error = None
                m.retry_at = 0.0
                m.rx_rate = m.tx_rate = 0.0

    def reload_from_disk(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.log.error(f"重新读取配置失败: {exc}")
            return
        self.cfg["mappings"] = cfg.get("mappings") or []
        new: dict[str, Mapping] = {}
        for spec in self.cfg["mappings"]:
            m = Mapping(spec)
            if m.id in self.mappings:
                m.status = self.mappings[m.id].status
            new[m.id] = m
        self.mappings = new
        self._sessions_dirty = True
        self._udp_dirty = True
        self.log.info(f"配置已重新加载，共 {len(self.mappings)} 条映射")

    # ------------------------------------------------ TCP 回连与转发

    async def _do_dial(self, msg: dict) -> None:
        mid = msg.get("mid")
        tok = msg.get("tok")
        m = self.mappings.get(mid)
        if m is None or not m.enabled:
            self.log.warn(f"收到未知映射的 dial: {mid}")
            return
        self._dials += 1
        try:
            d_reader, d_writer = await self._open(self.srv["host"], self.data_port, 10)
        except Exception as exc:
            self.log.error(f"[{m.name}] 回连服务端失败: {exc}")
            return
        try:
            await send_msg(d_writer, {"t": "mux", "tok": tok, "mid": mid, "proto": "tcp"})
        except Exception:
            d_writer.close()
            return
        try:
            l_reader, l_writer = await asyncio.wait_for(
                asyncio.open_connection(m.local_host, m.local_port), 6)
        except Exception as exc:
            m.mark_local_error(
                f"无法连接本地游戏端口 {m.local_host}:{m.local_port}"
                f"（{type(exc).__name__}），游戏服务端是否已启动？")
            self.log.warn(f"[{m.name}] {m.local_error}")
            d_writer.close()
            return
        m.clear_local_error()
        m.error = None
        self.log.debug(f"[{m.name}] 建立转发通道")
        try:
            await asyncio.gather(
                self._pipe(d_reader, l_writer),
                self._pipe(l_reader, d_writer),
                return_exceptions=True,
            )
        finally:
            for w in (d_writer, l_writer):
                try:
                    w.close()
                except Exception:
                    pass

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(BUF_SIZE)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.write_eof()
            except Exception:
                pass

    # ------------------------------------------------ UDP 隧道

    async def _udp_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await loop.create_datagram_endpoint(
                    lambda: UdpTunnelProtocol(self), local_addr=("0.0.0.0", 0))
                self.log.info("UDP 隧道 socket 已就绪")
                break
            except OSError as exc:
                self.log.warn(f"创建 UDP 隧道失败，5 秒后重试: {exc}")
                await asyncio.sleep(5)

        last_reg = 0.0
        while not self._stop.is_set():
            await asyncio.sleep(1)
            if not self.udp_transport:
                continue
            need = self._udp_dirty or (now() - last_reg) >= UDP_KEEPALIVE_S
            if need and self.connected:
                if self._send_udp_keepalive():
                    last_reg = now()
                self._udp_dirty = False
            self._reap_udp_sessions()

    def _reap_udp_sessions(self) -> None:
        cutoff = now() - UDP_SESSION_IDLE_S
        for key in [k for k, s in self.udp_sessions.items() if s.last < cutoff]:
            self.udp_sessions.pop(key).close()

    def udp_send_up(self, m: Mapping, player_addr, payload: bytes) -> None:
        if not self.udp_transport or not m.tunnel_id:
            return
        try:
            self.udp_transport.sendto(
                pack_udp_data(m.tunnel_id, player_addr, payload,
                              self.srv.get("token", "")),
                (self.srv["host"], int(self.srv["udp_port"])))
        except OSError as exc:
            self.log.debug(f"UDP 上行失败: {exc}")

    def udp_deliver_local(self, m: Mapping, player_addr, payload: bytes) -> None:
        key = (m.id, player_addr[0], player_addr[1])
        sess = self.udp_sessions.get(key)
        if sess is None:
            if len([k for k in self.udp_sessions if k[0] == m.id]) >= MAX_UDP_SESSIONS:
                return
            sess = LocalUdpSession(self, m, player_addr)
            self.udp_sessions[key] = sess     # 先登记，避免同一玩家被并发创建多次
            sess.pending.append(payload)
            try:
                asyncio.get_running_loop().create_task(self._open_session(sess, m, key))
            except RuntimeError:
                self.udp_sessions.pop(key, None)
            return
        sess.last = now()
        if sess.transport is not None:
            try:
                sess.transport.sendto(payload)
            except OSError as exc:
                self.log.debug(f"UDP 送本地失败: {exc}")
        elif len(sess.pending) < 32:
            sess.pending.append(payload)

    async def _open_session(self, sess: LocalUdpSession, m: Mapping, key) -> None:
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: sess, remote_addr=(m.local_host, m.local_port))
        except OSError as exc:
            self.udp_sessions.pop(key, None)
            self.log.warn(f"[{m.name}] 无法创建本地 UDP 会话: {exc}")
            m.mark_local_error(f"本地 UDP 不可用: {exc}")

    # ------------------------------------------------ 速率统计

    async def _rate_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1)
            t = now()
            dt = max(0.001, t - self._stat_t)
            tot_rx = sum(m.rx for m in self.mappings.values())
            tot_tx = sum(m.tx for m in self.mappings.values())
            self.stats["rx_rate"] = max(0.0, (tot_rx - self._stat_rx) / dt)
            self.stats["tx_rate"] = max(0.0, (tot_tx - self._stat_tx) / dt)
            self.stats["rx_bytes"] = tot_rx
            self.stats["tx_bytes"] = tot_tx
            self._stat_rx, self._stat_tx, self._stat_t = tot_rx, tot_tx, t
            for m in self.mappings.values():
                d = max(0.001, t - m._last_t)
                m.rx_rate = max(0.0, (m.rx - m._last_rx) / d)
                m.tx_rate = max(0.0, (m.tx - m._last_tx) / d)
                m._last_rx, m._last_tx, m._last_t = m.rx, m.tx, t
            await self._sync_registrations()

    # ------------------------------------------------ 快照

    def snapshot(self) -> dict:
        pub = self.public_ip
        return {
            "agent": {
                "version": VERSION, "pid": os.getpid(),
                "started_at": int(self.started_at),
                "uptime_s": int(now() - self.started_at),
                "config_path": self.config_path,
            },
            "server": {
                "host": self.srv.get("host"),
                "control_port": int(self.srv.get("control_port", 7000)),
                "data_port": self.data_port,
                "udp_port": int(self.srv.get("udp_port", 7000)),
                "connected": self.connected,
                "public_ip": pub,
                "latency_ms": self.latency_ms,
                "last_error": self.last_error,
                "reconnect_in_s": (round(self.reconnect_in_s, 1)
                                   if self.reconnect_in_s else None),
                "allowed_ports": self.server_info.get("allowed_ports") or [],
            },
            "stats": {
                "rx_bytes": self.stats.get("rx_bytes", 0),
                "tx_bytes": self.stats.get("tx_bytes", 0),
                "rx_rate": round(self.stats.get("rx_rate", 0.0), 1),
                "tx_rate": round(self.stats.get("tx_rate", 0.0), 1),
                "tcp_conns": sum(m.active_conns for m in self.mappings.values()
                                 if m.proto == "tcp"),
                "udp_sessions": len(self.udp_sessions),
            },
            "mappings": [m.snapshot(pub) for m in self.mappings.values()],
            "license": self.license_info(),
            "update": self.update_info(),
        }

    # ------------------------------------------------ 映射增删改

    def allowed_ranges(self) -> list:
        """服务端允许的公网端口范围，形如 [[25000, 26000], [25565, 25565]]。"""
        out = []
        for p in (self.server_info.get("allowed_ports") or []):
            try:
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    out.append((int(p[0]), int(p[1])))
            except (TypeError, ValueError):
                continue
        return out

    def port_allowed(self, port: int) -> bool:
        rng = self.allowed_ranges()
        return True if not rng else any(lo <= port <= hi for lo, hi in rng)

    def describe_allowed(self) -> str:
        rng = self.allowed_ranges()
        if not rng:
            return "（服务端未限制，任意端口均可）"
        return "、".join(f"{lo}-{hi}" if lo != hi else str(lo) for lo, hi in rng)

    def validate(self, spec: dict, mid: str | None = None) -> str | None:
        name = str(spec.get("name") or "").strip()
        if not name:
            return "名称不能为空"
        proto = str(spec.get("proto") or "").lower()
        if proto not in ("tcp", "udp"):
            return "协议只能是 tcp 或 udp"
        try:
            lp = int(spec.get("local_port"))
            rp = int(spec.get("remote_port"))
        except (TypeError, ValueError):
            return "端口必须是数字"
        for label, p in (("本地端口", lp), ("公网端口", rp)):
            if not (1 <= p <= 65535):
                return f"{label}必须在 1-65535 之间"
        if rp in (int(self.srv.get("control_port", 7000)),
                  self.data_port,
                  int(self.srv.get("udp_port", 7000))):
            return f"公网端口 {rp} 是 McLink 自己使用的端口"
        if not self.port_allowed(rp):
            return (f"公网端口 {rp} 不在服务端允许范围内"
                    f"（可用：{self.describe_allowed()}）")
        for m in self.mappings.values():
            if m.id != mid and m.proto == proto and m.remote_port == rp:
                return f"{proto.upper()} 公网端口 {rp} 已被映射「{m.name}」占用"
        return None

    def upsert(self, spec: dict) -> Mapping:
        mid = spec.get("id")
        if mid and mid in self.mappings:
            m = self.mappings[mid]
            new_proto = str(spec.get("proto") or m.proto).lower()
            try:
                new_remote = int(spec.get("remote_port") or m.remote_port)
            except (TypeError, ValueError):
                new_remote = m.remote_port
            # 协议或公网端口变了：先摘掉旧的隧道，下一轮重新注册
            if new_proto != m.proto or new_remote != m.remote_port:
                self.tunnel_index.pop(m.tunnel_id, None)
                for key in [k for k in self.udp_sessions if k[0] == m.id]:
                    self.udp_sessions.pop(key).close()
                m.tunnel_id = 0
                m.retry_at = 0.0
                m.status = "pending" if m.enabled else "inactive"
                m.error = None
            m.apply(spec)
        else:
            m = Mapping(spec)
            self.mappings[m.id] = m
            m.status = "pending" if m.enabled else "inactive"
        # 用户主动新增/编辑映射 = 明确要它跑起来，直接放行。
        # （只有"从配置文件里读出来的旧映射"才受启动开关约束，
        #   否则用户刚加完映射还得再去点一下开关，太反直觉。）
        if m.enabled:
            m.gate(True)
        self._sessions_dirty = True
        self._udp_dirty = True
        self.save_config()
        return m

    def delete(self, mid: str) -> bool:
        m = self.mappings.pop(mid, None)
        if not m:
            return False
        self.tunnel_index.pop(m.tunnel_id, None)
        for key in [k for k in self.udp_sessions if k[0] == mid]:
            self.udp_sessions.pop(key).close()
        self._sessions_dirty = True
        self._udp_dirty = True
        self.save_config()
        return True

    def set_enabled(self, mid: str, enabled: bool) -> bool:
        m = self.mappings.get(mid)
        if not m:
            return False
        m.enabled = enabled
        # 用户显式操作 = 放行（启动时是不放行的，见 __init__ 里的 gate）
        m.gate(enabled)
        if not enabled:
            self.tunnel_index.pop(m.tunnel_id, None)
            m.tunnel_id = 0
            m.status = "inactive"
            m.error = None
            m.rx_rate = m.tx_rate = 0.0
            for key in [k for k in self.udp_sessions if k[0] == mid]:
                self.udp_sessions.pop(key).close()
        else:
            m.retry_at = 0.0
            m.status = "pending"
            m.error = None
        self._sessions_dirty = True
        self._udp_dirty = True
        self.save_config()
        return True

    def start_all_mappings(self) -> int:
        """一键放行并启用所有映射（启动时默认不放行，给用户一个快捷入口）。"""
        n = 0
        for m in list(self.mappings.values()):
            if m.enabled and m._start_gate:
                continue
            self.set_enabled(m.id, True)
            n += 1
        return n

    def stop_all_mappings(self) -> int:
        """一键停用所有映射。"""
        n = 0
        for m in list(self.mappings.values()):
            if m.enabled or m._start_gate:
                self.set_enabled(m.id, False)
                n += 1
        return n

    async def selftest(self) -> list[dict]:
        out = []
        for m in self.mappings.values():
            if m.proto == "tcp":
                try:
                    r, w = await asyncio.wait_for(
                        asyncio.open_connection(m.local_host, m.local_port), 2)
                    w.close()
                    out.append({"mapping_id": m.id, "local_ok": True,
                                "msg": f"本地 {m.local_host}:{m.local_port} 可连接"})
                except Exception as exc:
                    out.append({"mapping_id": m.id, "local_ok": False,
                                "msg": f"本地 {m.local_host}:{m.local_port} 连不上"
                                       f"（{type(exc).__name__}）"})
            else:
                ok = any(k[0] == m.id for k in self.udp_sessions)
                out.append({"mapping_id": m.id, "local_ok": ok,
                            "msg": ("已有玩家会话，本地 UDP 正常" if ok else
                                    "UDP 无连接无法主动探测，请让玩家实际连一次")})
        return out


# ---------------------------------------------------------------- HTTP 控制台

class WebServer:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.web = agent.cfg.get("web") or {}
        self.host = self.web.get("host", "127.0.0.1")
        self.port = int(self.web.get("port", 8787))
        self.token = str(self.web.get("token") or "")
        self.srv: asyncio.Server | None = None

    async def start(self) -> None:
        """启动控制台。

        端口被占用时**顺延试几个**，绝不再抛异常 —— 之前这里抛 SystemExit，
        会把整个引擎线程带走，隧道跟着全断，而界面上一点提示都没有。
        网页控制台只是附加功能，桌面版没了它照样跑。
        """
        last = None
        for offset in range(6):
            port = self.port + offset
            try:
                self.srv = await asyncio.start_server(self.handle, self.host, port)
                self.port = port
                break
            except OSError as exc:
                last = exc
        if not self.srv:
            LOG.warn(f"网页控制台端口 {self.port}~{self.port + 5} 都被占用，已跳过"
                     f"（桌面版功能不受影响）: {last}")
            return
        LOG.info(f"控制台已就绪: http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self.srv:
            self.srv.close()
            try:
                await self.srv.wait_closed()
            except Exception:
                pass

    # -------------------------------------------- 请求处理

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await asyncio.wait_for(self._read_request(reader), 15)
        except Exception:
            writer.close()
            return
        if req is None:
            writer.close()
            return
        method, raw_path, headers, body = req
        parts = urlsplit(raw_path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)

        try:
            cors = self._cors(headers.get("origin"))
            if self.token and not self._authed(headers, query):
                await self._json(writer, 401, {"ok": False, "error": "控制台口令不正确"}, cors)
                return
            if method == "OPTIONS":       # 跨域预检（双击 index.html 打开时用得到）
                await self._json(writer, 204, {}, cors)
                return
            if path == "/api/events":
                await self._sse(writer, cors)
                return
            await self._route(method, path, query, body, writer, cors)
        except Exception as exc:
            LOG.error(f"控制台请求出错 {method} {path}: {exc!r}\n{traceback.format_exc()}")
            try:
                await self._json(writer, 500, {"ok": False, "error": str(exc)})
            except Exception:
                pass
        finally:
            if not writer.is_closing():
                try:
                    await writer.drain()
                except Exception:
                    pass
                writer.close()

    async def _read_request(self, reader):
        line = await reader.readline()
        if not line:
            return None
        try:
            method, raw_path, _ = line.decode("latin1").split(None, 2)
        except ValueError:
            return None
        headers = {}
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        body = b""
        if "content-length" in headers:
            try:
                n = int(headers["content-length"])
            except ValueError:
                n = 0
            if n:
                body = await reader.readexactly(n)
        return method, raw_path, headers, body

    def _authed(self, headers, query) -> bool:
        tok = headers.get("x-mclink-token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(str(tok), self.token)

    def _cors(self, origin: str | None) -> str:
        """只允许本机来源跨域，方便直接双击 index.html 打开控制台。"""
        if not origin:
            return ""
        host = urlsplit(origin).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1") or origin == "null":
            return (f"Access-Control-Allow-Origin: {origin}\r\n"
                    "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
                    "Access-Control-Allow-Headers: Content-Type, X-McLink-Token\r\n"
                    "Access-Control-Max-Age: 600\r\n")
        return ""

    async def _json(self, writer, status: int, obj: dict, cors: str = "") -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        writer.write(f"HTTP/1.1 {status} {self._reason(status)}\r\n".encode())
        writer.write(b"Content-Type: application/json; charset=utf-8\r\n")
        writer.write(b"Cache-Control: no-store\r\n")
        if cors:
            writer.write(cors.encode())
        if status == 204:
            writer.write(b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            return
        writer.write(f"Content-Length: {len(payload)}\r\n".encode())
        writer.write(b"Connection: close\r\n\r\n")
        writer.write(payload)

    @staticmethod
    def _reason(status: int) -> str:
        return {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
                404: "Not Found", 405: "Method Not Allowed",
                500: "Internal Server Error"}.get(status, "OK")

    async def _route(self, method, path, query, body, writer, cors: str = "") -> None:
        agent = self.agent

        if path == "/" or path == "/index.html":
            await self._static(writer, "index.html", "text/html; charset=utf-8", cors)
            return

        if path == "/api/state" and method == "GET":
            await self._json(writer, 200, agent.snapshot(), cors)
            return

        if path == "/api/logs" and method == "GET":
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            await self._json(writer, 200, {"logs": LOG.history[-limit:]}, cors)
            return

        if path == "/api/server_info" and method == "GET":
            info = dict(agent.server_info)
            info["uptime_s"] = info.get("uptime_s", 0)
            info["public_ip"] = agent.public_ip or info.get("public_ip")
            info["version"] = info.get("version") or VERSION
            info["data_port"] = agent.data_port
            info["shared_data_port"] = agent.data_port == int(agent.srv.get("control_port", 0))
            await self._json(writer, 200, {"ok": True, "info": info}, cors)
            return

        if path == "/api/selftest" and method == "GET":
            await self._json(writer, 200, {"ok": True, "results": await agent.selftest()}, cors)
            return

        if path == "/api/mappings" and method == "POST":
            spec = self._parse_json(body, writer)
            if spec is None:
                await self._json(writer, 400, {"ok": False, "error": "请求体不是合法 JSON"}, cors)
                return
            err = agent.validate(spec, spec.get("id"))
            if err:
                await self._json(writer, 400, {"ok": False, "error": err}, cors)
                return
            m = agent.upsert(spec)
            await self._json(writer, 200, {"ok": True, "mapping": m.snapshot(agent.public_ip)}, cors)
            return

        if path.startswith("/api/mappings/"):
            rest = path[len("/api/mappings/"):]
            if rest.endswith("/toggle") and method == "POST":
                mid = rest[:-len("/toggle")]
                payload = self._parse_json(body, writer, allow_empty=True) or {}
                if not agent.set_enabled(mid, bool(payload.get("enabled", True))):
                    await self._json(writer, 404, {"ok": False, "error": "映射不存在"}, cors)
                    return
                await self._json(writer, 200, {"ok": True}, cors)
                return
            if method == "DELETE":
                if not agent.delete(rest):
                    await self._json(writer, 404, {"ok": False, "error": "映射不存在"}, cors)
                    return
                await self._json(writer, 200, {"ok": True}, cors)
                return

        if path == "/api/control" and method == "POST":
            payload = self._parse_json(body, writer, allow_empty=True) or {}
            action = payload.get("action")
            if action == "reconnect":
                agent.log.info("手动触发重连…")
                if agent.control_writer:
                    try:
                        agent.control_writer.close()
                    except Exception:
                        pass
                await self._json(writer, 200, {"ok": True}, cors)
            elif action == "reload_config":
                agent.reload_from_disk()
                await self._json(writer, 200, {"ok": True}, cors)
            elif action == "restart_agent":
                await self._json(writer, 200, {"ok": True}, cors)
                asyncio.get_running_loop().call_later(0.4, respawn_agent)
            else:
                await self._json(writer, 400, {"ok": False, "error": "未知操作"}, cors)
            return

        await self._json(writer, 404, {"ok": False, "error": "接口不存在"}, cors)

    def _parse_json(self, body: bytes, writer, allow_empty: bool = False):
        if not body:
            return {} if allow_empty else None
        try:
            obj = json.loads(body.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError
            return obj
        except Exception:
            return {}

    async def _static(self, writer, name: str, ctype: str, cors: str = "") -> None:
        path = os.path.join(HERE, "web", name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            data = (b"<h1>McLink</h1><p>web/index.html \xe4\xb8\xa2\xe5\xa4\xb1\xe4\xba\x86</p>")
            ctype = "text/html; charset=utf-8"
        writer.write(b"HTTP/1.1 200 OK\r\n")
        writer.write(f"Content-Type: {ctype}\r\n".encode())
        writer.write(b"Cache-Control: no-store\r\n")
        if cors:
            writer.write(cors.encode())
        writer.write(f"Content-Length: {len(data)}\r\n".encode())
        writer.write(b"Connection: close\r\n\r\n")
        writer.write(data)

    async def _sse(self, writer, cors: str = "") -> None:
        q = LOG.subscribe()
        writer.write(b"HTTP/1.1 200 OK\r\n")
        writer.write(b"Content-Type: text/event-stream; charset=utf-8\r\n")
        writer.write(b"Cache-Control: no-store\r\n")
        writer.write(b"Connection: keep-alive\r\n")
        writer.write(b"X-Accel-Buffering: no\r\n")
        if cors:
            writer.write(cors.encode())
        writer.write(b"\r\n")
        await writer.drain()
        try:
            for rec in LOG.history[-100:]:
                await self._sse_send(writer, "log", rec)
            last = 0.0
            while True:
                drained = 0
                while drained < 50:
                    try:
                        rec = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self._sse_send(writer, "log", rec)
                    drained += 1
                if now() - last >= 1:
                    await self._sse_send(writer, "state", self.agent.snapshot())
                    last = now()
                await asyncio.sleep(0.2)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            LOG.unsubscribe(q)

    async def _sse_send(self, writer, event: str, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False)
        writer.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        await writer.drain()


# ---------------------------------------------------------------- 进程重启

# 「重启自己」时该用什么命令行拉起新进程。
# 由宿主程序设置 —— 桌面版必须把它设成 mclink_gui.py，
# 否则重启出来的会是一个没有窗口的后台内核（这就是之前"控制台一直重启"的根因之一）。
RESTART_ARGV: list | None = None


def respawn_agent() -> None:
    """重新拉起自己（用于控制台的"重启"功能）。"""
    LOG.info("正在重启客户端进程…")
    argv = RESTART_ARGV or [sys.executable, os.path.abspath(__file__), *sys.argv[1:]]
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        subprocess.Popen(argv, cwd=os.getcwd(), close_fds=True, **kwargs)
    except Exception as exc:
        LOG.error(f"重启失败: {exc}")
        return
    time.sleep(0.4)
    os._exit(0)


# ---------------------------------------------------------------- 入口

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"配置文件不存在: {path}\n请先复制 config.client.json 并填写服务器地址与密钥。")
    with open(path, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    merged = {**DEFAULT_CFG, **cfg}
    merged["server"] = {**DEFAULT_CFG["server"], **(cfg.get("server") or {})}
    merged["web"] = {**DEFAULT_CFG["web"], **(cfg.get("web") or {})}
    merged["client"] = {**DEFAULT_CFG["client"], **(cfg.get("client") or {})}
    return merged


async def amain(args) -> None:
    global LOG
    config_path = os.path.abspath(args.config)
    cfg = load_config(config_path)
    if args.web_port:
        cfg["web"]["port"] = int(args.web_port)
    LOG = LogBus(cfg.get("log_level", "info"), cfg.get("log_file", ""))

    if not cfg["server"].get("token") or cfg["server"]["token"] == "CHANGE-ME-PLEASE":
        LOG.warn("config.client.json 里的 token 还是默认值，请改成与服务端一致的随机密钥！")

    agent = Agent(cfg, config_path)
    web = WebServer(agent)
    await web.start()

    LOG.info(f"McLink 客户端 v{VERSION} 启动 (PID {os.getpid()})")
    LOG.info(f"服务器 {cfg['server']['host']}:{cfg['server']['control_port']}  "
             f"映射 {len(agent.mappings)} 条")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):
            pass

    runner = asyncio.create_task(agent.run())
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    await agent.shutdown()
    await web.stop()
    LOG.info("已退出。")


def main() -> None:
    ap = argparse.ArgumentParser(description="McLink 端口映射客户端")
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.client.json"))
    ap.add_argument("--web-port", type=int, default=0, help="覆盖控制台端口")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()
    if args.version:
        print(f"McLink client {VERSION}")
        return
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

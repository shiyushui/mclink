#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 授权管理（服务端）  mclink_license.py
============================================
软件分发出去以后，靠这里决定"谁能用"：

    管理员用管理员密钥 → 生成一串 24 小时有效、只能用一次的邀请码（并指定用户名）
    用户拿到邀请码 → 在自己的 McLink 里激活 → 服务端发一个长期设备令牌
    之后用户每次连接都用这个令牌自动校验，一直能用
    管理员可以随时停用/解绑/删除某个用户名

安全上的几个考虑：
  - 邀请码用 secrets 生成，熵足够，且**只能用一次、24 小时过期**
  - 设备令牌只存 **sha256 哈希**，就算 licenses.json 泄露也拿不到能用的令牌
  - 所有密钥比较都用 hmac.compare_digest（防时序侧信道）
  - 授权文件权限 0600，原子写入（先写 .tmp 再 replace，断电不会写坏）
  - 令牌绑定设备指纹，防止把配置整个拷到另一台电脑上白嫖
  - 激活失败有按 IP 的频率限制，防止暴力猜码
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

VERSION = 1

CODE_PREFIX = "MCLK"
INVITE_DEFAULT_HOURS = 24
INVITE_MAX_HOURS = 720                 # 一个月，再多就不合理了
MAX_ACTIVATE_FAILS = 8                 # 每个 IP 每分钟允许的失败次数
USERNAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def now() -> float:
    return time.time()


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def gen_invite_code() -> str:
    """生成形如 MCLK-7F3A-9B2C-4D1E 的邀请码（12 个十六进制字符 = 48 bit 熵）。"""
    raw = secrets.token_hex(6).upper()
    return f"{CODE_PREFIX}-" + "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def normalize_code(code: str) -> str:
    """把用户输入的各种写法（小写、缺前缀、空格/下划线分隔）统一成标准形式。"""
    s = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if s.startswith(CODE_PREFIX):
        s = s[len(CODE_PREFIX):]
    if len(s) != 12:
        return (code or "").strip().upper()
    return f"{CODE_PREFIX}-" + "-".join(s[i:i + 4] for i in range(0, 12, 4))


def mask_code(code: str) -> str:
    """列表里展示用的掩码，避免截图泄露完整密钥。"""
    parts = (code or "").split("-")
    if len(parts) == 4:
        return f"{parts[0]}-****-****-{parts[3]}"
    return (code or "")[:4] + "****"


def check_username(name: str) -> str | None:
    """校验用户名，返回错误信息或 None。"""
    if not name:
        return "用户名不能为空"
    if not (2 <= len(name) <= 32):
        return "用户名长度需要 2-32 个字符"
    bad = [c for c in name if c not in USERNAME_CHARS]
    if bad:
        return "用户名只能包含字母、数字、下划线和短横线"
    return None


class LicenseStore:
    """授权数据 + 全部业务规则。所有方法都在服务端的事件循环里调用（单线程），
    所以内部不需要加锁。"""

    def __init__(self, path: str, required_default: bool = False, log=None):
        self.path = path
        self.log = log
        self.required = bool(required_default)
        self.users: dict[str, dict] = {}
        self.invites: dict[str, dict] = {}
        self.started_at = now()
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ------------------------------------------------ 持久化

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:                     # noqa: BLE001
            if self.log:
                self.log.warn(f"授权文件读取失败，按空库处理: {exc}")
            return
        self.users = data.get("users") or {}
        self.invites = data.get("invites") or {}
        self.required = bool((data.get("settings") or {}).get("required", self.required))
        if self.log:
            self.log.info(f"授权库已载入：{len(self.users)} 个用户、"
                          f"{len(self.invites)} 个邀请码、"
                          f"校验{'已开启' if self.required else '未开启'}")

    def save(self) -> None:
        data = {
            "version": VERSION,
            "settings": {"required": self.required, "updated_at": int(now())},
            "users": self.users,
            "invites": self.invites,
        }
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save = now()
        except OSError as exc:
            if self.log:
                self.log.error(f"授权文件写入失败: {exc}")

    def touch(self) -> None:
        """标记有改动；最多每 15 秒落一次盘，避免心跳把磁盘写爆。"""
        self._dirty = True
        if now() - self._last_save > 15:
            self.save()

    def flush(self) -> None:
        if self._dirty:
            self.save()

    # ------------------------------------------------ 邀请码

    def purge_expired(self) -> None:
        """回收没用的邀请码记录：只清**过期且没用过**的（留一天缓冲便于回看）。

        ⚠️ 绝对不要在这里删"已使用但用户没了"的记录 —— 那些是历史档案，
        用户明确要求保留（"更新不要删过去邀请码"）。它们只是从默认列表里
        隐藏（见 list_invites 的 include_orphans），记录本身一直在。
        """
        t = now()
        gone = [c for c, r in self.invites.items()
                if not r.get("used_at") and r.get("expires_at", 0) < t - 86400]
        for c in gone:
            self.invites.pop(c, None)
        if gone:
            self.touch()

    def is_orphan(self, rec: dict) -> bool:
        """这条已用邀请码绑的用户是不是已经不存在了（= 历史记录）。

        记录**不会**被删除，只是默认不在列表里显示；`stats` 会报数量，
        管理台可以勾选"显示历史"翻出来。
        """
        if rec.get("orphaned_at"):
            return True
        if not rec.get("used_at"):
            return False
        name = rec.get("username") or ""
        if name and name in self.users:
            return False
        # 用户记录里可能还记着这张码（用户名对不上时的兜底判断）
        for u in self.users.values():
            if u.get("invite_code") and u.get("invite_code") == rec.get("code"):
                return False
        return True

    def create_invite(self, username: str, hours=INVITE_DEFAULT_HOURS, note: str = ""):
        username = (username or "").strip()
        err = check_username(username)
        if err:
            return None, err
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return None, "有效期必须是数字（小时）"
        if not (1 <= hours <= INVITE_MAX_HOURS):
            return None, f"有效期需要在 1-{INVITE_MAX_HOURS} 小时之间"

        self.purge_expired()
        code = gen_invite_code()
        while code in self.invites:
            code = gen_invite_code()
        rec = {
            "code": code,
            "username": username,
            "note": (note or "")[:80],
            "created_at": int(now()),
            "expires_at": int(now()) + hours * 3600,
            "used_at": None,
            "used_by": None,
        }
        self.invites[code] = rec
        self.save()
        if self.log:
            self.log.info(f"生成邀请码 {mask_code(code)} → 用户 {username}"
                          f"（{hours} 小时有效，限用一次）")
        return rec, None

    def revoke_invite(self, code: str):
        code = normalize_code(code)
        rec = self.invites.pop(code, None)
        if not rec:
            return False, "密钥不存在或已被撤销"
        self.save()
        if self.log:
            self.log.info(f"撤销邀请码 {mask_code(code)}")
        return True, None

    def invite_state(self, rec: dict) -> str:
        if rec.get("used_at"):
            # 已用，但绑的用户没了 —— 单独一种状态，好让管理台说清楚
            return "orphaned" if self.is_orphan(rec) else "used"
        if rec.get("expires_at", 0) < now():
            return "expired"
        return "pending"

    def list_invites(self, include_orphans: bool = False) -> list:
        """邀请码列表。

        `include_orphans=False`（默认）时**隐藏**"用户已被删除"的记录：
        这些记录**不会删除**（用户明确要求保留历史），只是默认不显示，
        免得列表里堆一堆绑给不存在的人的密钥。管理台可以勾选显示。
        """
        out = []
        for rec in sorted(self.invites.values(),
                          key=lambda r: r.get("created_at", 0), reverse=True):
            state = self.invite_state(rec)
            if state == "orphaned" and not include_orphans:
                continue
            item = dict(rec)
            item["code_masked"] = mask_code(rec.get("code", ""))
            item["state"] = state
            out.append(item)
        return out

    def orphan_invites(self) -> list:
        """只看"绑的用户已删除"的那些记录（给管理台提示用，不删）。"""
        out = []
        for rec in self.invites.values():
            if self.is_orphan(rec):
                item = dict(rec)
                item["code_masked"] = mask_code(rec.get("code", ""))
                item["state"] = "orphaned"
                out.append(item)
        return out

    # ------------------------------------------------ 激活

    def activate(self, code: str, fingerprint: str, device_name: str = ""):
        """用邀请码激活一台设备。返回 (result, error)。"""
        code = normalize_code(code)
        if not code:
            return None, "请填写密钥"
        rec = self.invites.get(code)
        if rec is None:
            return None, "密钥不存在（注意区分大小写，或确认没有多打空格）"
        if rec.get("used_at"):
            return None, "这个密钥已经被使用过了（每个密钥只能用一次）"
        if rec.get("expires_at", 0) < now():
            return None, "密钥已过期（有效期 24 小时），请向管理员要一个新的"

        username = rec.get("username") or ""
        err = check_username(username)
        if err:
            return None, f"该密钥绑定的用户名不合法：{err}"

        user = self.users.get(username)
        if user:
            if user.get("status") == "disabled":
                return None, f"用户名「{username}」已被停用，请联系管理员"
            if user.get("device_token_hash"):
                return None, f"用户名「{username}」已经绑定过设备了，请联系管理员解绑"

        token = secrets.token_urlsafe(32)
        fp_hash = _sha(fingerprint or "")
        self.users[username] = {
            "username": username,
            "status": "active",
            "device_token_hash": _sha(token),
            "device_hash": fp_hash,
            "device_name": (device_name or "")[:64],
            "note": rec.get("note", ""),
            "bound_at": int(now()),
            "last_seen": int(now()),
            "last_ip": None,
            # ---- 管理员在控制台「客户端」页要看的那些 ----
            "invite_code": code,          # 用的是哪张邀请码激活的
            "invite_masked": mask_code(code),
            "nickname": None,             # 客户端自己填的昵称（连上来之后回填）
            "client_version": None,       # 客户端版本号
            "machine": (device_name or "")[:64],
        }
        rec["used_at"] = int(now())
        rec["used_by"] = username
        self.save()
        if self.log:
            self.log.info(f"用户 {username} 激活成功（设备 {device_name or '未命名'}）")
        return {"device_token": token, "username": username}, None

    # ------------------------------------------------ 校验

    def check_device(self, device_token: str, fingerprint: str, ip: str | None = None):
        """校验设备令牌。返回 (username, status, reason)。"""
        if not device_token:
            return None, None, "尚未激活"
        h = _sha(device_token)
        for name, u in self.users.items():
            stored = u.get("device_token_hash") or ""
            if stored and hmac.compare_digest(stored, h):
                if u.get("status") != "active":
                    return name, u.get("status"), "账号已被管理员停用"
                dh = u.get("device_hash") or ""
                if dh and not hmac.compare_digest(dh, _sha(fingerprint or "")):
                    return name, u.get("status"), "设备不匹配（授权被复制到其他电脑了）"
                u["last_seen"] = int(now())
                if ip:
                    u["last_ip"] = ip
                self.touch()
                return name, "active", None
        return None, None, "授权无效或已被撤销"

    # ------------------------------------------------ 管理

    def set_status(self, username: str, status: str):
        username = (username or "").strip()
        err = check_username(username)
        if err:
            return False, err
        if status not in ("active", "disabled"):
            return False, "状态只能是 active 或 disabled"
        u = self.users.get(username)
        if u is None:
            if status == "active":
                return False, "用户不存在"
            # 允许预先停用一个"还没激活"的用户名 —— 相当于加入黑名单，
            # 这样管理员可以在发出邀请码后又反悔，而不用去追那张码。
            self.users[username] = {
                "username": username, "status": "disabled",
                "device_token_hash": None, "device_hash": None, "device_name": None,
                "note": "（尚未激活，已被预先停用）", "bound_at": None,
                "last_seen": None, "last_ip": None,
            }
            self.save()
            if self.log:
                self.log.info(f"用户名 {username} 已被预先停用（尚未激活）")
            return True, None
        u["status"] = status
        self.save()
        if self.log:
            self.log.info(f"用户 {username} 状态改为 {status}")
        return True, None

    def unbind(self, username: str):
        u = self.users.get(username)
        if not u:
            return False, "用户不存在"
        u["device_token_hash"] = None
        u["device_hash"] = None
        u["device_name"] = None
        u["bound_at"] = None
        self.save()
        if self.log:
            self.log.info(f"用户 {username} 已解绑设备（可以重新用邀请码激活）")
        return True, None

    def delete_user(self, username: str):
        """删掉用户，并把他的邀请码**标记成历史记录**（不删除）。

        这里改过两次语义，记一下免得再来回改：

        1. 最初只删用户、邀请码原样留着 -> 管理台里一直挂着一条绑给
           "已经不存在的人"的密钥（用户反馈"删除用户后邀请码依然存在"）。
        2. 于是改成连邀请码一起删 -> 能清干净，但**历史就没了**。
        3. 现在（用户要求"更新不要删过去邀请码"）：**记录保留，只是标记**。
           邀请码绑在用户名上，标记 `orphaned_at` 之后：
             - 默认列表不显示它（list_invites 过滤掉），列表干净
             - 勾选"显示历史"还能翻出来，数据一条不丢
             - 已用过的密钥本来就作废，留着无害
             - 没用过的待用密钥则**直接撤销**（防止拿它把已删的用户再激活回来）

        返回 (ok, error, detail)。detail 里说明标记/撤销了几张，好让管理台讲清楚。
        """
        username = (username or "").strip()
        if not username:
            return False, "用户名不能为空", None
        u = self.users.get(username)
        if u is None:
            return False, "用户不存在", None

        nickname = u.get("nickname") or ""
        bound_code = u.get("invite_code") or ""
        stamp = int(now())

        # 用户名对得上的 + 用户记录里记着的那张（兜底，防历史数据不一致）
        related = [c for c, r in self.invites.items()
                   if (r.get("username") or "") == username
                   or (bound_code and c == bound_code)]
        kept, revoked = [], []
        for code in related:
            rec = self.invites.get(code)
            if rec is None:
                continue
            if rec.get("used_at"):
                # 已用过的：留档，标记成"用户已删除"
                rec["orphaned_at"] = stamp
                rec["orphaned_note"] = f"用户 {username} 已删除（{stamp}）"
                kept.append(mask_code(code))
            else:
                # 还没用过的：撤掉，别让它把已删的用户再激活回来
                self.invites.pop(code, None)
                revoked.append(mask_code(code))

        self.users.pop(username, None)
        self.save()
        if self.log:
            bits = []
            if kept:
                bits.append(f"保留 {len(kept)} 张历史邀请码")
            if revoked:
                bits.append(f"撤销 {len(revoked)} 张未使用的邀请码")
            self.log.info(f"用户 {username} 已删除" + ("（" + "，".join(bits) + "）"
                                                       if bits else ""))
        detail = {
            "username": username,
            "nickname": nickname,
            # 兼容旧字段名：管理台用它显示"清理了几张"
            "invites_removed": len(kept) + len(revoked),
            "invites_kept": len(kept),
            "invites_revoked": len(revoked),
            "invites_masked": kept + revoked,
        }
        return True, None, detail

    def list_users(self) -> list:
        return sorted(self.users.values(), key=lambda u: u.get("bound_at") or 0,
                      reverse=True)

    def stats(self) -> dict:
        users = list(self.users.values())
        pend = sum(1 for r in self.invites.values()
                   if self.invite_state(r) == "pending")
        used = sum(1 for r in self.invites.values() if r.get("used_at"))
        orphan = sum(1 for r in self.invites.values() if self.is_orphan(r))
        return {
            "users": len(users),
            "active": sum(1 for u in users if u.get("status") == "active"),
            "disabled": sum(1 for u in users if u.get("status") == "disabled"),
            "invites_pending": pend,
            "invites_used": used,
            "invites_orphan": orphan,
            "required": self.required,
        }

    def set_required(self, value: bool) -> None:
        self.required = bool(value)
        self.save()
        if self.log:
            self.log.info("授权校验已" + ("开启" if self.required else "关闭"))


class RateLimiter:
    """按 IP 限制激活失败的频率，防止有人暴力猜邀请码。"""

    def __init__(self, limit: int = MAX_ACTIVATE_FAILS, window: float = 60.0):
        self.limit = limit
        self.window = window
        self.hits: dict[str, list] = {}

    def blocked(self, key: str) -> bool:
        t = now()
        arr = [x for x in self.hits.get(key, []) if t - x < self.window]
        self.hits[key] = arr
        return len(arr) >= self.limit

    def fail(self, key: str) -> None:
        self.hits.setdefault(key, []).append(now())

    def clear(self, key: str) -> None:
        self.hits.pop(key, None)

    def sweep(self) -> None:
        t = now()
        for k in list(self.hits):
            arr = [x for x in self.hits[k] if t - x < self.window]
            if arr:
                self.hits[k] = arr
            else:
                self.hits.pop(k, None)

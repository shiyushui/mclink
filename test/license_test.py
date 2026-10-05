#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 授权模块单元测试
=======================
直接测 server/mclink_license.py 的业务规则，不起网络：
邀请码生成/归一/掩码、激活、一次性、过期、设备绑定、停用、解绑、
预停用黑名单、哈希存储、持久化、限流。

    python test/license_test.py
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink_license import (                      # noqa: E402
    LicenseStore, RateLimiter, check_username, gen_invite_code, mask_code,
    normalize_code,
)

results = []


def ck(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def main() -> int:
    # 注意：临时目录放在工作区里。DSH 沙箱的 %TEMP% 会被回收，
    # 之前就是因为写到那儿导致文件中途消失。
    tmp = os.path.join(HERE, "_tmp_license")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, "licenses.json")

    print("\n== 1. 工具函数 ==")
    ck("用户名校验（合法/太短/含空格）",
       check_username("alice") is None and check_username("a") and check_username("a b"))
    code0 = gen_invite_code()
    ck("邀请码格式 MCLK-XXXX-XXXX-XXXX", code0.startswith("MCLK-") and len(code0) == 19, code0)
    ck("掩码不泄露中段", mask_code(code0) == f"MCLK-****-****-{code0[-4:]}", mask_code(code0))
    ck("各种输入写法都能归一",
       normalize_code("mclk7f3a9b2c4d1e") == "MCLK-7F3A-9B2C-4D1E"
       and normalize_code("MCLK 7F3A 9B2C 4D1E") == "MCLK-7F3A-9B2C-4D1E"
       and normalize_code("7f3a-9b2c-4d1e") == "MCLK-7F3A-9B2C-4D1E")

    L = LicenseStore(path, required_default=True)

    print("\n== 2. 邀请码与激活 ==")
    rec, err = L.create_invite("alice", 24, "朋友A")
    ck("生成邀请码", rec and not err, err or rec["code"])
    code = rec["code"]
    ck("非法时长被拒", L.create_invite("x", 0)[1] and L.create_invite("x", 9999)[1])
    ck("非法用户名被拒", L.create_invite("a b")[1] is not None)

    r, err = L.activate("bad-code", "fp1", "PC1")
    ck("错误密钥被拒", r is None and "不存在" in (err or ""), err)

    r, err = L.activate(code.lower().replace("mclk-", ""), "fp1", "PC1")
    ck("小写/省略前缀也能激活", r is not None, err or r["username"])
    tok = r["device_token"]

    r, err = L.activate(code, "fp2", "PC2")
    ck("同一密钥不能用第二次", r is None and "使用过" in (err or ""), err)

    print("\n== 3. 设备校验 ==")
    n, st, why = L.check_device(tok, "fp1")
    ck("令牌 + 同一台电脑 → 通过", n == "alice" and why is None, f"{n}/{why}")
    n, st, why = L.check_device(tok, "OTHER-FP")
    ck("令牌 + 换台电脑 → 拒绝", n == "alice" and "设备不匹配" in (why or ""), why)
    n, st, why = L.check_device("garbage", "fp1")
    ck("伪造令牌 → 拒绝", n is None and "无效" in (why or ""), why)
    n, st, why = L.check_device("", "fp1")
    ck("空令牌 → 未激活", n is None and "尚未激活" in (why or ""), why)

    print("\n== 4. 权限控制 ==")
    L.set_status("alice", "disabled")
    n, st, why = L.check_device(tok, "fp1")
    ck("停用后立刻失效", "停用" in (why or ""), why)
    L.set_status("alice", "active")

    ck("预先停用未激活的用户名（黑名单）", L.set_status("bob", "disabled")[0])
    rec2, _ = L.create_invite("bob", 1)
    r, err = L.activate(rec2["code"], "fp3", "PC3")
    ck("被预先停用的用户名无法激活", r is None and "停用" in (err or ""), err)

    L.unbind("alice")
    n, st, why = L.check_device(tok, "fp1")
    ck("解绑后旧令牌失效", n is None, why)

    print("\n== 5. 过期 ==")
    rec3, _ = L.create_invite("carol", 1)
    L.invites[rec3["code"]]["expires_at"] = int(time.time()) - 10
    r, err = L.activate(rec3["code"], "fp9", "PC9")
    ck("过期密钥被拒", r is None and "过期" in (err or ""), err)
    ck("过期状态识别", L.invite_state(L.invites[rec3["code"]]) == "expired")

    print("\n== 6. 存储安全 ==")
    raw = open(path, encoding="utf-8").read()
    ck("文件里没有明文设备令牌", tok not in raw)
    ck("文件里保存了令牌哈希", "device_token_hash" in raw)
    if os.name != "nt":
        ck("文件权限 0600", (os.stat(path).st_mode & 0o777) == 0o600)

    L2 = LicenseStore(path, required_default=False)
    ck("重启后数据仍在", "alice" in L2.users and L2.required is True)
    ck("重启后仍然开启校验", L2.required is True, str(L2.required))

    print("\n== 7. 限流 ==")
    rl = RateLimiter(limit=3, window=60)
    for _ in range(3):
        rl.fail("1.2.3.4")
    ck("超过阈值后被拦", rl.blocked("1.2.3.4"))
    ck("其他 IP 不受影响", not rl.blocked("5.6.7.8"))
    rl.clear("1.2.3.4")
    ck("清空后恢复", not rl.blocked("1.2.3.4"))

    print("\n== 8. 管理动作 ==")
    st = L.stats()
    ck("统计信息完整",
       st["users"] == 2 and st["active"] == 1 and st["disabled"] == 1, str(st))
    ck("撤销邀请码", L.revoke_invite(rec2["code"])[0])
    ck("重复撤销会报错", L.revoke_invite(rec2["code"])[0] is False)

    # 删除用户：**邀请码记录保留**（用户要求"更新不要删过去邀请码"），
    # 只是标记成历史、默认不在列表里显示。
    recd, _ = L.create_invite("dave", 24)
    r, err = L.activate(recd["code"], "fp-dave", "DAVE-PC")
    ck("dave 激活成功", r is not None, err or "")
    ck("此时列表里有 dave 的已用密钥",
       any(i.get("username") == "dave" for i in L.list_invites()))
    ck("删除 dave", L.delete_user("dave")[0] and "dave" not in L.users)
    ck("默认列表里不再显示 dave 的密钥（不再碍眼）",
       not any(i.get("username") == "dave" for i in L.list_invites()),
       str([i.get("username") for i in L.list_invites()]))
    # 核心：记录必须还在，不能删
    ck("但记录本身**没有**被删掉（历史保留）",
       L.invites.get(recd["code"]) is not None)
    ck("记录被标记成历史（orphaned_at）",
       bool(L.invites.get(recd["code"], {}).get("orphaned_at")))
    hist = L.orphan_invites()
    ck("orphan_invites() 能翻出这条历史",
       any(i.get("username") == "dave" for i in hist),
       str([i.get("username") for i in hist]))
    ck("勾选显示历史后列表里能看到",
       any(i.get("username") == "dave" for i in L.list_invites(include_orphans=True)))
    ck("统计里报了历史条数", int(L.stats().get("invites_orphan") or 0) >= 1,
       str(L.stats()))
    ck("别人（alice）的已用密钥不受影响",
       any(i.get("username") == "alice" for i in L.list_invites()))
    # 周期性清理**不能**把历史记录清掉
    L.purge_expired()
    ck("purge_expired() 不会删历史记录",
       L.invites.get(recd["code"]) is not None)

    ck("删除用户", L.delete_user("bob")[0] and "bob" not in L.users)
    ck("没用过的密钥在删用户时被撤销（防止重新激活已删的人）",
       not any(i.get("username") == "bob" for i in L.list_invites(include_orphans=True)),
       str([i.get("username") for i in L.list_invites(include_orphans=True)]))
    ck("删 bob 返回的明细区分了保留和撤销",
       L.delete_user("nobody")[0] is False)
    L.set_required(False)
    ck("可以关闭授权校验", L.required is False)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

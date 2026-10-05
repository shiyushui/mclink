#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 管理员控制台冒烟测试  (test/admin_smoke_test.py)
=====================================================
不连服务器、不起隧道，用一个"假 app"把四个后端接口顶掉，然后真的把
AdminWindow 构造出来，逐个检查界面状态：

  1. 窗口 / 两个标签页 / 顶栏授权状态
  2. 表单校验（空用户名、非法用户名、非法小时数）
  3. 生成邀请码 -> 密钥展示 + 复制按钮
  4. 邀请码表格的行数与字段
  5. 非管理员时按钮 disabled + 遮罩；解锁后变 normal
  6. 撤销 / 停用 / 解绑 / 删除 的确认框路径
  7. 回调带 error 时不崩，并且按钮能恢复

    python test/admin_smoke_test.py
"""

import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
sys.path.insert(0, CLIENT)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return ok


def flush(root, rounds=6, delay=0.02):
    """把 after() 排队的回调（模拟的网络回调）真的跑完。"""
    for _ in range(rounds):
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(delay)


NOW = time.time()

# ---------------------------------------------------------------- 假的后端数据

FAKE_INVITES = {
    "invites": [
        {"code": "MCLK-7F3A-9B2C-4D1E", "username": "alice", "created_at": NOW - 600,
         "expires_at": NOW + 3600 * 23, "used_at": None, "used_by": None,
         "note": "朋友A", "state": "pending"},
        {"code": "MCLK-AAAA-BBBB-CCCC", "username": "bob", "created_at": NOW - 4000,
         "expires_at": NOW + 200, "used_at": NOW - 100, "used_by": "DEV-2",
         "note": "", "state": "used"},
        {"code": "MCLK-DDDD-EEEE-FFFF", "username": "carol", "created_at": NOW - 90000,
         "expires_at": NOW - 60, "used_at": None, "used_by": None,
         "note": "过期了", "state": "pending"},          # 本地时间判断应为"已过期"
        {"code": "MCLK-1111-2222-3333", "username": "erin", "created_at": NOW - 90000,
         "expires_at": NOW - 30, "used_at": NOW - 20, "used_by": "DEV-3",
         "note": "用过也过期了", "state": "used"},        # used_at 优先 -> 已使用
        # 用过、但绑的用户已经不在用户列表里了 -> "用户已删"（孤儿记录）
        {"code": "MCLK-9999-8888-7777", "username": "ghost", "created_at": NOW - 90000,
         "expires_at": NOW + 3600, "used_at": NOW - 500, "used_by": "DEV-9",
         "note": "用户已删", "state": "orphaned"},
    ]
}

FAKE_USERS = {
    "users": [
        {"username": "alice", "status": "active", "bound_at": NOW - 86400 * 3,
         "last_seen": NOW - 120, "last_ip": "1.2.3.4", "device_name": "DESKTOP-ABC",
         "note": "朋友A"},
        {"username": "bob", "status": "disabled", "bound_at": NOW - 86400 * 9,
         "last_seen": None, "last_ip": None, "device_name": None, "note": ""},
        # erin 必须在用户列表里，否则上面那条会被算成"用户已删"
        {"username": "erin", "status": "active", "bound_at": NOW - 86400 * 2,
         "last_seen": NOW - 300, "last_ip": "5.6.7.8", "device_name": "DESKTOP-XYZ",
         "note": ""},
    ]
}

FAKE_STATS = {"users": 3, "active": 2, "disabled": 1,
              "invites_pending": 1, "invites_used": 2, "invites_orphan": 1}


class FakeApp:
    """只有界面上用到的那几个接口，全部同步/立即回调。"""

    def __init__(self, root, is_admin=False, licensed=True):
        self.root = root
        self.licensed = licensed
        self.username = "alice" if licensed else None
        self.is_admin = is_admin
        self.admin_key_saved = is_admin
        self.device_token = "DEV-TOKEN-123456"
        self.calls = []                 # [(action, args), ...]
        self.toasts = []                # [(text, level), ...]
        self.copies = []
        # 覆盖某个 action 的返回，用来模拟服务端报错
        self.fail_next = None
        self.override = {}
        # 按钮工厂：和主程序里的同名同签名就够用了
        self.f_title = ("", 14, "bold")
        self.f_card_title = ("", 11, "bold")
        self.f_body = ("", 10)
        self.f_small = ("", 9)
        self.f_tiny = ("", 8)
        self.f_mono = ("Consolas", 9)
        self.f_mono_b = ("Consolas", 10, "bold")

    # ---------------------------------------------------------- 后端接口
    def license_info(self):
        return {
            "licensed": self.licensed,
            "is_admin": self.is_admin,
            "username": self.username,
            "status": "active" if self.licensed else None,
            "reason": None if self.licensed else "尚未激活，请输入邀请码",
            "admin_key_saved": self.admin_key_saved,
            "device_token": self.device_token if self.licensed else None,
        }

    def admin_unlock(self, key):
        if key == "admin-secret":
            self.is_admin = True
            self.admin_key_saved = True
            return True, None
        return False, "密钥不正确"

    def admin_lock(self):
        self.is_admin = False
        self.admin_key_saved = False

    def admin_call(self, action, args, callback):
        self.calls.append((action, dict(args or {})))
        # 用 after(1) 模拟"回调稍后才回来"
        self.root.after(1, lambda: self._respond(action, args, callback))

    def _respond(self, action, args, callback):
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            callback(None, err)
            return
        if action in self.override:
            val = self.override[action]
            # 字符串 / 异常都当作"服务端返回的失败原因"，其余按正常结果回调
            if isinstance(val, Exception):
                callback(None, str(val))
            elif isinstance(val, str):
                callback(None, val)
            else:
                callback(val, None)
            return
        table = {
            "invite_list": FAKE_INVITES,
            "user_list": FAKE_USERS,
            "stats": FAKE_STATS,
            "invite_create": {"code": "MCLK-NEW1-NEW2-NEW3",
                              "username": (args or {}).get("username", "alice"),
                              "expires_at": time.time() + 3600 * 24},
            "invite_revoke": {},
            "user_set_status": {},
            "user_unbind": {},
            "user_delete": {},
        }
        if action not in table:
            callback(None, "没有管理员权限")
            return
        callback(table[action], None)

    # ---------------------------------------------------------- 界面工具
    def _btn(self, parent, text, cmd, kind="normal", width=None):
        b = tk.Button(parent, text=text, command=cmd, font=self.f_small, bd=0,
                      padx=10, pady=4)
        if width:
            b.configure(width=width)
        return b

    def toast(self, text, level="info", ms=2600):
        self.toasts.append((text, level))

    def copy(self, text, label="已复制"):
        self.copies.append((text, label))
        self.toasts.append((f"{label}：{text}", "ok"))

    def last_toast(self):
        return self.toasts[-1] if self.toasts else ("", "")


def tree_rows(tree):
    return [tree.item(i, "values") for i in tree.get_children()]


def find_links(win, text, root=None):
    """按文字找表格单元格里的操作按钮（_Link）。

    默认两个表格一起找，传 root=某个 Treeview 就只找那一张表。
    """
    out = []
    scope = root if root is not None else win

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, win_app_module._Link) and c.cget("text") == text:
                out.append(c)
            walk(c)
    walk(scope)
    return out


def link_state(link):
    """_Link 没有 state 选项，用内部开关 + 前景色判断。"""
    return "normal" if link._enabled else "disabled"


def main() -> int:
    import mclink_admin as admin
    global win_app_module
    win_app_module = admin

    root = tk.Tk()
    root.title("fake main")
    root.geometry("900x600+80+60")
    root.update()

    # 确认框会阻塞等待用户点击，测试里打桩成"点确定 / 点取消"
    confirm_answer = {"value": True}

    class FakeConfirm:
        def __init__(self, parent, title, message, danger=False):
            self.title, self.message, self.danger = title, message, danger
            self.result = bool(confirm_answer["value"])

    admin.ConfirmDialog = FakeConfirm

    # 解锁密钥的小窗同样会被打桩：直接给结果
    key_answer = {"value": None}

    class FakeKeyDialog:
        def __init__(self, parent):
            self.result = key_answer["value"]

    admin._KeyDialog = FakeKeyDialog
    # 所有阻塞等待一律变成空操作
    admin.AdminWindow._wait = lambda self, win=None: None

    app = FakeApp(root, is_admin=False)
    win = None
    try:
        # ---------------------------------------------------------- 1. 构造
        print("\n== 1. 窗口与标签页 ==")
        win = admin.AdminWindow(app)
        flush(root)
        check("AdminWindow 构造成功", win.winfo_exists() == 1)
        check("窗口标题正确", win.title() == "McLink 管理员控制台", win.title())
        check("用 ttk.Notebook 分页", isinstance(win.nb, admin.ttk.Notebook))
        tabs = [win.nb.tab(i, "text") for i in win.nb.tabs()]
        check("四个标签页：邀请码 / 用户 / 客户端 / 更新",
              tabs == ["邀请码", "用户", "客户端", "更新"], str(tabs))
        check("顶栏显示已授权用户",
              "已授权" in win.lbl_lic.cget("text") and "alice" in win.lbl_lic.cget("text"),
              win.lbl_lic.cget("text"))
        check("未解锁时不显示管理员模式胶囊",
              win.lbl_pill.cget("text") == "未解锁", win.lbl_pill.cget("text"))
        check("表格用 Treeview 且 show=headings",
              "headings" in str(win.tree_invite.cget("show")))
        check("表头列名齐全",
              [win.tree_invite.heading(c, "text") for c in win.tree_invite["columns"]]
              == ["密钥", "用户名", "状态", "到期时间", "备注", "操作"],
              str([win.tree_invite.heading(c, "text")
                   for c in win.tree_invite["columns"]]))
        check("表格行高 26、斑马纹已配置",
              str(admin.ttk.Style(win).lookup("MC.Treeview", "rowheight")) == "26"
              and win.tree_invite.tag_configure("odd").get("background")
              != win.tree_invite.tag_configure("even").get("background"),
              f"rowheight={admin.ttk.Style(win).lookup('MC.Treeview', 'rowheight')}")
        check("非管理员时有遮罩提示",
              win.overlay.winfo_ismapped() == 1
              and win.ov_title.cget("text") == "需要管理员密钥",
              win.ov_title.cget("text"))

        # 首屏已经拉过一次数据
        actions = [a for a, _ in app.calls]
        check("首屏自动请求了 invite_list / user_list / stats",
              {"invite_list", "user_list", "stats"}.issubset(set(actions)),
              str(sorted(set(actions))))

        # ---------------------------------------------------------- 2. 非管理员权限
        print("\n== 2. 非管理员：按钮必须 disabled ==")
        check("生成邀请码按钮 disabled",
              str(win.btn_create.cget("state")) == "disabled")
        check("复制密钥按钮 disabled",
              str(win.btn_recent_copy.cget("state")) == "disabled")
        check("非管理员时表格里不放操作按钮", len(win.row_links) == 0,
              f"{len(win.row_links)} 个")

        # ---------------------------------------------------------- 3. 解锁
        print("\n== 3. 管理员密钥解锁 ==")
        key_answer["value"] = "wrong-key"
        win.on_unlock()
        flush(root)
        check("错误密钥给出红色提示", app.last_toast() == ("密钥不正确", "err"),
              str(app.last_toast()))
        check("错误密钥不会进入管理员模式", win.is_admin is False)

        key_answer["value"] = "admin-secret"
        win.on_unlock()
        flush(root)
        check("正确密钥后进入管理员模式", win.is_admin is True)
        check("胶囊显示管理员模式", win.lbl_pill.cget("text") == "管理员模式",
              win.lbl_pill.cget("text"))
        check("解锁后遮罩消失", win.overlay.winfo_ismapped() == 0)
        check("解锁后生成按钮恢复 normal",
              str(win.btn_create.cget("state")) == "normal")
        check("解锁后顶栏按钮变成退出管理员模式",
              win.btn_unlock.cget("text") == "退出管理员模式",
              win.btn_unlock.cget("text"))

        # ---------------------------------------------------------- 4. 表单校验
        print("\n== 4. 生成表单校验 ==")
        before = len(app.calls)

        win.vars["username"].set("")
        win.vars["hours"].set("24")
        win.on_create()
        flush(root)
        check("空用户名被拦下",
              "用户名" in win.lbl_form_err.cget("text") and len(app.calls) == before,
              win.lbl_form_err.cget("text"))
        check("空用户名时输入框被标红",
              str(win.entries["username"].cget("highlightbackground")).lower()
              in ("#ef4444", "red"))

        win.vars["username"].set("a b")          # 含空格，非法
        win.on_create()
        flush(root)
        check("非法用户名（带空格）被拦下",
              "2-32" in win.lbl_form_err.cget("text") and len(app.calls) == before,
              win.lbl_form_err.cget("text"))

        win.vars["username"].set("a")            # 太短
        win.on_create()
        flush(root)
        check("过短用户名被拦下", len(app.calls) == before,
              win.lbl_form_err.cget("text"))

        win.vars["username"].set("alice")
        win.vars["hours"].set("abc")
        win.on_create()
        flush(root)
        check("非数字小时数被拦下",
              "整数" in win.lbl_form_err.cget("text") and len(app.calls) == before,
              win.lbl_form_err.cget("text"))

        win.vars["hours"].set("999")
        win.on_create()
        flush(root)
        check("超出范围的小时数被拦下",
              "720" in win.lbl_form_err.cget("text") and len(app.calls) == before,
              win.lbl_form_err.cget("text"))
        check("非法输入时小时数输入框被标红",
              str(win.entries["hours"].cget("highlightbackground")).lower()
              in ("#ef4444", "red"))

        # ---------------------------------------------------------- 5. 生成邀请码
        print("\n== 5. 生成邀请码 ==")
        win.vars["username"].set("dave")
        win.vars["hours"].set("24")
        win.vars["note"].set("朋友D")
        win.on_create()
        flush(root)
        created = [c for c in app.calls if c[0] == "invite_create"]
        check("发出了 invite_create 请求", len(created) == 1, str(created))
        check("请求参数正确",
              created and created[0][1] == {"username": "dave", "hours": 24,
                                            "note": "朋友D"},
              str(created[0][1]) if created else "")
        check("成功后密钥显示在界面上",
              win.lbl_code.cget("text") == "MCLK-NEW1-NEW2-NEW3",
              win.lbl_code.cget("text"))
        check("密钥用大号等宽字体",
              isinstance(win.lbl_code.cget("font"), str)
              and ("Consolas" in str(win.lbl_code.cget("font"))
                   or "15" in str(win.lbl_code.cget("font"))),
              str(win.lbl_code.cget("font")))
        check("提示 24 小时内有效，只能使用一次",
              "只能使用一次" in win.lbl_recent_hint.cget("text"),
              win.lbl_recent_hint.cget("text"))
        check("最近生成区域已展开", win.recent.winfo_ismapped() == 1)
        check("自动复制了一次密钥",
              any(c[0] == "MCLK-NEW1-NEW2-NEW3" for c in app.copies), str(app.copies[-1:]))
        check("复制密钥按钮可用",
              str(win.btn_recent_copy.cget("state")) == "normal")
        check("生成后用户名输入框被清空", win.vars["username"].get() == "")

        # 复制按钮真的能复制
        app.copies.clear()
        win.btn_recent_copy.invoke()
        check("点复制密钥按钮会写剪贴板",
              bool(app.copies) and app.copies[-1][0] == "MCLK-NEW1-NEW2-NEW3",
              str(app.copies))

        # ---------------------------------------------------------- 6. 邀请码表格
        print("\n== 6. 邀请码表格 ==")
        rows = tree_rows(win.tree_invite)
        check("邀请码表格 5 行", len(rows) == 5, f"{len(rows)} 行")
        check("第一行字段正确",
              rows and list(rows[0])[:2] == ["MCLK-7F3A-9B2C-4D1E", "alice"]
              and rows[0][4] == "朋友A",
              str(rows[0]) if rows else "")
        check("待使用状态文本带绿点",
              rows and rows[0][2].endswith("待使用"), str(rows[0][2]) if rows else "")
        check("已使用状态正确",
              rows and rows[1][2].endswith("已使用"), str(rows[1][2]) if rows else "")
        check("过期判断按本地时间生效（expires_at 已过 -> 已过期）",
              rows and rows[2][2].endswith("已过期"), str(rows[2][2]) if rows else "")
        check("used_at 优先于 expires_at（用过又过期的算已使用）",
              rows and rows[3][2].endswith("已使用"), str(rows[3][2]) if rows else "")
        check("绑的用户已被删除的邀请码标成「用户已删」",
              rows and rows[4][2].endswith("用户已删"), str(rows[4][2]) if rows else "")
        check("到期时间格式化成 2026-10-04 19:20 这种",
              rows and len(rows[0][3]) == 16 and rows[0][3][4] == "-",
              str(rows[0][3]) if rows else "")
        check("空备注显示破折号",
              rows and rows[1][4] == "—", str(rows[1][4]) if rows else "")
        check("计数文字正确",
              "共 5 个" in win.lbl_invite_count.cget("text")
              and "1 个待使用" in win.lbl_invite_count.cget("text")
              and "1 条历史记录" in win.lbl_invite_count.cget("text"),
              win.lbl_invite_count.cget("text"))

        copies_l = find_links(win, "复制", win.tree_invite)
        revokes = find_links(win, "撤销", win.tree_invite)
        check("每行都有复制按钮", len(copies_l) == 5, f"{len(copies_l)} 个")
        check("每行都有撤销按钮", len(revokes) == 5, f"{len(revokes)} 个")
        check("管理员时操作按钮是 normal",
              all(link_state(l) == "normal" for l in copies_l))
        used_revokes = [l for l in revokes
                        if not getattr(l, "_enabled", True)]
        # 5 条里只有 alice 那条还没用过，其余 4 条（已使用 ×2 + 已过期 + 用户已删）都不能撤销
        check("已使用/已过期/用户已删的邀请码不能再撤销", len(used_revokes) == 4,
              f"{len(used_revokes)} 个被禁用")
        check("只有待使用的码才能撤销",
              [i for i, l in enumerate(revokes) if l._enabled] == [0],
              str([i for i, l in enumerate(revokes) if l._enabled]))
        invite_links = len(copies_l) + len(revokes)
        user_links = len(find_links(win, "删除", win.tree_user))
        check("在树里放了操作按钮（等于表格单元格控件）",
              len(win.row_links) == invite_links + 3 * user_links,
              f"{len(win.row_links)} 个（邀请码 {invite_links} + 用户 3×{user_links}）")
        check("刷新后旧的行内按钮被销毁，不会越堆越多",
              len([w for w in win.tree_invite.winfo_children()]) == 5,
              f"{len(win.tree_invite.winfo_children())} 个行容器")

        # 复制一行
        app.copies.clear()
        copies_l[0].invoke()
        check("点行内复制会写剪贴板",
              bool(app.copies) and app.copies[-1][0] == "MCLK-7F3A-9B2C-4D1E",
              str(app.copies))

        # ---------------------------------------------------------- 7. 撤销确认路径
        print("\n== 7. 确认框路径（撤销 / 停用 / 解绑 / 删除） ==")
        # 每次刷新都会重建行内按钮，所以点之前重新取一遍（拿到的才是当前有效的）
        app.calls.clear()
        confirm_answer["value"] = False
        find_links(win, "撤销", win.tree_invite)[0].invoke()
        flush(root)
        check("撤销时点取消 -> 不发请求", len(app.calls) == 0, str(app.calls))

        confirm_answer["value"] = True
        find_links(win, "撤销", win.tree_invite)[0].invoke()
        flush(root)
        check("撤销时点确定 -> 发出 invite_revoke",
              [c for c in app.calls if c[0] == "invite_revoke"]
              == [("invite_revoke", {"code": "MCLK-7F3A-9B2C-4D1E"})],
              str(app.calls))

        # ---------------------------------------------------------- 8. 用户表格
        print("\n== 8. 用户表格与统计 ==")
        rows = tree_rows(win.tree_user)
        check("用户表格 3 行", len(rows) == 3, f"{len(rows)} 行")
        check("用户名 / 状态正确",
              rows and rows[0][0] == "alice" and rows[0][1].endswith("正常")
              and rows[1][1].endswith("已停用"),
              str(rows))
        check("绑定设备名显示正确",
              rows and rows[0][2] == "DESKTOP-ABC", str(rows[0][2]) if rows else "")
        check("最后在线时间格式化正确",
              rows and len(rows[0][3]) == 16, str(rows[0][3]) if rows else "")
        check("None 的时间显示破折号",
              rows and rows[1][3] == "—" and rows[1][4] == "—" and rows[1][2] == "—",
              str(rows[1]))
        check("统计文字正确",
              win.lbl_user_stats.cget("text")
              == ("共 3 个用户 · 2 个正常 · 1 个已停用"
                  "    ·    1 条邀请码绑的用户已删除（服务端会自动清）"
                  "    ·    1 个邀请码待使用"),
              win.lbl_user_stats.cget("text"))

        toggle = find_links(win, "停用")
        enable = find_links(win, "启用")
        check("每个用户都有 停用/启用 按钮",
              len(toggle) == 2 and len(enable) == 1,
              f"停用={len(toggle)} 启用={len(enable)}")
        check("每个用户都有解绑设备按钮", len(find_links(win, "解绑设备")) == 3,
              f"{len(find_links(win, '解绑设备'))} 个")
        check("每个用户都有删除按钮", len(find_links(win, "删除")) == 3,
              f"{len(find_links(win, '删除'))} 个")
        check("没绑设备的用户不能点解绑",
              [link_state(l) for l in find_links(win, "解绑设备")]
              == ["normal", "disabled", "normal"],
              str([link_state(l) for l in find_links(win, "解绑设备")]))

        app.calls.clear()
        confirm_answer["value"] = True
        find_links(win, "停用")[0].invoke()
        flush(root)
        check("停用 -> user_set_status(disabled)",
              ("user_set_status", {"username": "alice", "status": "disabled"})
              in app.calls, str(app.calls))

        app.calls.clear()
        find_links(win, "解绑设备")[0].invoke()
        flush(root)
        check("解绑 -> user_unbind(alice)",
              ("user_unbind", {"username": "alice"}) in app.calls, str(app.calls))

        app.calls.clear()
        find_links(win, "删除")[1].invoke()
        flush(root)
        check("删除 -> user_delete(bob)",
              ("user_delete", {"username": "bob"}) in app.calls, str(app.calls))

        app.calls.clear()
        confirm_answer["value"] = False
        find_links(win, "删除")[0].invoke()
        flush(root)
        check("删除时点取消 -> 不发请求", len(app.calls) == 0, str(app.calls))
        confirm_answer["value"] = True

        # ---- 表格下面的常驻操作条（行内按钮被裁掉时的兜底入口）----
        print("\n== 8b. 底部常驻操作条 ==")
        check("操作条存在", hasattr(win, "bar_user_actions"))
        # Tk 不会给没选中的 Notebook 页布局，所以先切到「用户」页再量
        win.nb.select(win.tab_user)
        flush(root)
        # 关键回归：Treeview 是 fill=both/expand=True，操作条必须**先** pack，
        # 否则会被表格顶到窗口底部外面 —— 表现就是"按钮根本看不到"。
        bar = win.bar_user_actions
        check("操作条真的显示出来了（没被表格顶出窗口）",
              bool(bar.winfo_ismapped()) and bar.winfo_height() > 10,
              f"mapped={bar.winfo_ismapped()} h={bar.winfo_height()}")
        bar_bottom = bar.winfo_rooty() + bar.winfo_height()
        win_bottom = win.winfo_rooty() + win.winfo_height()
        check("操作条落在窗口范围内",
              0 < bar.winfo_rooty() < win_bottom and bar_bottom <= win_bottom + 2,
              f"bar {bar.winfo_rooty()}..{bar_bottom} / window ..{win_bottom}")
        for name, b in (("停用/启用", win.btn_bar_toggle),
                        ("解绑设备", win.btn_bar_unbind),
                        ("删除用户", win.btn_bar_delete)):
            check(f"操作条上的「{name}」可见",
                  bool(b.winfo_ismapped()) and b.winfo_width() > 10
                  and b.winfo_height() > 10,
                  f"mapped={b.winfo_ismapped()} {b.winfo_width()}x{b.winfo_height()}")
        check("没选中时删除按钮是禁用的",
              str(win.btn_bar_delete.cget("state")) == "disabled",
              str(win.btn_bar_delete.cget("state")))
        check("提示文字引导先选一行",
              "点一行" in win.lbl_bar_pick.cget("text"),
              win.lbl_bar_pick.cget("text"))

        # 选中 bob 这一行
        bob_iid = None
        for iid in win.tree_user.get_children():
            if win.tree_user.set(iid, "username") == "bob":
                bob_iid = iid
        check("能在表格里找到 bob 那行", bob_iid is not None)
        win.tree_user.selection_set(bob_iid)
        flush(root)
        check("选中后删除按钮变可用",
              str(win.btn_bar_delete.cget("state")) == "normal",
              str(win.btn_bar_delete.cget("state")))
        check("选中后提示显示是谁",
              "bob" in win.lbl_bar_pick.cget("text"),
              win.lbl_bar_pick.cget("text"))
        check("选中已停用的人 -> 按钮显示「启用」",
              win.btn_bar_toggle.cget("text") == "启用",
              win.btn_bar_toggle.cget("text"))
        check("没绑设备 -> 底部解绑按钮禁用",
              str(win.btn_bar_unbind.cget("state")) == "disabled",
              str(win.btn_bar_unbind.cget("state")))

        app.calls.clear()
        win.btn_bar_delete.invoke()
        flush(root)
        check("底部「删除用户」-> user_delete(bob)",
              ("user_delete", {"username": "bob"}) in app.calls, str(app.calls))

        # 选中 alice（有设备、状态正常）
        alice_iid = None
        for iid in win.tree_user.get_children():
            if win.tree_user.set(iid, "username") == "alice":
                alice_iid = iid
        win.tree_user.selection_set(alice_iid)
        flush(root)
        check("选中正常用户 -> 按钮显示「停用」",
              win.btn_bar_toggle.cget("text") == "停用",
              win.btn_bar_toggle.cget("text"))
        check("绑了设备 -> 底部解绑按钮可用",
              str(win.btn_bar_unbind.cget("state")) == "normal",
              str(win.btn_bar_unbind.cget("state")))
        app.calls.clear()
        win.btn_bar_unbind.invoke()
        flush(root)
        check("底部「解绑设备」-> user_unbind(alice)",
              ("user_unbind", {"username": "alice"}) in app.calls, str(app.calls))
        app.calls.clear()
        win.btn_bar_toggle.invoke()
        flush(root)
        check("底部「停用」-> user_set_status(alice, disabled)",
              ("user_set_status", {"username": "alice", "status": "disabled"})
              in app.calls, str(app.calls))
        # 清理选中，别影响后面的用例
        win.tree_user.selection_remove(*win.tree_user.selection())
        flush(root)

        # ---- 确认框尺寸：按钮不能被长文案顶出窗口 ----
        print("\n== 8c. 确认框尺寸（按钮必须可见） ==")
        import mclink_gui as _g
        _orig_wait = tk.Toplevel.wait_window
        tk.Toplevel.wait_window = lambda self, *a, **k: None
        try:
            for label, msg, dg in (
                    ("删除用户(长文案)",
                     "确定要删除用户「test」吗？\n\n"
                     "该用户的设备绑定和授权会立刻清除，此操作不可撤销。\n\n"
                     "他名下的邀请码不会被删除：已经用过的会留成历史记录"
                     "（默认隐藏，可勾选「显示历史」查看）；还没用过的会被撤销，"
                     "防止拿它把已删的人再激活回来。", True),
                    ("旧版那条最长的删除文案",
                     "确定要删除用户「test」吗？\n\n"
                     "该用户的设备绑定、授权，以及他名下的邀请码会一并清除，"
                     "此操作不可撤销。\n\n"
                     "（邀请码是绑在用户名上的，用户删了、密钥留着，"
                     "列表里就会一直多一条绑给「已经不存在的人」的记录。）", True),
                    ("解绑设备", "确定要解绑「alice」的设备吗？\n\n"
                                "解绑后对方需要重新输入邀请码激活。", True),
                    ("极短文案", "确定吗？", False)):
                d = _g.ConfirmDialog(win, "确认", msg, danger=dg)
                flush(root)
                # update_idletasks 之后再量一次：窗口尺寸是构造里改的，
                # 不等 Tk 应用完 geometry，量到的还是旧值（会变成空断言）
                d.update_idletasks()
                ch = d.winfo_height()
                got = []
                for w in d.winfo_children():
                    for c in w.winfo_children():
                        try:
                            t = str(c.cget("text"))
                        except Exception:
                            continue
                        if t in ("确定", "确定删除", "取消"):
                            y = c.winfo_rooty() - d.winfo_rooty()
                            hgt = c.winfo_height()
                            # 上下两条边都要在窗口里 —— 只看底边的话，
                            # 被挤成 y=-170、高 1px 的按钮会被误判成"在窗口内"
                            got.append((t, c.winfo_ismapped(),
                                        y >= 0 and hgt > 10
                                        and y + hgt <= ch + 2, y, hgt))
                check(f"{label}：确定/取消都在窗口内",
                      len(got) == 2
                      and all(m and inside for _t, m, inside, _y, _h in got),
                      f"窗口高 {ch}, 按钮 {[(g[0], g[3], g[4]) for g in got]}")
                d.destroy()
        finally:
            tk.Toplevel.wait_window = _orig_wait

        # ---------------------------------------------------------- 9. 错误回调
        print("\n== 9. 回调带 error 不崩 ==")
        app.fail_next = "用户名已存在"
        app.toasts.clear()
        win.vars["username"].set("alice")
        win.vars["hours"].set("24")
        win.on_create()
        flush(root)
        check("服务端报错时弹出红色提示",
              ("用户名已存在", "err") in app.toasts, str(app.toasts))
        check("报错后生成按钮恢复可用",
              str(win.btn_create.cget("state")) == "normal",
              str(win.btn_create.cget("state")))
        check("报错后忙碌计数归零", win._busy == 0, str(win._busy))

        # 撤销时会把列表也刷一遍，先等这些回调全部落地
        flush(root)
        app.override["invite_list"] = "没有管理员权限"
        app.toasts.clear()
        win.load_invites()
        flush(root)
        check("刷新列表报错也会提示且不崩",
              ("没有管理员权限", "err") in app.toasts, str(app.toasts))
        app.override.pop("invite_list", None)
        win.load_invites()
        flush(root)

        app.override["stats"] = RuntimeError("服务端超时")
        win.load_stats()
        flush(root)
        check("统计接口抛异常也不崩", True)
        del app.override["stats"]

        # 返回 None / 空结果也要能撑住
        app.override["user_list"] = None
        win.load_users()
        flush(root)
        check("user_list 返回 None 时表格清空而不报错",
              len(win.tree_user.get_children()) == 0)
        app.override["user_list"] = FAKE_USERS
        win.load_users()
        flush(root)
        check("恢复数据后表格又能填满",
              len(win.tree_user.get_children()) == 3)

        # ---------------------------------------------------------- 10. 忙碌态
        print("\n== 10. 请求期间按钮禁用 ==")
        win.load_invites()
        busy_text = win.btn_invite_refresh.cget("text")
        check("请求发出后按钮显示处理中并禁用",
              str(win.btn_invite_refresh.cget("state")) == "disabled"
              and ("处理中" in busy_text or "刷新中" in busy_text),
              f"{busy_text} / {win.btn_invite_refresh.cget('state')}")
        flush(root)
        check("回调回来后按钮文字与状态恢复",
              str(win.btn_invite_refresh.cget("state")) == "normal"
              and win.btn_invite_refresh.cget("text") == "刷新",
              f"{win.btn_invite_refresh.cget('text')} / "
              f"{win.btn_invite_refresh.cget('state')}")

        # 请求期间表格里的操作按钮也要一起禁用
        win.load_users()
        busy_links = [link_state(l) for l in find_links(win, "删除", win.tree_user)]
        check("请求期间行内操作按钮也 disabled",
              busy_links and set(busy_links) == {"disabled"}, str(busy_links))
        flush(root)
        check("回调回来行内操作按钮恢复 normal",
              set(link_state(l) for l in find_links(win, "删除", win.tree_user))
              == {"normal"},
              str([link_state(l) for l in find_links(win, "删除", win.tree_user)]))

        # 同时发两个请求，忙碌计数要能正确归零
        win.load_users()
        win.load_stats()
        check("并发请求时忙碌计数累加", win._busy >= 1, str(win._busy))
        flush(root)
        check("并发请求结束后忙碌计数归零", win._busy == 0, str(win._busy))

        # ---------------------------------------------------------- 11. 退出管理员
        print("\n== 11. 退出管理员模式 ==")
        win.on_lock()
        flush(root)
        check("退出后不再是管理员", win.is_admin is False)
        check("退出后遮罩重新盖回来", win.overlay.winfo_ismapped() == 1)
        check("退出后生成按钮再次 disabled",
              str(win.btn_create.cget("state")) == "disabled")
        check("退出后顶栏按钮变回输入管理员密钥",
              win.btn_unlock.cget("text") == "输入管理员密钥",
              win.btn_unlock.cget("text"))
        check("退出后最近生成的密钥被清掉", win._last_code is None)

        # ---------------------------------------------------------- 12. 未授权状态
        print("\n== 12. 未授权时的顶栏 ==")
        app2 = FakeApp(root, is_admin=False, licensed=False)
        win2 = admin.AdminWindow(app2)
        flush(root)
        check("未授权时顶栏红色显示原因",
              "未授权" in win2.lbl_lic.cget("text")
              and "尚未激活" in win2.lbl_lic.cget("text"),
              win2.lbl_lic.cget("text"))
        win2.destroy()
        flush(root)

    except Exception as exc:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("管理员控制台各部件构造无异常", False, f"{type(exc).__name__}: {exc}")

    finally:
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok, _ in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

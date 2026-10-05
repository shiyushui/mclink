#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 管理员控制台  (mclink_admin.py)
=====================================
"授权分发"的可视化界面：管理员把软件发给朋友，朋友用一次性密钥激活；
管理员用管理员密钥进入管理员模式，在这里发码、管人。

用法（主程序里只需要一行）：
    from mclink_admin import AdminWindow
    AdminWindow(self)                       # 自己会 grab_set()，返回时窗口已关闭

本模块只负责"界面 + 调后端"，所有真正的网络动作都交给 app：
    app.license_info()                 读当前授权状态（同步）
    app.admin_unlock(key) / admin_lock() 进入 / 退出管理员模式（同步）
    app.admin_call(action, args, cb)   发管理员请求（异步，回调在主线程）

设计约定（为了可测试）：
    * 被 import 时不会创建任何窗口、不会碰 tkinter。
    * 所有"会阻塞事件循环"的调用都集中在 _wait() 里，测试可以直接覆盖它。
    * 表格单元格里的按钮统一用 _Link（其实是按钮外观的 Label），
      避免 tkinter 的 command 名字解析在带下标的 lambda 上踩坑。
"""

from __future__ import annotations

import os
import re
import sys
import time
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 复用主程序的样式件
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mclink_gui import (BG, CARD, LINE, LINE_SOFT, TEXT, TEXT2, MUTED, MINT, SKY,
                        GREEN, AMBER, RED, BLUE, PURPLE, ConfirmDialog)  # noqa: E402

# ---------------------------------------------------------------- 常量

WIN_TITLE = "McLink 管理员控制台"
WIN_W, WIN_H = 860, 640

# 表格的两种底色，斑马纹用
ROW_A = CARD
ROW_B = "#f8fbff"
HEAD_BG = "#f1f5fa"          # 表头浅灰
SEL_BG = "#e3efff"           # 选中行

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")   # 用户名规则
HOURS_MIN, HOURS_MAX = 1, 720
DEFAULT_HOURS = "24"

# 状态 -> (显示文字, 颜色)。Treeview 没法给单个单元格上色，
# 所以文字里带一个"●"色块，再配合表格整行的斑马纹来做区分。
INVITE_STATE = {
    "used":     ("● 已使用", MUTED),
    "expired":  ("● 已过期", AMBER),
    "pending":  ("● 待使用", GREEN),
    # 已用过、但它绑的那个用户已经被删了。服务端会自己收拾掉，
    # 这里只是让管理员一眼看懂"这条为什么还在这儿"。
    "orphaned": ("● 用户已删", RED),
}
USER_STATE = {
    "active":   ("● 正常", GREEN),
    "disabled": ("● 已停用", RED),
}
NON_ADMIN_HINT = "需要管理员密钥"


def fmt_ts(ts) -> str:
    """时间戳 -> '2026-10-04 19:20'，空值给破折号。"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def invite_state_of(row: dict, now: float | None = None,
                    known_users: set | None = None) -> str:
    """算出邀请码现在的真实状态：待使用 / 已过期 / 已使用 / 用户已删。

    服务端返回的 state 可能与本地时间略有出入，这里以本地时间为准再算一遍：
    只要还没被使用、且已经过了到期时间，就算"已过期"。

    `known_users` 是当前用户列表里的用户名集合（可选）。给了它的话，
    已使用但绑的用户已经不存在的邀请码会算成 "orphaned" —— 这种记录
    服务端会自己清掉，但在清掉之前让管理员看懂状态比显示"已使用"更好。
    """
    if row.get("used_at"):
        name = row.get("username") or ""
        if known_users is not None and name and name not in known_users:
            return "orphaned"
        if str(row.get("state") or "").lower() == "orphaned":
            return "orphaned"
        return "used"
    exp = row.get("expires_at")
    now = time.time() if now is None else now
    try:
        if exp and float(exp) < now:
            return "expired"
    except (TypeError, ValueError):
        pass
    state = str(row.get("state") or "").lower()
    if state in ("used", "expired", "pending", "orphaned"):
        return state
    return "pending"


class _Link(tk.Label):
    """长得像按钮的 Label，放在 Treeview 单元格里当操作按钮用。

    不用 tk.Button 的原因有两个：
      1) 树里放真按钮不支持 state="disabled"，而需求要求非管理员时"全部 disabled"；
      2) tkinter 解析 command 名字时会被带下标的 lambda 里的点号绊倒。
    这里自己管理启用状态，测试可以直接调 invoke() 模拟点击。
    """

    def __init__(self, parent, text, cmd, kind="ghost", bg=CARD):
        color = RED if kind == "danger" else TEXT2
        hover = "#fbdcdc" if kind == "danger" else "#e4ebf5"
        base = bg if kind == "ghost" else ("#fdecec" if kind == "danger" else "#eef2f8")
        super().__init__(parent, text=text, font=("", 9), bd=0, padx=6, pady=1,
                         bg=base, fg=color, cursor="hand2")
        self._cmd = cmd
        self._kind = kind
        self._bg = base
        self._hover = hover
        self._enabled = True
        self._allowed = True                       # 这个操作本身是否允许
        self._alive = True                         # 所在表格行还在不在
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    # ------------------------------------------------ 交互
    def _on_click(self, _evt=None):
        if not self._enabled:
            return
        try:
            self._cmd()
        except Exception:                     # noqa: BLE001 —— 单个按钮出错不该炸掉窗口
            import traceback
            traceback.print_exc()

    def _on_enter(self, _evt=None):
        if self._enabled:
            self.configure(bg=self._hover)

    def _on_leave(self, _evt=None):
        if self._enabled:
            self.configure(bg=self._bg)

    # ------------------------------------------------ 对外接口
    def invoke(self):
        """给测试用：等价于用户点了一下。"""
        self._on_click()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        if self._enabled:
            self.configure(fg=RED if self._kind == "danger" else TEXT2,
                           bg=self._bg, cursor="hand2")
        else:
            # 禁用时连底色一起去掉，免得看起来还能点
            self.configure(fg="#c3ccd8", bg="#f4f6fa", cursor="arrow")

    def set_allowed(self, allow: bool) -> None:
        """标记这个操作"本来就该不该给点"（比如已用过的码不能再撤销）。

        和 set_enabled 的区别：这里会再走一遍 update_permissions 的繁忙判断，
        不会被刷新时的 re-enable 覆盖掉。
        """
        self._allowed = bool(allow)
        self.set_enabled(self._allowed)

    def kill(self) -> None:
        """所在的表格行被销毁了：标记为失效，后续不再改它的状态。"""
        self._alive = False
        self._enabled = False


# ---------------------------------------------------------------- 输入管理员密钥

class _KeyDialog(tk.Toplevel):
    """输入管理员密钥的小窗。同步阻塞在 _wait() 里，直到用户确定或取消。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.title("输入管理员密钥")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(parent)

        holder = tk.Frame(self, bg=CARD)
        holder.pack(fill="both", expand=True, padx=24, pady=22)
        tk.Label(holder, text="管理员密钥", bg=CARD, fg=TEXT,
                 font=("", 11, "bold")).pack(anchor="w")
        tk.Label(holder, text="密钥只存在本机，解锁后下次启动会自动记住。",
                 bg=CARD, fg=MUTED, font=("", 9)).pack(anchor="w", pady=(4, 10))
        self.var = tk.StringVar()
        entry = tk.Entry(holder, textvariable=self.var, show="•", width=38,
                         bg="#fbfcfe", fg=TEXT, relief="flat", bd=0, font=("", 10),
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=SKY, insertbackground=TEXT)
        entry.pack(fill="x", ipady=6, ipadx=6)
        self.lbl_err = tk.Label(holder, text="", bg=CARD, fg=RED, font=("", 9))
        self.lbl_err.pack(anchor="w", pady=(6, 0))

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=24, pady=(0, 16))
        tk.Button(foot, text="解锁", command=self._ok, font=("", 10), bg=SKY, fg="white",
                  bd=0, relief="flat", padx=18, pady=6, cursor="hand2",
                  activebackground="#1ea3ec").pack(side="right")
        tk.Button(foot, text="取消", command=self._no, font=("", 10), bg=LINE_SOFT,
                  fg=TEXT2, bd=0, relief="flat", padx=18, pady=6, cursor="hand2",
                  activebackground="#e4ebf5").pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._no)
        entry.bind("<Return>", lambda e: self._ok())
        entry.focus_set()

        # 高度同样按内容算，并给错误提示留一行余量 —— 否则密钥输错、
        # lbl_err 一长出文字，底部按钮就被顶出窗口（和 ConfirmDialog 一个坑）。
        w = 420
        self.update_idletasks()
        need = max(150, self.winfo_reqheight()) + 24
        try:
            avail = self.winfo_screenheight() - 120
        except Exception:                          # noqa: BLE001
            avail = need
        h = min(need, avail)
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        x = max(0, min(x, max(0, self.winfo_screenwidth() - w - 8)))
        y = max(0, min(y, max(0, self.winfo_screenheight() - h - 8)))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.grab_set()

    # ------------------------------------------------ 按钮
    def _ok(self):
        self.result = self.var.get().strip()
        self.destroy()

    def _no(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------- 主窗口

class AdminWindow(tk.Toplevel):
    """管理员控制台。AdminWindow(app) 构造后窗口就会显示出来。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.info: dict = {}
        self.is_admin = False                      # 缓存一份，省得反复问 app
        self._busy = 0                             # 正在跑的网络请求数
        self._admin_buttons: list = []             # 需要管理员权限才能点的按钮
        self._last_code = None                     # 最近生成的那串密钥
        self._invite_rows: list = []
        self._user_rows: list = []
        self._bar_username = ""                    # 底部操作条记住的选中用户
        self._render_guard = False                 # 防止两个表格互相触发重画
        self._client_rows: list = []
        self._row_widgets: list = []               # [(tree, iid, 行内控件), ...]
        self._ctx = ""                             # 用户名/小时数输入框的错误态

        # ---- 外观 ----
        self.title(WIN_TITLE)
        self.configure(bg=BG)
        self.minsize(760, 560)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._center(WIN_W, WIN_H)

        # ---- 组件 ----
        self._init_style()
        self._build_topbar()
        self._build_notebook()
        self._build_invite_tab()
        self._build_user_tab()
        self._build_client_tab()
        self._build_update_tab()
        self._build_overlay()

        # ---- 首屏 ----
        self.reload_info()
        self.refresh_all()

        # 窗口尺寸变化时让遮罩和表格跟着动
        self.bind("<Configure>", self._on_resize)

        try:
            self.grab_set()
        except Exception:                          # noqa: BLE001
            pass

    # ================================================================ 基础设施

    def _center(self, w: int, h: int) -> None:
        """把窗口摆到主窗口正中。"""
        try:
            r = self.app.root
            r.update_idletasks()
            x = r.winfo_rootx() + (r.winfo_width() - w) // 2
            y = r.winfo_rooty() + (r.winfo_height() - h) // 3
            self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        except Exception:                          # noqa: BLE001
            self.geometry(f"{w}x{h}")

    def _wait(self, win=None) -> None:
        """阻塞等待子窗口关闭。抽成一个方法，测试里直接覆盖成空操作。

        win 为空时退化成等待自己，仅作兜底。
        """
        try:
            self.wait_window(win if win is not None else self)
        except Exception:                          # noqa: BLE001
            pass

    def _btn(self, parent, text, cmd, kind="normal", width=None, admin=False):
        """借主程序的按钮工厂，admin=True 的按钮会进"非管理员必须禁用"名单。"""
        b = self.app._btn(parent, text, cmd, kind=kind, width=width)
        if admin:
            self._admin_buttons.append(b)
        return b

    # ================================================================ ttk 浅色样式

    def _init_style(self) -> None:
        """把 ttk 调成本程序的浅色风格：去掉 3D 边框、行高 26、表头浅灰、斑马纹。"""
        st = ttk.Style(self)
        try:
            if "clam" in st.theme_names():
                st.theme_use("clam")
        except Exception:                          # noqa: BLE001
            pass

        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 4, 2, 0))
        st.configure("TNotebook.Tab", background="#e9eef6", foreground=TEXT2,
                     padding=(18, 7), borderwidth=0, font=("", 10))
        st.map("TNotebook.Tab",
               background=[("selected", CARD), ("active", "#eef4fd")],
               foreground=[("selected", TEXT), ("active", TEXT)])

        st.configure("MC.Treeview", background=ROW_A, fieldbackground=CARD,
                     foreground=TEXT, rowheight=26, borderwidth=0, relief="flat",
                     font=("", 9))
        st.map("MC.Treeview",
               background=[("selected", SEL_BG)],
               foreground=[("selected", TEXT)])
        st.configure("MC.Treeview.Heading", background=HEAD_BG, foreground=TEXT2,
                     relief="flat", borderwidth=0, padding=(6, 6), font=("", 9, "bold"))
        st.map("MC.Treeview.Heading", background=[("active", "#e6edf7")])
        st.layout("MC.Treeview", [("MC.Treeview.treearea", {"sticky": "nswe"})])

        # 表格外面套一层细边框，看起来和卡片一致
        st.configure("MC.TFrame", background=LINE, borderwidth=0)
        st.configure("MC.Vertical.TScrollbar", background="#cdd6e3", troughcolor=CARD,
                     borderwidth=0, arrowsize=12, relief="flat")
        st.map("MC.Vertical.TScrollbar", background=[("active", "#b6c2d2")])

    def _make_tree(self, parent, columns) -> ttk.Treeview:
        """建一个 show='headings' 的表格，列宽由 _fit_columns 统管。"""
        wrap = ttk.Frame(parent, style="MC.TFrame")
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        tree = ttk.Treeview(inner, columns=[c[0] for c in columns],
                            show="headings", style="MC.Treeview", selectmode="browse")
        tree._mclink_cols = list(columns)          # 记下"想要的"宽度
        tree._mclink_ops_w = None                  # 操作列的实测宽度
        for key, title, width, minw, anchor in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=minw, anchor=anchor, stretch=False)
        tree.tag_configure("odd", background=ROW_B)
        tree.tag_configure("even", background=ROW_A)

        sb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview,
                           style="MC.Vertical.TScrollbar")
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        # 表格尺寸一变就重新分配列宽，保证最后一列不会被切掉
        tree.bind("<Configure>", lambda e, t=tree: self._fit_columns(t), add="+")
        return tree

    def _fit_columns(self, tree) -> None:
        """按表格当前宽度分配列宽：操作列给足（实测），其余列按比例分剩下的。

        这里最容易出的 bug：操作列宽度**必须**容得下那一行所有按钮，
        否则 Treeview 会把超出部分裁掉 —— 表现就是"删除按钮看不到"。
        所以：
          1. ops 的最小宽度 = 实测需要（框宽 + 内边距），不是模型里写的那个数；
          2. 其余列按比例缩，保证 总宽 <= 可视宽，最后一列不会被挤出可视区。
        """
        cols = getattr(tree, "_mclink_cols", None)
        if not cols:
            return
        keys = [c[0] for c in cols]
        if "ops" not in keys:
            return
        try:
            inner_w = tree.winfo_width() - 4       # 减去两根竖线
        except Exception:                          # noqa: BLE001
            return
        if inner_w < 200:
            return
        models = {c[0]: c for c in cols}
        others = [k for k in keys if k != "ops"]
        base = [models[k][2] for k in others]

        # 操作列：先按实测需要，再夹到 [模型 minwidth, 模型 width] 之间，
        # 但**下限不能低于实测需要**，否则按钮一定被裁。
        ops_need = int(getattr(tree, "_mclink_ops_w", None) or 0)
        ops_w = max(models["ops"][2], ops_need)
        # 不能贪到把别的列挤成负数：最多占表格的 60%
        ops_w = min(ops_w, max(models["ops"][2], int(inner_w * 0.6)))
        free = max(160, inner_w - ops_w)
        total = float(sum(base)) or 1.0
        width = {}
        for k, b in zip(others, base):
            width[k] = max(models[k][3], int(free * b / total))
        # 正着分配会因为 minwidth 抬升而超宽 —— 超出部分从最宽的列里扣回来，
        # 否则总宽 > 可视宽，最右边的操作列就被挤出屏幕了。
        over = sum(width.values()) - free
        if over > 0:
            for k in sorted(width, key=lambda x: width[x], reverse=True):
                if over <= 0:
                    break
                floor = models[k][3]
                cut = min(over, max(0, width[k] - floor))
                width[k] -= cut
                over -= cut
        # 把四舍五入的零头补给"备注"这类弹性列，避免右边留缝
        width[others[-1]] += free - sum(width.values())
        for k in others:
            try:
                tree.column(k, width=max(models[k][3], width[k]))
            except Exception:                      # noqa: BLE001
                pass
        try:
            tree.column("ops", width=ops_w)
        except Exception:                          # noqa: BLE001
            pass

    @staticmethod
    def _tree_reset(tree: ttk.Treeview) -> None:
        """清空表格：先拆掉贴在行上的按钮容器，再删行，避免越堆越多。"""
        for holder in tree.winfo_children():
            for child in holder.winfo_children():
                if isinstance(child, _Link):
                    child.kill()
            holder.destroy()
        for item in tree.get_children():
            tree.delete(item)

    # ================================================================ 顶部状态栏

    def _build_topbar(self) -> None:
        bar = tk.Frame(self, bg=CARD)
        bar.pack(fill="x", side="top")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", side="top")
        self.topbar = bar

        # ---- 左：当前授权状态 ----
        left = tk.Frame(bar, bg=CARD)
        left.pack(side="left", padx=16, pady=11)
        self.lbl_lic = tk.Label(left, text="正在读取授权状态…", bg=CARD, fg=MUTED,
                                font=("", 10, "bold"))
        self.lbl_lic.pack(side="left")
        self.lbl_lic_sub = tk.Label(left, text="", bg=CARD, fg=MUTED, font=("", 8))
        self.lbl_lic_sub.pack(side="left", padx=(8, 0), pady=(2, 0))

        # ---- 右：管理员模式 ----
        right = tk.Frame(bar, bg=CARD)
        right.pack(side="right", padx=16, pady=11)
        self.pill = tk.Frame(right, bg=LINE_SOFT, padx=11, pady=4)
        self.lbl_pill = tk.Label(self.pill, text="未解锁", bg=LINE_SOFT, fg=MUTED,
                                 font=("", 9, "bold"))
        self.lbl_pill.pack()
        self.btn_unlock = self._btn(right, "输入管理员密钥", self.on_unlock, kind="ghost")
        self.btn_unlock.pack(side="right")

    # ================================================================ 标签页

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self, style="TNotebook")
        self.nb.pack(fill="both", expand=True, padx=14, pady=(10, 14))

        self.tab_invite = tk.Frame(self.nb, bg=BG)
        self.tab_user = tk.Frame(self.nb, bg=BG)
        self.tab_client = tk.Frame(self.nb, bg=BG)
        self.tab_update = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_invite, text="邀请码")
        self.nb.add(self.tab_user, text="用户")
        self.nb.add(self.tab_client, text="客户端")
        self.nb.add(self.tab_update, text="更新")
        self.nb.select(self.tab_invite)

    # ---------------------------------------------------------------- 邀请码页

    def _label(self, parent, text, font_key="f_small"):
        return tk.Label(parent, text=text, bg=CARD, fg=TEXT2,
                        font=getattr(self.app, font_key))

    def _form_field(self, parent, key, label, value="", width=14, hint=""):
        """表单里的一个输入框。

        返回的是"整列"这个 Frame（调用方把它 pack/grid 到一行里），
        Entry 本身已经 pack 在列内，值存在 self.vars[key]。
        """
        col = tk.Frame(parent, bg=CARD)
        tk.Label(col, text=label, bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(0, 3))
        var = tk.StringVar(value=str(value))
        entry = tk.Entry(col, textvariable=var, font=self.app.f_body, width=width,
                         bg="#fbfcfe", fg=TEXT, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=SKY, insertbackground=TEXT)
        entry.pack(fill="x", ipady=5, ipadx=6)
        if hint:
            tk.Label(col, text=hint, bg=CARD, fg=MUTED,
                     font=self.app.f_tiny).pack(anchor="w", pady=(2, 0))
        self.vars[key] = var
        self.entries[key] = entry
        return col

    def _build_invite_tab(self) -> None:
        self.vars: dict = {}
        self.entries: dict = {}
        self.row_links: list = []                  # 所有单元格按钮，用来统一 enable/disable

        # ---------- 生成表单 ----------
        card = tk.Frame(self.tab_invite, bg=LINE)
        card.pack(fill="x", padx=12, pady=(12, 8))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        box = tk.Frame(inner, bg=CARD)
        box.pack(fill="x", padx=14, pady=12)

        self._label(box, "生成一次性邀请码", "f_card_title").pack(anchor="w", pady=(0, 8))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")

        self._form_field(row, "username", "用户名", "", width=16).pack(
            side="left", padx=(0, 8))
        self._form_field(row, "hours", "有效期(小时)", DEFAULT_HOURS, width=8).pack(
            side="left", padx=(0, 8))
        note_col = tk.Frame(row, bg=CARD)
        note_col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._form_field(note_col, "note", "备注（可选）", "", width=20).pack(fill="x")

        act = tk.Frame(row, bg=CARD)
        act.pack(side="left")
        tk.Label(act, text=" ", bg=CARD, font=self.app.f_small).pack(anchor="w", pady=(0, 3))
        self.btn_create = self._btn(act, "生成邀请码", self.on_create, kind="primary",
                                    admin=True)
        self.btn_create.pack()

        self.lbl_form_err = tk.Label(box, text="", bg=CARD, fg=RED, font=self.app.f_small)
        self.lbl_form_err.pack(anchor="w", pady=(8, 0))
        tk.Label(box, text="用户名只能包含字母、数字、下划线和中划线，长度 2-32。",
                 bg=CARD, fg=MUTED, font=self.app.f_tiny).pack(anchor="w", pady=(4, 0))

        # ---------- 最近生成（在列表上方，首次生成后才会出现） ----------
        self.recent = tk.Frame(self.tab_invite, bg=LINE)
        r_inner = tk.Frame(self.recent, bg="#f2fbf6")
        r_inner.pack(fill="both", expand=True, padx=1, pady=1)
        rp = tk.Frame(r_inner, bg="#f2fbf6")
        rp.pack(fill="x", padx=14, pady=12)
        head = tk.Frame(rp, bg="#f2fbf6")
        head.pack(fill="x")
        tk.Label(head, text="最近生成", bg="#f2fbf6", fg=GREEN,
                 font=self.app.f_card_title).pack(side="left")
        self.lbl_recent_meta = tk.Label(head, text="", bg="#f2fbf6", fg=TEXT2,
                                        font=self.app.f_small)
        self.lbl_recent_meta.pack(side="left", padx=(10, 0))
        self.btn_recent_copy = self._btn(head, "复制密钥", self.on_copy_last, kind="ghost",
                                         admin=True)
        self.btn_recent_copy.pack(side="right")

        self.lbl_code = tk.Label(rp, text="", bg="#f2fbf6", fg=GREEN, font=self._code_font())
        self.lbl_code.pack(anchor="w", pady=(8, 4))
        self.lbl_recent_hint = tk.Label(
            rp, text="24 小时内有效，只能使用一次，请发给对方",
            bg="#f2fbf6", fg=AMBER, font=self.app.f_small)
        self.lbl_recent_hint.pack(anchor="w")

        # ---------- 列表（用 before= 插到"最近生成"上面，保证顺序固定） ----------
        head2 = tk.Frame(self.tab_invite, bg=BG)
        tk.Label(head2, text="全部邀请码", bg=BG, fg=TEXT,
                 font=self.app.f_card_title).pack(side="left")
        self.lbl_invite_count = tk.Label(head2, text="", bg=BG, fg=MUTED,
                                         font=self.app.f_small)
        self.lbl_invite_count.pack(side="left", padx=(10, 0))
        self.btn_invite_refresh = self._btn(head2, "刷新", self.on_refresh_invites,
                                            kind="ghost")
        self.btn_invite_refresh.pack(side="right")
        # 历史记录开关：删用户时邀请码**不会被删**，只是默认不显示。
        # 想查历史就勾上这个，数据一条都不少。
        self.v_invite_hist = tk.BooleanVar(value=False)
        self.chk_invite_hist = tk.Checkbutton(
            head2, text="显示历史（用户已删除的记录）", variable=self.v_invite_hist,
            command=self.on_toggle_invite_hist, bg=BG, fg=TEXT2,
            font=self.app.f_small, activebackground=BG,
            selectcolor="#eef6ff", anchor="w")
        self.chk_invite_hist.pack(side="right", padx=(0, 10))
        # recent 先 pack 一次拿到位置，再立刻 forget，之后 show_recent() 才能插回原位
        self.recent.pack(fill="x", padx=12, pady=(0, 8))
        head2.pack(fill="x", padx=14, pady=(2, 4), before=self.recent)
        self._head_invite = head2

        # 操作列一开始就建好，非管理员时遮罩会挡住整页
        self.tree_invite = self._make_tree(self.tab_invite, self._invite_columns(True))
        self.show_recent(False)                    # 还没生成过就先藏起来

    def show_recent(self, on: bool = True) -> None:
        """显示 / 隐藏"最近生成"面板。位置固定在"全部邀请码"标题上方。"""
        if on:
            if not self.recent.winfo_ismapped():
                self.recent.pack(fill="x", padx=12, pady=(0, 8),
                                 before=self._head_invite)
        else:
            self.recent.pack_forget()

    def _code_font(self):
        """密钥用大号等宽字体显示。"""
        base = 15
        try:
            f = self.app.f_mono_b
            if isinstance(f, tuple) and len(f) >= 2 and isinstance(f[1], int):
                base = max(15, f[1] + 5)
            return (f[0], base, "bold")
        except Exception:                          # noqa: BLE001
            return ("Consolas", base, "bold")

    @staticmethod
    def _invite_columns(with_actions: bool):
        # 列宽加起来要能塞进 860 宽窗口的表格区（约 790px），否则最后一列会被切掉
        cols = [("code", "密钥", 168, 130, "w"),
                ("username", "用户名", 106, 70, "w"),
                ("state", "状态", 80, 60, "w"),
                ("expires", "到期时间", 130, 100, "w"),
                ("note", "备注", 130, 80, "w")]
        if with_actions:
            cols.append(("ops", "操作", 140, 110, "w"))
        return cols

    def _ensure_invite_actions(self) -> None:
        """只有管理员才需要"操作"列；第一次进入管理员模式时补上。"""
        if "ops" in self.tree_invite["columns"]:
            return
        for key, title, width, minw, anchor in self._invite_columns(True)[-1:]:
            self.tree_invite.configure(columns=list(self.tree_invite["columns"]) + [key])
            self.tree_invite.heading(key, text=title, anchor="w")
            self.tree_invite.column(key, width=width, minwidth=minw, anchor=anchor,
                                    stretch=True)

    # ---------------------------------------------------------------- 用户页

    def _build_user_tab(self) -> None:
        head = tk.Frame(self.tab_user, bg=BG)
        head.pack(fill="x", padx=14, pady=(14, 6))
        self.lbl_user_stats = tk.Label(head, text="", bg=BG, fg=TEXT2,
                                       font=self.app.f_card_title)
        self.lbl_user_stats.pack(side="left")
        self.btn_user_refresh = self._btn(head, "刷新", self.on_refresh_users, kind="ghost")
        self.btn_user_refresh.pack(side="right")

        # 表格下面的常驻操作条**必须先 pack**（side="bottom"）。
        # 为什么要有它：行内的「删除」是贴 Treeview 单元格画的，一旦窗口变窄、
        # 列被挤、或者行还没排上，按钮就会被裁掉甚至完全看不见
        # （用户反馈的"删除按钮都看不到"）。这排按钮是普通布局，永远在。
        # 而 Treeview 是 fill=both/expand=True，后 pack 它就会把先 pack 的挤掉 ——
        # 所以这里顺序不能反，反了操作条就被顶出窗口底部（这次又踩了一次）。
        bar = tk.Frame(self.tab_user, bg=BG)
        bar.pack(side="bottom", fill="x", padx=14, pady=(8, 10))
        self.bar_user_actions = bar
        self.lbl_bar_pick = tk.Label(bar, text="在表格里点一行，再用这里的按钮操作",
                                     bg=BG, fg=MUTED, font=self.app.f_small)
        self.lbl_bar_pick.pack(side="left")
        self.btn_bar_delete = self._btn(bar, "删除用户", self._bar_delete,
                                        kind="danger")
        self.btn_bar_delete.pack(side="right", padx=(8, 0))
        self.btn_bar_unbind = self._btn(bar, "解绑设备", self._bar_unbind,
                                        kind="normal")
        self.btn_bar_unbind.pack(side="right", padx=(8, 0))
        self.btn_bar_toggle = self._btn(bar, "停用", self._bar_toggle, kind="normal")
        self.btn_bar_toggle.pack(side="right")
        for b in (self.btn_bar_toggle, self.btn_bar_unbind, self.btn_bar_delete):
            try:
                b.configure(state="disabled")
            except Exception:                      # noqa: BLE001
                pass

        self.tree_user = self._make_tree(self.tab_user, self._user_columns(True))
        try:
            self.tree_user.bind("<<TreeviewSelect>>", self._on_user_select, add="+")
        except Exception:                          # noqa: BLE001
            pass

    @staticmethod
    def _user_columns(with_actions: bool):
        # 同上：总宽要留在表格可视区内
        cols = [("username", "用户名", 96, 70, "w"),
                ("state", "状态", 76, 60, "w"),
                ("device", "绑定设备", 128, 80, "w"),
                ("seen", "最后在线", 126, 100, "w"),
                ("ip", "最后 IP", 108, 90, "w"),
                ("note", "备注", 86, 60, "w")]
        if with_actions:
            cols.append(("ops", "操作", 200, 160, "w"))
        return cols

    def _ensure_user_actions(self) -> None:
        if "ops" in self.tree_user["columns"]:
            return
        key, title, width, minw, anchor = self._user_columns(True)[-1]
        self.tree_user.configure(columns=list(self.tree_user["columns"]) + [key])
        self.tree_user.heading(key, text=title, anchor="w")
        self.tree_user.column(key, width=width, minwidth=minw, anchor=anchor, stretch=True)

    # ---------------------------------------------------------------- 客户端页

    def _build_client_tab(self) -> None:
        """谁在用、用什么昵称、从哪个 IP 来、拿哪张邀请码激活的。"""
        head = tk.Frame(self.tab_client, bg=BG)
        head.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(head, text="客户端（昵称由用户自己在客户端里填）", bg=BG, fg=TEXT,
                 font=self.app.f_card_title).pack(side="left")
        self.btn_client_refresh = self._btn(head, "刷新", self.on_refresh_clients,
                                            kind="ghost")
        self.btn_client_refresh.pack(side="right")

        self.lbl_client_stats = tk.Label(self.tab_client, text="", bg=BG, fg=TEXT2,
                                         font=self.app.f_small)
        self.lbl_client_stats.pack(anchor="w", padx=14, pady=(0, 6))

        self.tree_client = self._make_tree(self.tab_client, self._client_columns())
        tk.Label(self.tab_client,
                 text="「在线」表示此刻正连着服务端。IP 是服务端看到的来源地址，"
                      "不是用户的家用宽带内网地址。",
                 bg=BG, fg=MUTED, font=self.app.f_tiny).pack(anchor="w", padx=14,
                                                             pady=(6, 0))

    @staticmethod
    def _client_columns():
        return [("online", "在线", 58, 50, "center"),
                ("nickname", "昵称", 110, 70, "w"),
                ("username", "用户名", 96, 70, "w"),
                ("invite", "邀请码", 128, 90, "w"),
                ("ip", "来源 IP", 118, 90, "w"),
                ("version", "客户端版本", 86, 70, "w"),
                ("machine", "机器名", 118, 70, "w"),
                ("seen", "最后在线", 122, 100, "w")]

    def on_refresh_clients(self) -> None:
        self.load_clients()
        self.load_stats()

    def load_clients(self) -> None:
        self._call("client_list", {}, ok=self.render_clients,
                   btn=self.btn_client_refresh, busy_text="刷新中…")

    def render_clients(self, result) -> None:
        rows = []
        if isinstance(result, dict):
            rows = result.get("clients") or []
        elif isinstance(result, list):
            rows = result
        self._client_rows = rows

        self._tree_reset(self.tree_client)
        for i, r in enumerate(rows):
            r = r if isinstance(r, dict) else {}
            online = "● 在线" if r.get("online") else "○ 离线"
            self.tree_client.insert(
                "", "end", tags=("odd" if i % 2 else "even",),
                values=(online, r.get("nickname") or "—", r.get("username") or "—",
                        r.get("invite") or "—", r.get("ip") or "—",
                        r.get("version") or "—", r.get("machine") or "—",
                        fmt_ts(r.get("last_seen"))))
        n_online = sum(1 for r in rows if r and r.get("online"))
        self.lbl_client_stats.configure(
            text=f"共 {len(rows)} 个客户端 · {n_online} 个在线")
        self.after(30, lambda: self._settle_row_widgets(self.tree_client))

    # ---------------------------------------------------------------- 更新页

    def _build_update_tab(self) -> None:
        """显示"服务端现在下发的客户端更新包"，以及怎么发布一个新包。"""
        card = tk.Frame(self.tab_update, bg=LINE)
        card.pack(fill="x", padx=12, pady=(14, 8))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        box = tk.Frame(inner, bg=CARD)
        box.pack(fill="x", padx=14, pady=12)

        tk.Label(box, text="客户端自动更新", bg=CARD, fg=TEXT,
                 font=self.app.f_card_title).pack(anchor="w")
        self.lbl_upd_state = tk.Label(box, text="正在读取…", bg=CARD, fg=TEXT2,
                                      font=self.app.f_small, justify="left",
                                      wraplength=740)
        self.lbl_upd_state.pack(anchor="w", pady=(6, 0))
        self.lbl_upd_notes = tk.Label(box, text="", bg=CARD, fg=MUTED,
                                      font=self.app.f_tiny, justify="left",
                                      wraplength=740)
        self.lbl_upd_notes.pack(anchor="w", pady=(4, 0))

        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        self.btn_upd_refresh = self._btn(row, "刷新", self.on_refresh_update,
                                         kind="ghost")
        self.btn_upd_refresh.pack(side="right")

        howto = (
            "怎么发布一个新版本（在你自己的电脑上跑一条命令）\n"
            "  1) 改客户端版本号：client\\version.json 里的 version / release\n"
            "  2) 打包：client\\launcher\\make_dist.ps1 -Zip\n"
            "  3) 发布：tools\\publish_update.ps1 -Zip <第 2 步生成的 zip>\n"
            "     （它会 scp 上传、写好清单、重启服务端）\n"
            "  4) 朋友那边的客户端下次启动就会看到「有新版本」提示，点一下就装好了。\n\n"
            "别人正在玩游戏时不会被强制重启：更新只提示，由用户自己决定什么时候装。"
        )
        tk.Label(self.tab_update, text=howto, bg=BG, fg=TEXT2, font=self.app.f_small,
                 justify="left", anchor="w").pack(fill="x", padx=16, pady=(10, 0))

    def on_refresh_update(self) -> None:
        self.load_update()

    def load_update(self) -> None:
        self._call("update_status", {}, ok=self.render_update,
                   btn=self.btn_upd_refresh, busy_text="读取中…")

    def render_update(self, result) -> None:
        result = result if isinstance(result, dict) else {}
        cur = result.get("version") or "—"
        rel = result.get("release") or "—"
        pkgs = result.get("packages") or {}
        if pkgs:
            bits = []
            for plat, e in sorted(pkgs.items()):
                e = e if isinstance(e, dict) else {}
                size = e.get("size") or 0
                bits.append(f"{plat} v{e.get('version') or '?'}"
                            f"（{int(size) / 1024:.0f} KB）")
            text = f"服务端当前下发的更新包：{'、'.join(bits)}\n清单版本 {cur} · 批次 {rel}"
        else:
            text = ("服务端还没有发布任何客户端更新包 —— 客户端不会看到更新提示。\n"
                    "发布方法见下方说明。")
        self.lbl_upd_state.configure(text=text)
        note = result.get("notes") or ""
        self.lbl_upd_notes.configure(text=("更新说明：" + note) if note else "")
        self.update_permissions()

    # ================================================================ 非管理员遮罩

    def _build_overlay(self) -> None:
        """盖在内容上的提示层：非管理员时挡住操作，防止误以为可以点。"""
        self.overlay = tk.Frame(self.nb, bg=BG)
        self.ov_card = tk.Frame(self.overlay, bg=CARD, highlightthickness=1,
                                highlightbackground=LINE)
        self.ov_card.pack(expand=True)
        self.ov_title = tk.Label(self.ov_card, text=NON_ADMIN_HINT, bg=CARD, fg=TEXT,
                                 font=self.app.f_title)
        self.ov_title.pack(padx=34, pady=(20, 4))
        self.ov_sub = tk.Label(self.ov_card,
                               text="输入管理员密钥后即可管理邀请码、用户与客户端。",
                               bg=CARD, fg=MUTED, font=self.app.f_small)
        self.ov_sub.pack(padx=34)
        self.ov_btn = self._btn(self.ov_card, "输入管理员密钥", self.on_unlock,
                                kind="primary")
        self.ov_btn.pack(pady=(14, 22))

    def _on_resize(self, _evt=None) -> None:
        """窗口大小变了：遮罩重新铺满，行内按钮按新列宽重新摆位。"""
        try:
            if self.overlay.winfo_ismapped():
                self.overlay.place_configure(x=0, y=0,
                                             width=self.nb.winfo_width(),
                                             height=self.nb.winfo_height())
        except Exception:                          # noqa: BLE001
            pass
        self.after(40, self._settle_row_widgets)

    # ================================================================ 状态刷新

    def reload_info(self) -> None:
        """读一次 app.license_info()，更新顶栏和权限状态。"""
        try:
            info = self.app.license_info() or {}
        except Exception as exc:                   # noqa: BLE001
            info = {}
            self.app.toast(f"读取授权状态失败：{exc}", "err")
        self.info = info
        self.is_admin = bool(info.get("is_admin"))
        self.render_topbar()
        self.update_permissions()

    def render_topbar(self) -> None:
        info = self.info or {}
        licensed = bool(info.get("licensed"))
        username = info.get("username")
        reason = info.get("reason")

        if licensed:
            name = username or "未知用户"
            self.lbl_lic.configure(text=f"已授权 · 用户名 {name}", fg=GREEN)
            bits = []
            st = info.get("status")
            if st:
                bits.append("状态：" + ("正常" if st == "active" else "已停用"))
            if info.get("device_token"):
                bits.append("设备：" + str(info["device_token"])[:12])
            self.lbl_lic_sub.configure(text="    ".join(bits))
        else:
            self.lbl_lic.configure(text="未授权 · " + (reason or "尚未激活"),
                                   fg=RED)
            self.lbl_lic_sub.configure(
                text="用一次性邀请码激活后即可开始映射" if not reason else "")

        if self.is_admin:
            self.pill.configure(bg="#eafaf1")
            self.lbl_pill.configure(text="管理员模式", bg="#eafaf1", fg=GREEN)
            try:
                self.btn_unlock.configure(text="退出管理员模式", command=self.on_lock)
            except Exception:                      # noqa: BLE001
                pass
        else:
            self.pill.configure(bg=LINE_SOFT)
            self.lbl_pill.configure(text="未解锁", bg=LINE_SOFT, fg=MUTED)
            try:
                self.btn_unlock.configure(text="输入管理员密钥", command=self.on_unlock)
            except Exception:                      # noqa: BLE001
                pass

    def update_permissions(self) -> None:
        """按"是否管理员"和"是否正在请求"决定按钮的可用性与遮罩。"""
        busy = self._busy > 0
        for b in self._admin_buttons:
            try:
                b.configure(state="normal" if (self.is_admin and not busy)
                            else "disabled")
            except Exception:                      # noqa: BLE001
                pass

        # 表格重建时旧的行按钮已经跟着销毁，先把它们剔掉再改状态，
        # 免得把状态设到一堆已经没用的对象上
        if self.is_admin:
            self._prune_links()
            for link in self.row_links:
                link.set_enabled(not busy and link._allowed)

        # 解锁按钮在工作时也不能重复点
        try:
            self.btn_unlock.configure(state="disabled" if busy else "normal")
        except Exception:                          # noqa: BLE001
            pass

        if self.is_admin:
            self.overlay.place_forget()
            # 管理员才需要操作列
            self._ensure_invite_actions()
            self._ensure_user_actions()
        else:
            try:
                self.overlay.place(x=0, y=0, width=self.nb.winfo_width(),
                                   height=self.nb.winfo_height())
                self.overlay.lift()
            except Exception:                      # noqa: BLE001
                pass

    # ------------------------------------------------ 管理员模式

    def on_unlock(self) -> None:
        """弹窗输入管理员密钥 -> app.admin_unlock()。"""
        dlg = _KeyDialog(self)
        self._wait(dlg)
        key = getattr(dlg, "result", None)
        if not key:
            return
        try:
            ok, err = self.app.admin_unlock(key)
        except Exception as exc:                   # noqa: BLE001
            self.app.toast(f"解锁失败：{exc}", "err")
            return
        if not ok:
            self.app.toast(err or "密钥不正确", "err")
            return
        self.app.toast("已进入管理员模式", "ok")
        self.reload_info()
        self.refresh_all()

    def on_lock(self) -> None:
        """退出管理员模式（本地记住的密钥也会清掉）。"""
        try:
            self.app.admin_lock()
        except Exception as exc:                   # noqa: BLE001
            self.app.toast(f"退出管理员模式失败：{exc}", "err")
            return
        self._last_code = None
        self.recent.pack_forget()
        self.app.toast("已退出管理员模式")
        self.reload_info()
        self.refresh_all()

    # ================================================================ 异步请求封装

    def _busy_begin(self, btn=None, text="处理中…"):
        """返回一个回调包装器：请求发出->界面进入忙碌，回调回来->恢复。"""
        self._busy += 1
        if btn is not None:
            try:
                btn.configure(text=text, state="disabled")
            except Exception:                      # noqa: BLE001
                pass
        self.update_permissions()

        def done(result, error):
            self._busy = max(0, self._busy - 1)
            if btn is not None:
                try:
                    btn.configure(text=self._btn_label(btn), state="normal")
                except Exception:                  # noqa: BLE001
                    pass
            self.update_permissions()
            if error:
                self.app.toast(str(error), "err")
        return done

    @staticmethod
    def _btn_label(btn) -> str:
        """按钮忙碌前的原文，从 _busy_labels 里取。"""
        return getattr(btn, "_mclink_label", None) or btn.cget("text")

    def _call(self, action, args, ok=None, btn=None, busy_text="处理中…"):
        """发一次管理员请求。ok(result) 只在没有 error 的时候被调用。"""
        if btn is not None:
            try:
                btn._mclink_label = btn.cget("text")   # 记下原文，回调后还原
            except Exception:                          # noqa: BLE001
                pass
        done = self._busy_begin(btn, busy_text)

        def cb(result, error):
            done(result, error)
            if error:
                return
            try:
                if ok is not None:
                    ok(result)
            except Exception as exc:                   # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.app.toast(f"界面刷新出错：{exc}", "err")

        try:
            self.app.admin_call(action, args or {}, cb)
        except Exception as exc:                       # noqa: BLE001
            done(None, str(exc))
            self.app.toast(f"请求发送失败：{exc}", "err")

    def refresh_all(self) -> None:
        """所有标签页的数据都拉一遍。"""
        self.load_invites()
        self.load_users()
        self.load_clients()
        self.load_update()
        self.load_stats()

    def on_refresh_invites(self) -> None:
        self.load_invites()

    def on_refresh_users(self) -> None:
        self.load_users()
        self.load_stats()

    # ================================================================ 邀请码：生成

    def validate_form(self) -> tuple:
        """校验表单，返回 (username, hours, note, error, bad_field)。

        error 非空表示不通过；bad_field 是要描红的输入框 key。
        """
        username = (self.vars["username"].get() or "").strip()
        hours_raw = (self.vars["hours"].get() or "").strip()
        note = (self.vars["note"].get() or "").strip()

        if not username:
            return None, None, note, "请填写用户名", "username"
        if not USERNAME_RE.match(username):
            return (None, None, note,
                    "用户名只能是 2-32 位的字母、数字、下划线或中划线", "username")
        if not hours_raw:
            return None, None, note, "请填写有效期小时数", "hours"
        try:
            hours = int(hours_raw)
        except ValueError:
            return None, None, note, "有效期必须是整数小时", "hours"
        if not (HOURS_MIN <= hours <= HOURS_MAX):
            return (None, None, note,
                    f"有效期需要在 {HOURS_MIN}-{HOURS_MAX} 小时之间", "hours")
        return username, hours, note, "", ""

    def _mark_field(self, key: str, bad: bool) -> None:
        """把不合法的输入框描红。"""
        try:
            self.entries[key].configure(highlightbackground=RED if bad else LINE,
                                        highlightcolor=RED if bad else SKY)
        except Exception:                          # noqa: BLE001
            pass

    def on_create(self) -> None:
        """生成邀请码：先本地校验，再把请求丢给后端。"""
        username, hours, note, err, key = self.validate_form()
        self._mark_field("username", key == "username")
        self._mark_field("hours", key == "hours")
        if err:
            self.lbl_form_err.configure(text="⚠ " + err)
            return
        self.lbl_form_err.configure(text="")
        # 生成之后就把用户名清掉，方便连着给下一个人发
        self.vars["username"].set("")
        self._call("invite_create",
                   {"username": username, "hours": hours, "note": note},
                   ok=self.on_created, btn=self.btn_create)

    def on_created(self, result) -> None:
        """生成成功的回调：把密钥大号显示在"最近生成"里，并自动复制一次。"""
        result = result if isinstance(result, dict) else {}
        code = result.get("code") or ""
        if not code:
            self.app.toast("服务端没有返回邀请码", "err")
            return
        self._last_code = code
        username = result.get("username") or ""
        self.lbl_code.configure(text=code)
        self.lbl_recent_meta.configure(
            text=(f"用户名 {username}    ·    到期 {fmt_ts(result.get('expires_at'))}"
                  if username else f"到期 {fmt_ts(result.get('expires_at'))}"))
        self.show_recent(True)
        self.app.copy(code, "邀请码已复制")
        self.load_invites()                       # 列表也顺手刷一下

    def on_copy_last(self) -> None:
        if self._last_code:
            self.app.copy(self._last_code, "邀请码已复制")
        else:
            self.app.toast("还没有生成过邀请码", "warn")

    # ================================================================ 邀请码：列表

    def load_invites(self) -> None:
        show_hist = bool(getattr(self, "v_invite_hist", None)
                         and self.v_invite_hist.get())
        self._call("invite_list", {"include_history": show_hist},
                   ok=self.render_invites,
                   btn=self.btn_invite_refresh, busy_text="刷新中…")

    def on_toggle_invite_hist(self) -> None:
        """勾选/取消「显示历史」—— 重新拉一次列表（记录一直在服务端）。"""
        show = bool(self.v_invite_hist.get())
        self.app.toast("已显示全部历史邀请码" if show
                       else "已隐藏「用户已删除」的历史记录（数据仍在服务端保存）",
                       "info", 3200)
        self.load_invites()

    def render_invites(self, result) -> None:
        rows = []
        if isinstance(result, dict):
            rows = result.get("invites") or []
            try:
                self._invite_history_count = int(result.get("history_count") or 0)
            except Exception:                        # noqa: BLE001
                self._invite_history_count = 0
        elif isinstance(result, list):
            rows = result
        self._invite_rows = rows

        self._tree_reset(self.tree_invite)
        self._prune_links()
        now = time.time()
        # 用户列表里已有的用户名 —— 用来认出"绑给已删除用户"的孤儿邀请码
        known_users = {str(u.get("username") or "")
                       for u in (self._user_rows or []) if isinstance(u, dict)}
        known_users.discard("")
        if not known_users:
            known_users = None
        for i, row in enumerate(rows):
            row = row if isinstance(row, dict) else {}
            code = str(row.get("code") or "")
            state = invite_state_of(row, now, known_users)
            # 状态文字用"●"色块区分（Treeview 没法只给单元格上色），
            # 颜色值留给以后要改成整行着色时用
            text, _color = INVITE_STATE.get(state, ("● 未知", MUTED))
            tag = "odd" if i % 2 else "even"
            iid = self.tree_invite.insert(
                "", "end", tags=tag,
                values=(code, row.get("username") or "—", text,
                        fmt_ts(row.get("expires_at")), row.get("note") or "—"))

            if self.is_admin:
                bg = ROW_B if i % 2 else ROW_A
                holder = tk.Frame(self.tree_invite, bg=bg)
                self._add_link(holder, "复制", lambda c=code: self._copy_row(c), bg=bg)
                revoke = self._add_link(holder, "撤销", lambda c=code: self.on_revoke(c),
                                        kind="danger", bg=bg)
                if state != "pending":
                    revoke.set_allowed(False)      # 已使用/已过期的码没什么好撤销的
                holder.update_idletasks()
                self._place_row_widget(self.tree_invite, iid, holder)

        pending = sum(1 for r in rows
                      if invite_state_of(r, now, known_users) == "pending")
        orphan = sum(1 for r in rows
                     if invite_state_of(r, now, known_users) == "orphaned")
        count_text = f"共 {len(rows)} 个 · {pending} 个待使用"
        if orphan:
            count_text += f" · 含 {orphan} 条历史记录（用户已删除，仅留档）"
        hist = int(getattr(self, "_invite_history_count", 0) or 0)
        showing_hist = bool(getattr(self, "v_invite_hist", None)
                            and self.v_invite_hist.get())
        if hist and not showing_hist:
            count_text += f" · 另有 {hist} 条历史记录未显示（勾选「显示历史」可查）"
        self.lbl_invite_count.configure(text=count_text)
        self.update_permissions()
        # 列宽是 Tk 稍后才算完的，等它定下来再摆一次按钮
        self.after(30, lambda: self._settle_row_widgets(self.tree_invite))

    def _prune_links(self) -> None:
        """把已经被表格重建带走的行内按钮从管理名单里剔掉。"""
        self.row_links = [l for l in self.row_links if l._alive]

    def _place_row_widget(self, tree, iid: str, widget, tries: int = 0) -> None:
        """把某一行的操作列换成自定义控件（Treeview 本身放不了按钮）。"""
        if tries > 40:                             # 约 2.4 秒还排不上就不管了
            return
        try:
            bbox = tree.bbox(iid, "ops")
        except Exception:                          # noqa: BLE001
            bbox = None
        if not bbox:
            # 行还没显示出来（被滚动到可视区外），稍后再试
            try:
                tree.after(60, lambda: self._place_row_widget(tree, iid, widget, tries + 1))
            except Exception:                      # noqa: BLE001
                pass
            return
        x, y, w, h = bbox
        widget.place(x=x, y=y, width=w, height=h)
        widget.update_idletasks()
        # 记住这一行操作列到底需要多宽，供 _fit_columns 分配列宽用。
        # 注意：这里要量的是**里面所有按钮排完之后的实际宽度**，
        # 不能只看 widget.winfo_reqwidth() —— holder 已经被 place 成固定宽度了，
        # reqwidth 反映不出内容真实需要多少，量小了操作列就会被裁掉。
        need = 0
        kids = widget.winfo_children()
        for kid in kids:
            try:
                need += kid.winfo_reqwidth() + 4
            except Exception:                      # noqa: BLE001
                pass
        if not need:
            need = widget.winfo_reqwidth()
        need += 10                                  # 左右各留一点
        if not getattr(tree, "_mclink_ops_w", None) or need > tree._mclink_ops_w:
            tree._mclink_ops_w = need
        # 记下它属于哪一行，好让 Tk 把列宽算完之后再摆一次
        try:
            self._row_widgets.append((tree, iid, widget))
        except AttributeError:
            self._row_widgets = [(tree, iid, widget)]

    def _settle_row_widgets(self, tree=None) -> None:
        """Tk 把列宽/滚动位置最终定下来之后，再按真实 bbox 摆一遍行内控件。

        顺序很重要：先用实测到的操作列宽度重算列宽，再按新列宽摆按钮，
        否则按钮会按"还没定下来的列宽"落位，右边那一两个就被切掉了。
        """
        trees = []
        for t, _iid, _w in getattr(self, "_row_widgets", []):
            if (tree is None or t is tree) and t not in trees:
                trees.append(t)
        for t in trees:
            self._fit_columns(t)

        keep = []
        for item in getattr(self, "_row_widgets", []):
            t, iid, widget = item
            if tree is not None and t is not tree:
                keep.append(item)
                continue
            try:
                if not widget.winfo_exists() or not t.exists(iid):
                    continue
                bbox = t.bbox(iid, "ops")
            except Exception:                      # noqa: BLE001
                continue
            if bbox:
                x, y, w, h = bbox
                widget.place(x=x, y=y, width=w, height=h)
            keep.append(item)
        self._row_widgets = keep

    def _copy_row(self, code: str) -> None:
        self.app.copy(code, "邀请码已复制")

    def on_revoke(self, code: str) -> None:
        if not self._confirm("撤销邀请码",
                             f"确定要撤销邀请码「{code}」吗？\n\n"
                             f"撤销后这串密钥立刻失效，别人拿到也用不了。",
                             danger=True):
            return
        self._call("invite_revoke", {"code": code},
                   ok=lambda _r: (self.app.toast("邀请码已撤销", "ok"),
                                  self.load_invites()),
                   btn=self.btn_invite_refresh)

    # ================================================================ 用户：列表

    def _on_user_select(self, _event=None) -> None:
        """选中一行时，底部那排按钮跟着变可用/变文字。"""
        btn = getattr(self, "bar_user_actions", None)
        if btn is None:
            return
        u = self._selected_user()
        if u:
            # 记住选中的是谁：表格重建（刷新/删人之后）会把 selection 清掉，
            # 不留这一份的话底部按钮就会突然"点了没反应"。
            self._bar_username = str(u.get("username") or "")
        state = "normal" if (self.is_admin and u) else "disabled"
        if u:
            is_on = str(u.get("status") or "") != "disabled"
            self.btn_bar_toggle.configure(
                text="停用" if is_on else "启用", state=state)
            # 判断"有没有绑设备"只看 device_name —— 和行内按钮、表格里
            # 显示的那一列保持一致。bound_at 在解绑后可能还留着旧值，
            # 拿它当依据会让"解绑"在没设备的用户上变成可点（空操作）。
            has_dev = bool(u.get("device_name"))
            self.btn_bar_unbind.configure(
                state="normal" if (state == "normal" and has_dev) else "disabled")
            self.lbl_bar_pick.configure(
                text=f"已选中：{u.get('nickname') or u.get('username')}"
                     f"（{u.get('username')}）")
        else:
            self.btn_bar_toggle.configure(text="停用", state="disabled")
            self.btn_bar_unbind.configure(state="disabled")
            self.lbl_bar_pick.configure(text="在表格里点一行，再用这里的按钮操作")
        self.btn_bar_delete.configure(state=state)

    def _selected_user(self) -> dict:
        """当前选中的那一行对应的用户记录（没有就返回空 dict）。

        表格选中项优先；表格刚被重建（selection 被清空）时退回记住的那个用户名，
        这样点"删除/停用"不会因为一次刷新就失灵。
        """
        want = ""
        try:
            sel = self.tree_user.selection()
            if sel:
                want = str(self.tree_user.set(sel[0], "username") or "")
        except Exception:                            # noqa: BLE001
            want = ""
        if not want:
            want = str(getattr(self, "_bar_username", "") or "")
        if not want:
            return {}
        for u in (self._user_rows or []):
            if isinstance(u, dict) and str(u.get("username") or "") == want:
                return u
        return {}

    def _bar_toggle(self) -> None:
        u = self._selected_user()
        if u:
            self.on_toggle_user(str(u.get("username") or ""),
                                str(u.get("status") or "") != "disabled")

    def _bar_unbind(self) -> None:
        u = self._selected_user()
        if u:
            self.on_unbind(str(u.get("username") or ""))

    def _bar_delete(self) -> None:
        u = self._selected_user()
        if u:
            self.on_delete_user(str(u.get("username") or ""))

    def load_users(self) -> None:
        self._call("user_list", {}, ok=self.render_users,
                   btn=self.btn_user_refresh, busy_text="刷新中…")

    def load_stats(self) -> None:
        self._call("stats", {}, ok=self.render_stats)

    def render_stats(self, result) -> None:
        result = result if isinstance(result, dict) else {}
        total = int(result.get("users") or 0)
        active = int(result.get("active") or 0)
        disabled = int(result.get("disabled") or 0)
        pending = result.get("invites_pending")
        orphan = int(result.get("invites_orphan") or 0)
        text = f"共 {total} 个用户 · {active} 个正常 · {disabled} 个已停用"
        if orphan:
            text += f"    ·    {orphan} 条邀请码绑的用户已删除（服务端会自动清）"
        if pending is not None:
            text += f"    ·    {int(pending)} 个邀请码待使用"
        self.lbl_user_stats.configure(text=text)

    def render_users(self, result) -> None:
        rows = []
        if isinstance(result, dict):
            rows = result.get("users") or []
        elif isinstance(result, list):
            rows = result
        self._user_rows = rows

        self._tree_reset(self.tree_user)
        self._prune_links()
        for i, row in enumerate(rows):
            row = row if isinstance(row, dict) else {}
            username = str(row.get("username") or "")
            status = str(row.get("status") or "").lower()
            text, _color = USER_STATE.get(status,
                                          ("正常" if not status else status, MUTED))
            tag = "odd" if i % 2 else "even"
            iid = self.tree_user.insert(
                "", "end", tags=("odd" if i % 2 else "even",),
                values=(username, text, row.get("device_name") or "—",
                        fmt_ts(row.get("last_seen")), row.get("last_ip") or "—",
                        row.get("note") or "—"))

            if self.is_admin:
                bg = ROW_B if i % 2 else ROW_A
                holder = tk.Frame(self.tree_user, bg=bg)
                disabled = (status == "disabled")
                self._add_link(holder, "启用" if disabled else "停用",
                               # 点"停用" -> 目标是 disabled；点"启用" -> 目标是 active
                               lambda u=username, d=not disabled: self.on_toggle_user(u, d),
                               kind="ghost" if disabled else "normal", bg=bg)
                unbind = self._add_link(holder, "解绑设备",
                                        lambda u=username: self.on_unbind(u), bg=bg)
                self._add_link(holder, "删除", lambda u=username: self.on_delete_user(u),
                               kind="danger", bg=bg)
                # 没绑设备的用户，解绑是空操作，直接禁掉
                if not row.get("device_name"):
                    unbind.set_allowed(False)
                holder.update_idletasks()
                self._place_row_widget(self.tree_user, iid, holder)

        self.update_permissions()
        self.after(30, lambda: self._settle_row_widgets(self.tree_user))
        # 表格重建后选中状态被清掉，同步一下底部操作条
        try:
            self._on_user_select()
        except Exception:                          # noqa: BLE001
            pass
        # 用户列表变了 → 邀请码页的"孤儿"状态也会变（谁被删了）。
        # 用 _render_guard 挡住互相触发的递归：render_invites 里也会
        # update_permissions()，两边都不设防就会无限转下去。
        if self._invite_rows and not self._render_guard:
            self._render_guard = True
            try:
                self.render_invites({"invites": self._invite_rows})
            finally:
                self._render_guard = False

    def _add_link(self, holder, text, cmd, kind="ghost", bg=CARD) -> _Link:
        link = _Link(holder, text, cmd, kind=kind, bg=bg)
        link.pack(side="left", padx=2)
        self.row_links.append(link)
        return link

    # ================================================================ 用户：操作

    def _confirm(self, title: str, message: str, danger: bool = False) -> bool:
        """二次确认。ConfirmDialog 自己会阻塞，所以这里不需要 _wait()。"""
        try:
            return bool(ConfirmDialog(self, title, message, danger=danger).result)
        except Exception as exc:                   # noqa: BLE001
            self.app.toast(f"确认框打开失败：{exc}", "err")
            return False

    def on_toggle_user(self, username: str, disable: bool) -> None:
        """停用 / 启用一个用户。"""
        if disable:
            msg = (f"确定要停用「{username}」吗？\n\n"
                   f"停用后对方马上会断开连接，重新启用才能继续使用。")
        else:
            msg = f"确定要重新启用「{username}」吗？\n\n启用后对方可以立刻继续使用。"
        if not self._confirm("停用用户" if disable else "启用用户", msg, danger=disable):
            return
        status = "disabled" if disable else "active"
        self._call("user_set_status", {"username": username, "status": status},
                   ok=lambda _r: (self.app.toast("已停用" if disable else "已启用", "ok"),
                                  self.load_users(), self.load_stats()),
                   btn=self.btn_user_refresh)

    def on_unbind(self, username: str) -> None:
        if not self._confirm("解绑设备",
                             f"确定要解绑「{username}」的设备吗？\n\n"
                             f"解绑后对方需要重新输入邀请码激活。", danger=True):
            return
        self._call("user_unbind", {"username": username},
                   ok=lambda _r: (self.app.toast("设备已解绑", "ok"),
                                  self.load_users(), self.load_stats()),
                   btn=self.btn_user_refresh)

    def on_delete_user(self, username: str) -> None:
        if not self._confirm("删除用户",
                             f"确定要删除用户「{username}」吗？\n\n"
                             f"该用户的设备绑定和授权会立刻清除，此操作不可撤销。\n\n"
                             f"他名下的邀请码不会被删除：已经用过的会留成历史"
                             f"记录（默认隐藏，可勾选「显示历史」查看）；还没用过的"
                             f"会被撤销，防止拿它把已删的人再激活回来。",
                             danger=True):
            return

        def done(result):
            r = result if isinstance(result, dict) else {}
            kept = int(r.get("invites_kept") or 0)
            revoked = int(r.get("invites_revoked") or 0)
            bits = []
            if kept:
                bits.append(f"保留 {kept} 张历史邀请码")
            if revoked:
                bits.append(f"撤销 {revoked} 张未使用的")
            code_bit = ("，" + "、".join(bits)) if bits else ""
            who = r.get("nickname") or username
            self.app.toast(f"已删除用户 {who}{code_bit}", "ok")
            self.load_users()
            self.load_stats()
            self.load_invites()        # 邀请码页也要跟着刷新，不然还显示旧数据

        self._call("user_delete", {"username": username}, ok=done,
                   btn=self.btn_user_refresh)


# ---------------------------------------------------------------- 便捷入口

def open_admin(app) -> AdminWindow:
    """给主程序用的一行调用。"""
    return AdminWindow(app)


def open_admin_if_allowed(app):
    """分发包里不给开管理员控制台（配置里没有管理员密钥，开了也进不去）。"""
    import mclink_gui
    if getattr(mclink_gui, "DIST_MODE", False):
        return None
    return AdminWindow(app)


if __name__ == "__main__":                          # 手工调试用（需要真的 app，通常不用）
    print("mclink_admin.py 是模块，请在主程序里 from mclink_admin import AdminWindow")

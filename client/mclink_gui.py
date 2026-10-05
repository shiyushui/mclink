#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 桌面版  (mclink_gui.py)
==============================
把原来"后台进程 + 浏览器控制台"的方式做成一个真正的桌面程序：
双击打开一个原生窗口，隧道就在这个进程里跑，不需要开浏览器、不需要命令行。

架构（关键：只有一个进程，但分成两个线程）：
    ┌── 主线程 ────────────────┐   ┌── 引擎线程 ─────────────────┐
    │  tkinter 窗口 / 事件循环   │   │  asyncio 事件循环            │
    │  - 每 0.5s 读一次 state   │◀──│  - Agent（隧道、控制通道）    │
    │  - 用户操作 → call() 提交  │──▶│  - WebServer（可选，仅本机）  │
    └──────────────────────────┘   └─────────────────────────────┘
所有对 Agent 状态的读写都通过 `Engine.call()` 丢进引擎线程执行，
GUI 只读 `Engine.state` 这个已经算好的快照，因此不存在多线程竞争。

用法：
    pythonw mclink_gui.py            # 无控制台窗口启动（正式用法）
    python  mclink_gui.py --debug    # 带控制台，方便看报错
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import ctypes
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mclink_client as mc          # noqa: E402
import mclink_icon                  # noqa: E402

try:
    import mclink_winicon
except Exception:                    # 图标是可选增强，缺了也要能跑
    mclink_winicon = None

try:
    import mclink_tray
except Exception:                    # 托盘是可选增强，缺了也要能跑
    mclink_tray = None

APP_NAME = "McLink"
APP_VER = mc.VERSION

# 进程的 AppUserModelID。任务栏靠它把窗口和"正经软件"关联起来，
# 不设就会被归到 python.exe 名下（图标和名字都变 Python）。
AUMID = "McLink.PortMapper.Desktop.1"

# 分发包标记：make_dist.ps1 会在 config.client.json 里写 "dist": true。
# 分发版没有管理员密钥，所以界面上的「管理员」入口要藏掉 ——
# 留着也进不去，只会让朋友以为能用。
DIST_MODE = False


def is_dist_build(agent=None, config_path: str | None = None) -> bool:
    """这份客户端是不是 make_dist.ps1 打出来的分发包？

    判定看 config.client.json 根的 "dist": true。用配置而不是"当前目录里有没有
    mclink_admin.py"，是因为源码目录和分发目录的文件差别只有这个开关，写进配置
    最直白，也方便你自己想调试时手工改回来。

    引擎还没起来（agent 是 None）时直接读配置文件，免得界面比引擎早一步、
    结果把「管理员」按钮画出来。
    """
    try:
        if agent is not None:
            return bool((agent.cfg or {}).get("dist"))
    except Exception:                              # noqa: BLE001
        pass
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8-sig") as fh:
                return bool((json.load(fh) or {}).get("dist"))
        except Exception:                          # noqa: BLE001
            pass
    return False

# ---------------------------------------------------------------- 配色（与网页版一致）
BG = "#f4f7fb"
CARD = "#ffffff"
LINE = "#e7ecf3"
LINE_SOFT = "#f0f4f9"
TEXT = "#1b2434"
TEXT2 = "#4a5666"
MUTED = "#7b8798"
MINT = "#3ddc97"
SKY = "#35b6ff"
GREEN = "#12b76a"
AMBER = "#f59e0b"
RED = "#ef4444"
BLUE = "#2e90fa"
PURPLE = "#8b5cf6"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


def pick_font(root: tk.Tk) -> str:
    fams = set(tkfont.families(root))
    for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei"):
        if f in fams:
            return f
    return "TkDefaultFont"


# ---------------------------------------------------------------- 单实例保护

class SingleInstance:
    """只允许一个 McLink 在跑。

    没有这个的话，用户双击两下就会起两个进程：两个都去抢网页控制台端口，
    抢不到的那个引擎线程直接死掉 —— 表现就是"开了但什么都没在跑"，
    用户以为没启动就再双击一次，越开越多。

    现在第二次启动会把**已经在跑的那个窗口叫到前面来**，然后自己退出。

    实现：Windows 命名互斥体做判定（不占端口、进程退出由系统自动释放），
    再用一个本地回环 IPC 端口把 "show" 命令递过去。
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str, port_file: str):
        self.name = name
        self.port_file = port_file
        self._handle = None
        self._srv = None
        self.owns = False

    def acquire(self) -> bool:
        """返回 True 表示本进程是第一个实例。"""
        if os.name != "nt":
            self.owns = True
            return True
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_wchar_p]
            self._handle = k32.CreateMutexW(None, 0, self.name)
            self.owns = (ctypes.get_last_error() != self.ERROR_ALREADY_EXISTS)
        except Exception:                            # noqa: BLE001
            self.owns = True                         # 判定不了就放行，别挡用户
        return self.owns

    def signal_existing(self) -> bool:
        """让已经在跑的那个实例把窗口显示出来。"""
        try:
            with open(self.port_file, "r", encoding="utf-8-sig") as fh:
                port = int(fh.read().strip())
        except (OSError, ValueError):
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.sendall(b"show\n")
            return True
        except OSError:
            return False

    async def serve(self, on_show) -> None:
        """（第一个实例）起 IPC 监听，收到 show 就把窗口叫出来。"""
        try:
            self._srv = await asyncio.start_server(
                lambda r, w: self._conn(r, w, on_show), "127.0.0.1", 0)
        except OSError:
            return
        try:
            port = self._srv.sockets[0].getsockname()[1]
            d = os.path.dirname(os.path.abspath(self.port_file))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.port_file, "w", encoding="utf-8") as fh:
                fh.write(str(port))
        except (OSError, IndexError):
            pass

    @staticmethod
    async def _conn(reader, writer, on_show) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 3)
            if line.strip() == b"show":
                on_show()
        except Exception:                            # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def cleanup(self) -> None:
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.port_file):
                os.remove(self.port_file)
        except OSError:
            pass


# ---------------------------------------------------------------- 引擎线程

class Engine(threading.Thread):
    """在后台线程里跑 asyncio 事件循环 + McLink Agent。"""

    def __init__(self, config_path: str, enable_web: bool = True, on_started=None):
        super().__init__(daemon=True, name="mclink-engine")
        self.config_path = config_path
        self.enable_web = enable_web
        self.on_started = on_started         # 在引擎线程里、事件循环就绪后回调
        self.loop: asyncio.AbstractEventLoop | None = None
        self.agent: mc.Agent | None = None
        self.web = None
        self.state: dict | None = None
        self.logs: list = []
        self.ready = threading.Event()
        self.fatal: str | None = None
        self.web_port: int | None = None

    # ------------------------------------------------ 线程主体

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as exc:                     # noqa: BLE001
            self.fatal = f"{type(exc).__name__}: {exc}"
            import traceback
            traceback.print_exc()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass
            self.ready.set()

    async def _main(self) -> None:
        cfg = mc.load_config(self.config_path)
        mc.LOG = mc.LogBus(cfg.get("log_level", "info"), cfg.get("log_file", ""))
        self.agent = mc.Agent(cfg, self.config_path)

        if self.enable_web:
            try:
                self.web = mc.WebServer(self.agent)
                await self.web.start()
                self.web_port = self.web.port if self.web.srv else None
            except BaseException as exc:             # noqa: BLE001
                # 用 BaseException：万一还有人抛 SystemExit，也不能把引擎带走
                mc.LOG.warn(f"网页控制台启动失败（不影响桌面版使用）: {exc}")
                self.web = None
                self.web_port = None

        self.ready.set()
        if self.on_started is not None:
            try:
                self.on_started(self)
            except Exception as exc:                 # noqa: BLE001
                mc.LOG.warn(f"启动回调出错: {exc!r}")
        poll = asyncio.create_task(self._poll_loop())
        try:
            await self.agent.run()
        finally:
            poll.cancel()
            if self.web is not None:
                try:
                    await self.web.stop()
                except Exception:
                    pass

    async def _poll_loop(self) -> None:
        while True:
            try:
                self.state = self.agent.snapshot()
                self.logs = list(mc.LOG.history[-400:])
            except Exception:
                pass
            await asyncio.sleep(0.5)

    # ------------------------------------------------ 线程安全调用

    def call(self, fn, *args, timeout: float = 10, **kwargs):
        """在引擎线程（事件循环）里同步执行 fn，并把结果带回主线程。"""
        if self.loop is None or self.agent is None:
            raise RuntimeError("引擎尚未就绪")
        box: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            if box.set_running_or_notify_cancel() is False:
                return
            try:
                box.set_result(fn(*args, **kwargs))
            except BaseException as exc:             # noqa: BLE001
                box.set_exception(exc)

        self.loop.call_soon_threadsafe(runner)
        return box.result(timeout=timeout)

    def call_async(self, coro, timeout: float = 15):
        """把一个协程丢进引擎线程执行并等结果（会阻塞调用线程，只用于用户主动操作）。"""
        if self.loop is None or self.agent is None:
            raise RuntimeError("引擎尚未就绪")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit_async(self, coro, on_done=None, timeout: float = 0) -> None:
        """把一个协程丢进引擎线程跑，**不阻塞 UI**，完成后回主线程调 on_done。

        检查更新、下载更新包这类可能跑十几秒的活儿必须用这个，
        不然界面会卡住不动（看起来像死了）。
        on_done(result, error) 在主线程被调用。
        """
        if self.loop is None or self.agent is None:
            if on_done:
                on_done(None, "引擎尚未就绪")
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception as exc:                     # noqa: BLE001
            if on_done:
                on_done(None, str(exc))
            return
        if on_done is None:
            return

        def _done(f):
            try:
                res, err = f.result(), None
            except Exception as exc:                 # noqa: BLE001
                res, err = None, (str(exc) or "操作失败")
            post = getattr(self, "post_to_main", None)
            if post is None:
                try:
                    on_done(res, err)
                except Exception:                    # noqa: BLE001
                    pass
                return
            try:
                post(lambda: on_done(res, err))
            except Exception:                        # noqa: BLE001
                pass

        fut.add_done_callback(_done)

    def stop(self, timeout: float = 6) -> None:
        if self.loop is None or self.agent is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            fut.result(timeout)
        except Exception:
            pass

    async def _shutdown(self) -> None:
        try:
            await self.agent.shutdown()
        except Exception:
            pass
        if self.web is not None:
            try:
                await self.web.stop()
            except Exception:
                pass


# ---------------------------------------------------------------- 小工具

def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_rate(n: float) -> str:
    n = float(n or 0)
    if n < 1024:
        return f"{n:.0f} B/s"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB/s"
    return f"{n / 1048576:.1f} MB/s"


def fmt_uptime(sec: float) -> str:
    sec = int(sec or 0)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    if d:
        return f"{d}天{h}小时{m}分"
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def fmt_ranges(ranges) -> str:
    if not ranges:
        return "不限"
    return "、".join(f"{lo}-{hi}" if lo != hi else str(lo) for lo, hi in ranges)


class Tooltip:
    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#2b3444", fg="white",
                 font=(FONT, 9), padx=9, pady=5).pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ---------------------------------------------------------------- 圆角基础控件
# tkinter 没有圆角，全靠 Canvas 手画。这三个东西撑起了整个界面的观感。

def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画一个圆角矩形。

    用平滑多边形近似：在四个角各放两个重合的点，smooth=True 时
    只有这些重合点会被"拉圆"，直边保持笔直。
    """
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class PillButton(tk.Canvas):
    """胶囊形按钮（替代方方正正的 tk.Button）。

    刻意兼容 tk.Button 的常用接口：configure(text=/fg=/bg=/state=)、cget("text")、
    pack()/grid() —— 这样调用方和测试都不用改。
    """

    def __init__(self, parent, text, command=None, font=None, bg=LINE_SOFT, fg=TEXT2,
                 abg=None, padx=16, pady=7, radius=None, min_width=0, parent_bg=None):
        self._text = text
        self._bg = bg
        self._fg = fg
        self._abg = abg or bg
        self._padx = padx
        self._pady = pady
        self._state = "normal"
        self._command = command
        self._hovering = False
        self._font = tkfont.Font(font=font or (FONT, 9))
        self._min_width = min_width

        h = self._font.metrics("linespace") + 2 * pady
        w = max(self._font.measure(text) + 2 * padx, min_width)
        self._r = radius if radius is not None else h / 2.0

        try:
            pbg = parent_bg or parent.cget("bg")
        except Exception:
            pbg = CARD
        self._parent_bg = pbg

        super().__init__(parent, width=w, height=h, bg=pbg, highlightthickness=0,
                         bd=0, cursor="hand2")
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    # ---- 兼容 tk.Button 的接口 ----
    def configure(self, **kw):
        redraw = False
        for k, v in list(kw.items()):
            if k == "text":
                self._text = v
                tk.Canvas.configure(
                    self, width=max(self._font.measure(v) + 2 * self._padx,
                                    self._min_width))
                redraw = True
            elif k == "fg":
                self._fg = v
                redraw = True
            elif k == "bg":
                self._bg = v
                redraw = True
            elif k == "activebackground":
                self._abg = v
            elif k == "state":
                self._state = v
                tk.Canvas.configure(self, cursor="hand2" if v == "normal" else "arrow")
                redraw = True
            elif k == "command":
                self._command = v
            elif k == "font":
                self._font = tkfont.Font(font=v)
                redraw = True
            else:
                try:
                    tk.Canvas.configure(self, **{k: v})
                except Exception:
                    pass
        if redraw:
            self._draw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "fg":
            return self._fg
        if key == "bg":
            return self._bg
        if key == "state":
            return self._state
        return tk.Canvas.cget(self, key)

    # ---- 绘制与交互 ----
    def _set_hover(self, on):
        self._hovering = on
        self._draw()

    def _on_click(self, _event=None):
        if self._state != "normal":
            return
        if self._command:
            self._command()

    def _draw(self):
        self.delete("all")
        w = int(tk.Canvas.cget(self, "width"))
        h = int(tk.Canvas.cget(self, "height"))
        if self._state == "disabled":
            bg, fg = "#eff2f7", "#bcc5d2"
        else:
            bg = self._abg if self._hovering else self._bg
            fg = self._fg
        round_rect(self, 0, 0, w, h, self._r, fill=bg, outline="")
        self.create_text(w / 2.0, h / 2.0 + 1, text=self._text, fill=fg, font=self._font)


class RoundCard(tk.Frame):
    """圆角卡片容器：往 `.body` 里塞控件即可，高度自动跟着内容走。"""

    def __init__(self, parent, bg=CARD, border=LINE, radius=14, pad=13,
                 parent_bg=BG):
        super().__init__(parent, bg=parent_bg)
        self._cbg, self._border, self._r, self._pad = bg, border, radius, pad
        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(pad, pad, window=self.body, anchor="nw")
        self._shape = None
        self._h = 0
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_body(self, _event=None):
        want = self.body.winfo_reqheight() + 2 * self._pad
        if want != self._h:
            self._h = want
            self.canvas.configure(height=want)
        self._redraw()

    def _on_canvas(self, _event=None):
        w = self.canvas.winfo_width()
        if w > 1:
            self.canvas.itemconfigure(self._win, width=max(1, w - 2 * self._pad))
        self._redraw()

    def _redraw(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        if self._shape is not None:
            self.canvas.delete(self._shape)
        self._shape = round_rect(self.canvas, 1, 1, w - 1, h - 1, self._r,
                                 fill=self._cbg, outline=self._border, width=1)
        self.canvas.tag_lower(self._shape)

    def set_border(self, color: str) -> None:
        self._border = color
        self._redraw()


# ---------------------------------------------------------------- 主窗口

class McLinkApp:
    def __init__(self, config_path: str, enable_web: bool = True,
                 start_hidden: bool = False, on_started=None):
        self.config_path = config_path
        self.root = tk.Tk()
        self.root.withdraw()

        global FONT
        FONT = pick_font(self.root)
        # ⚠️ 字号别往小里调。Tk 的正数字号是**点**，8 点算下来汉字只有 ~12px 高，
        # 在 1080p 上中文基本糊成一团（用户反馈过副标题"字体有问题"）。
        # 小字统一提到 9/10，正文 11，保证中文可读。
        self.f_title = (FONT, 15, "bold")
        self.f_card_title = (FONT, 12, "bold")
        self.f_body = (FONT, 11)
        self.f_small = (FONT, 10)
        self.f_tiny = (FONT, 9)
        self.f_stat = (FONT, 18, "bold")
        self.f_mono = (MONO, 10)
        self.f_mono_b = (MONO, 11, "bold")

        self.engine = Engine(config_path, enable_web=enable_web, on_started=on_started)
        # 引擎线程里跑完的异步任务要回到主线程刷界面，得先把这个口子接上
        self.engine.post_to_main = self.post
        self.engine.start()
        self.cards: dict[str, "MappingCard"] = {}
        self.cmd_queue: queue.Queue = queue.Queue()
        self._last_log_key = 0
        self._toast_after = None
        self.tray = None
        self._quitting = False
        self.start_hidden = start_hidden
        self._upd_bar_shown = False
        self._upd_busy = False
        self._upd_dismissed = False
        self._upd_last = {}          # 最近一次 update_info（「详情」窗口用）

        global DIST_MODE
        DIST_MODE = is_dist_build(self.engine.agent, config_path)

        self._build()
        self._load_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._tick)
        self.root.after(400, self._setup_tray)
        self.root.after(1200, self._setup_taskbar_shortcut)

    def _setup_taskbar_shortcut(self) -> None:
        """建一个开始菜单快捷方式，让任务栏按钮的标题和图标都对上。

        只在分发包里做：你自己的源码目录直接从桌面快捷方式启动就行，不用往
        开始菜单里塞东西。失败静默跳过（图标已经由 _load_icon 设好了）。
        """
        if not DIST_MODE or mclink_winicon is None:
            return
        try:
            exe = os.path.join(HERE, "McLink.exe")
            if not os.path.exists(exe):
                return
            ico = os.path.join(HERE, "assets", "mclink.ico")
            mclink_winicon.ensure_taskbar_shortcut(
                AUMID, exe, icon=ico if os.path.exists(ico) else None)
        except Exception:                          # noqa: BLE001
            pass

    # ------------------------------------------------ 外观

    def _load_icon(self) -> None:
        """优先用 assets 里的真实图标；没有才退回内置生成的箭头图标。

        Windows 任务栏认的是「窗口图标 + 窗口类图标 + AppUserModelID」，
        光设 tkinter 的 iconbitmap / iconphoto 是不够的（Tk 8.6 只往窗口类里
        放一个图标，实测 WM_GETICON 仍然是空），任务栏就会退回 pythonw.exe
        的 Python 图标。所以这里再补一刀 Win32 的 WM_SETICON + 类图标。
        """
        ico = os.path.join(HERE, "assets", "mclink.ico")
        png64 = os.path.join(HERE, "assets", "mclink-64.png")
        self._icon_img = None
        try:
            if os.path.exists(png64):
                self._icon_img = tk.PhotoImage(file=png64)
            else:
                import base64
                self._icon_img = tk.PhotoImage(
                    data=base64.b64encode(mclink_icon.png_bytes(64)).decode("ascii"))
        except Exception:
            self._icon_img = None
        # iconbitmap 用 .ico，Windows 才能拿到多尺寸（小图标/大图标/Alt-Tab 各取所需）
        try:
            if os.path.exists(ico):
                self.root.iconbitmap(default=ico)
        except Exception:
            pass
        if self._icon_img is not None:
            try:
                self.root.iconphoto(True, self._icon_img)
            except Exception:
                pass
        self._apply_win_icon(ico)

    def _apply_win_icon(self, ico: str) -> None:
        """用 Win32 把图标真正设到窗口上 —— 任务栏显示哪个图标就看这一步。

        必须等窗口真的创建出来（拿到 HWND）之后再设，所以先 update_idletasks，
        再靠 after() 延后一拍重复一次，防止窗口映射时又被覆盖掉。
        """
        if mclink_winicon is None:
            return

        def _set() -> None:
            try:
                mclink_winicon.apply_window_icon(self.root, ico=ico)
            except Exception:                     # noqa: BLE001
                pass

        try:
            self.root.update_idletasks()
        except Exception:
            pass
        _set()
        try:
            self.root.after(60, _set)
        except Exception:
            pass

    def _build(self) -> None:
        r = self.root
        r.title(f"{APP_NAME} · 端口映射")
        r.configure(bg=BG)
        r.geometry("1000x748")
        r.minsize(880, 600)

        self._build_header()
        self._build_license_bar()
        self._build_update_bar()
        self._build_stats()
        # 先从底部占位，再让中间区域 expand，否则日志会被列表挤扁
        self._build_statusbar()
        self._build_log()

        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(4, 0))

        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(2, 10))
        self.btn_add = self._btn(bar, "＋  新增映射", self.on_add, kind="primary")
        self.btn_add.pack(side="left")
        # 启动时默认不自动开映射，所以给一个一键开始/停下的入口
        self.btn_start_all = self._btn(bar, "▶  启动全部映射", self.on_start_all,
                                       kind="normal")
        self.btn_start_all.pack(side="left", padx=(8, 0))
        self.btn_stop_all = self._btn(bar, "■  全部停止", self.on_stop_all,
                                      kind="ghost")
        self.btn_stop_all.pack(side="left", padx=(6, 0))
        self.lbl_hint = tk.Label(bar, text="", bg=BG, fg=MUTED, font=self.f_small)
        self.lbl_hint.pack(side="right")

        # ---- 映射列表（可滚动） ----
        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                                 width=12, troughcolor=BG, bg="#cdd6e3", relief="flat",
                                 activebackground="#b6c2d2", borderwidth=0)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self._list_win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._list_win, width=e.width))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.list_frame.bind("<MouseWheel>", self._on_wheel)

        self.empty_box = self._build_empty(self.list_frame)

    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=CARD, height=66)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", padx=18, pady=12)
        logo = tk.Canvas(left, width=34, height=34, bg=CARD, highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        self._draw_logo(logo)
        txt = tk.Frame(left, bg=CARD)
        txt.pack(side="left")
        tk.Label(txt, text=APP_NAME, bg=CARD, fg=TEXT,
                 font=self.f_title).pack(anchor="w")
        # 副标题用 f_small（10 点）而不是 f_tiny —— 这是品牌区，糊了很难看
        tk.Label(txt, text="端口映射 · 内网穿透", bg=CARD, fg=MUTED,
                 font=self.f_small).pack(anchor="w")

        right = tk.Frame(head, bg=CARD)
        right.pack(side="right", padx=18, pady=12)

        self.btn_settings = self._btn(right, "设置", self.on_settings, kind="ghost")
        self.btn_settings.pack(side="right", padx=(8, 0))
        # 「管理员」只在管理员自己的客户端上出现：分发包里 admin_token 是空的，
        # 点了也进不去，只会让人以为是坏了。分发版直接不建这个按钮。
        self.btn_admin = None
        if not DIST_MODE:
            self.btn_admin = self._btn(right, "管理员", self.on_admin, kind="ghost")
            self.btn_admin.pack(side="right", padx=(8, 0))
        self.btn_reconnect = self._btn(right, "重连", self.on_reconnect, kind="ghost")
        self.btn_reconnect.pack(side="right")

        self.pill = tk.Frame(right, bg=LINE_SOFT, padx=12, pady=6)
        self.pill.pack(side="right", padx=(0, 10))
        self.pill_dot = tk.Canvas(self.pill, width=9, height=9, bg=LINE_SOFT,
                                  highlightthickness=0)
        self.pill_dot.pack(side="left", padx=(0, 7))
        self._dot = self.pill_dot.create_oval(1, 1, 8, 8, fill=MUTED, outline="")
        self.pill_text = tk.Label(self.pill, text="正在启动…", bg=LINE_SOFT, fg=TEXT2,
                                  font=self.f_small)
        self.pill_text.pack(side="left")

    def _draw_logo(self, cv: tk.Canvas) -> None:
        """用圆角矩形 + 双箭头画 logo（和生成的图标同款）。"""
        w = h = 34
        r = 9
        # 竖向渐变用线段近似
        for i in range(h):
            t = i / (h - 1)
            col = "#%02x%02x%02x" % (
                int(0x3d + (0x35 - 0x3d) * t),
                int(0xdc + (0xb6 - 0xdc) * t),
                int(0x97 + (0xff - 0x97) * t))
            cv.create_line(0, i, w, i, fill=col)
        # 用四个扇形把方角"啃"成圆角（扇形圆心正好是圆角圆心，半径 r）
        # Tk 角度：0°=东，逆时针为正
        for (cx, cy, start) in ((r, r, 90), (w - r, r, 0),
                                (r, h - r, 180), (w - r, h - r, 270)):
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=90,
                          style="pieslice", fill=CARD, outline=CARD)
        # 双箭头
        cv.create_line(9, 13, 23, 13, fill="white", width=3, arrow="last",
                       arrowshape=(7, 8, 3))
        cv.create_line(25, 21, 11, 21, fill="white", width=3, arrow="last",
                       arrowshape=(7, 8, 3))

    def _build_license_bar(self) -> None:
        """未授权时显示在顶部的一条提醒（授权正常时完全隐藏）。"""
        bar = tk.Frame(self.root, bg="#fff6e8")
        wrap = tk.Frame(bar, bg="#fff6e8")
        wrap.pack(fill="x", padx=18, pady=9)
        tk.Label(wrap, text="🔒", bg="#fff6e8", fg=AMBER,
                 font=(FONT, 12, "bold")).pack(side="left", padx=(0, 8))
        self.lic_text = tk.Label(wrap, text="", bg="#fff6e8", fg="#9a5b06",
                                 font=self.f_small)
        self.lic_text.pack(side="left")
        self._btn(wrap, "输入密钥激活", self.on_activate,
                  kind="primary").pack(side="right")
        tk.Frame(bar, bg="#f3e0c2", height=1).pack(fill="x", side="bottom")
        self.license_bar = bar
        self._lic_bar_shown = False

    def _build_update_bar(self) -> None:
        """发现新版本时显示在顶部的一条提示（不自动装，用户点了才更新）。

        ⚠️ 布局要点：**按钮必须抢在文字前面拿到宽度**。
        原来用 pack(side="left") 放文字、pack(side="right") 放按钮 ——
        packer 是按顺序分空间的，文字标签先要走了它需要的全部宽度，
        窗口一窄（非全屏）按钮就被挤到可视区外面，表现就是"看不到下载按钮"。
        现在改成 grid：右边按钮列给固定宽度，文字列拿去剩下多少用多少，
        并且按像素把文字截断到刚好放得下 —— 窗口再窄按钮也在。
        """
        bar = tk.Frame(self.root, bg="#eef4ff")
        wrap = tk.Frame(bar, bg="#eef4ff")
        wrap.pack(fill="x", padx=18, pady=9)
        # 列：0=图标(固定) 1=文字(伸缩) 2=进度条(仅下载时) 3=按钮(固定)
        wrap.columnconfigure(1, weight=1)
        self._upd_wrap = wrap

        tk.Label(wrap, text="⬆", bg="#eef4ff", fg=BLUE,
                 font=(FONT, 12, "bold")).grid(row=0, column=0, padx=(0, 8),
                                               sticky="w")
        self.upd_text = tk.Label(wrap, text="", bg="#eef4ff", fg="#1a4b8f",
                                 font=self.f_small, anchor="w", justify="left")
        self.upd_text.grid(row=0, column=1, sticky="w")

        # 按钮容器：整体放第 3 列，里面按视觉顺序从右往左 pack
        btns = tk.Frame(wrap, bg="#eef4ff")
        btns.grid(row=0, column=3, sticky="e")
        self._upd_btns = btns
        self.btn_update = self._btn(btns, "立即更新", self.on_update_clicked,
                                    kind="primary")
        self.btn_update.pack(side="right")
        self.btn_update_later = self._btn(btns, "以后再说", self.on_update_later,
                                         kind="ghost")
        self.btn_update_later.pack(side="right", padx=(0, 8))
        # 「详情」：说明太长时顶栏放不下，点开看全的（可滚动）
        self.btn_update_notes = self._btn(btns, "详情", self.on_show_update_notes,
                                          kind="ghost")
        self.btn_update_notes.pack(side="right", padx=(0, 8))

        # 下载进度条：只在下载中出现，平时完全隐藏（不占空间）
        self.upd_prog = tk.Canvas(wrap, width=220, height=8, bg="#eef4ff",
                                  highlightthickness=0, bd=0)
        self._upd_prog_track = None
        self._upd_prog_fill = None
        # 窗口尺寸一变就重新裁剪文字，保证按钮始终可见
        wrap.bind("<Configure>", lambda _e: self._refit_upd_text())
        tk.Frame(bar, bg="#c9dcf7", height=1).pack(fill="x", side="bottom")
        self.update_bar = bar
        self._upd_bar_shown = False
        self._upd_prog_shown = False
        self._upd_notes_shown = False
        self._upd_full_text = ""       # 未截断的完整文字

    # ---------------------------------------------------------- 顶部提示条文字

    def _upd_text_set(self, text: str) -> None:
        """记住完整文字，然后按当前可用宽度裁剪显示。"""
        self._upd_full_text = text or ""
        self._refit_upd_text()

    def _refit_upd_text(self) -> None:
        """把提示条文字截断到"放得下"的长度，给右边按钮留出空间。

        做法是量像素而不是数字符：中文和英文宽度差很多，按字符数截会算不准。
        """
        full = getattr(self, "_upd_full_text", "")
        lbl = getattr(self, "upd_text", None)
        if lbl is None:
            return
        if not full:
            lbl.configure(text="")
            return
        try:
            wrap = self._upd_wrap
            btns = self._upd_btns
            # 可用 = 容器宽 - 图标 - 按钮组 - 进度条 - 若干间距
            icon_w = 30
            btn_w = btns.winfo_reqwidth()
            prog_w = 0
            if getattr(self, "_upd_prog_shown", False):
                progress = getattr(self, "upd_prog", None)
                prog_w = (progress.winfo_reqwidth() + 14) if progress else 0
            avail = wrap.winfo_width() - icon_w - btn_w - prog_w - 24
            if avail <= 40:
                # 实在太窄：只留一句最短的提示，别把按钮挤没了
                lbl.configure(text="有新版本")
                return
            font = tkfont.Font(font=self.f_small)
            if font.measure(full) <= avail:
                lbl.configure(text=full)
                return
            ell = "…"
            ew = font.measure(ell)
            lo, hi = 0, len(full)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(full[:mid]) + ew <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            lbl.configure(text=(full[:lo].rstrip() + ell) if lo > 0 else "有新版本")
        except Exception:                            # noqa: BLE001
            try:
                lbl.configure(text=full[:40])
            except Exception:                        # noqa: BLE001
                pass

    def _draw_progress(self, frac: float) -> None:
        """把进度条画出来（frac 0~1）。首次调用时把 track/fill 两个矩形建好。"""
        cv = self.upd_prog
        try:
            w = int(cv.cget("width"))
            h = int(cv.cget("height"))
        except Exception:                            # noqa: BLE001
            return
        frac = max(0.0, min(1.0, float(frac or 0.0)))
        if self._upd_prog_track is None:
            self._upd_prog_track = cv.create_rectangle(
                0, 0, w, h, fill="#c9dcf7", outline="")
        if self._upd_prog_fill is None:
            self._upd_prog_fill = cv.create_rectangle(
                0, 0, 0, h, fill=BLUE, outline="")
        cv.coords(self._upd_prog_fill, 0, 0, int(w * frac), h)

    def _build_stats(self) -> None:
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=18, pady=(14, 6))
        self._stats_row = row
        self.stat_vals = {}
        specs = [("up", "上传速率", "↑", SKY), ("down", "下载速率", "↓", MINT),
                 ("conn", "活跃连接", "⇅", BLUE), ("uptime", "运行时长", "◷", PURPLE)]
        for i, (key, title, glyph, color) in enumerate(specs):
            row.columnconfigure(i, weight=1, uniform="stat")
            card = RoundCard(row, radius=16, pad=14)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 9, 0))
            box = card.body
            top = tk.Frame(box, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text=glyph, bg=CARD, fg=color,
                     font=(FONT, 12, "bold")).pack(side="left", padx=(0, 6))
            tk.Label(top, text=title, bg=CARD, fg=MUTED, font=self.f_small).pack(side="left")
            val = tk.Label(box, text="—", bg=CARD, fg=TEXT, font=self.f_stat)
            val.pack(anchor="w", pady=(2, 0))
            self.stat_vals[key] = val

    def _build_empty(self, parent) -> tk.Frame:
        box = RoundCard(parent, radius=16, pad=22)
        pad = tk.Frame(box.body, bg=CARD)
        pad.pack(fill="x")
        tk.Label(pad, text="还没有任何映射", bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        steps = [
            "1. 到阿里云控制台的「防火墙」放行端口：TCP 7000、UDP 7000，"
            "以及游戏端口池（例如 TCP + UDP 各 25000/26000）。",
            "2. 点上方「＋ 新增映射」，选一个游戏模板，或自己填本地端口。",
            "3. 把卡片上的「连接地址」发给朋友，他们直接连进来就能玩。",
        ]
        for s in steps:
            tk.Label(pad, text=s, bg=CARD, fg=TEXT2, font=self.f_small,
                     justify="left", wraplength=760, anchor="w").pack(
                anchor="w", pady=(8 if s.startswith("1") else 4, 0))
        return box

    def _build_log(self) -> None:
        self.log_open = tk.BooleanVar(value=True)
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="x", padx=18, pady=(8, 0), side="bottom")
        self.log_wrap = wrap

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x")
        self.log_toggle = tk.Label(head, text="▾  运行日志", bg=BG, fg=TEXT2,
                                   font=(FONT, 10, "bold"), cursor="hand2")
        self.log_toggle.pack(side="left")
        self.log_toggle.bind("<Button-1>", lambda e: self.on_toggle_log())
        self.log_autoscroll = tk.BooleanVar(value=True)
        self._btn(head, "暂停滚动", self.on_toggle_scroll, kind="ghost").pack(side="right")
        self._btn(head, "清空", self.on_clear_log, kind="ghost").pack(
            side="right", padx=(0, 6))

        self.log_box = RoundCard(wrap, bg="#fbfcfe", radius=14, pad=11, parent_bg=BG)
        self.log_box.pack(fill="x", pady=(6, 0))
        inner = self.log_box.body
        self.log_text = tk.Text(inner, height=7, bg="#fbfcfe", fg=TEXT2, bd=0,
                                highlightthickness=0, font=self.f_mono, wrap="none",
                                state="disabled", padx=10, pady=8)
        sb = tk.Scrollbar(inner, command=self.log_text.yview, width=11,
                          troughcolor="#fbfcfe", bg="#cdd6e3", relief="flat",
                          borderwidth=0, activebackground="#b6c2d2")
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("info", foreground=TEXT2)
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("debug", foreground=MUTED)

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=CARD, height=30)
        bar.pack(fill="x", side="bottom")
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.lbl_toast = tk.Label(bar, text=f"{APP_NAME} v{APP_VER}", bg=CARD, fg=MUTED,
                                  font=self.f_tiny)
        self.lbl_toast.pack(side="left", padx=16)
        self.lbl_right = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=self.f_tiny)
        self.lbl_right.pack(side="right", padx=16)

    # ------------------------------------------------ 通用控件

    def _btn(self, parent, text, cmd, kind="normal", width=None):
        styles = {
            "primary": dict(bg=SKY, fg="white", abg="#1ea3ec"),
            "ghost": dict(bg=LINE_SOFT, fg=TEXT2, abg="#e4ebf5"),
            "danger": dict(bg="#fdecec", fg=RED, abg="#fbdcdc"),
            "normal": dict(bg="#eef2f8", fg=TEXT2, abg="#e4ebf5"),
        }
        s = styles.get(kind, styles["normal"])
        minw = 0
        if width:
            minw = int(width * tkfont.Font(font=self.f_small).measure("0"))
        return PillButton(parent, text, cmd, font=self.f_small, bg=s["bg"],
                          fg=s["fg"], abg=s["abg"], min_width=minw)

    def toast(self, text: str, level: str = "info", ms: int = 2600) -> None:
        color = {"ok": GREEN, "err": RED, "warn": AMBER}.get(level, TEXT2)
        self.lbl_toast.configure(text=text, fg=color)
        if self._toast_after:
            try:
                self.root.after_cancel(self._toast_after)
            except Exception:
                pass
        self._toast_after = self.root.after(
            ms, lambda: self.lbl_toast.configure(text=f"{APP_NAME} v{APP_VER}", fg=MUTED))

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def copy(self, text: str, label: str = "已复制") -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.toast(f"{label}：{text}", "ok")
        except Exception as exc:
            self.toast(f"复制失败：{exc}", "err")

    # ------------------------------------------------ 定时刷新

    def _tick(self) -> None:
        if self._quitting:
            return
        eng = self.engine
        if eng.fatal:
            self.pill_text.configure(text="引擎启动失败")
            self.pill_dot.itemconfigure(self._dot, fill=RED)
            self.toast(eng.fatal, "err", 8000)
        st = eng.state
        if st:
            self._render(st)
            self._render_logs(eng.logs)
            self._sync_tray_tip(st)
        elif not eng.ready.is_set():
            self.pill_text.configure(text="正在启动…")
        self.root.after(500, self._tick)

    def _render(self, st: dict) -> None:
        sv = st.get("server", {})
        stats = st.get("stats", {})
        ag = st.get("agent", {})

        connected = bool(sv.get("connected"))
        recon = sv.get("reconnect_in_s")
        if connected:
            self.pill_dot.itemconfigure(self._dot, fill=GREEN)
            lat = sv.get("latency_ms")
            self.pill_text.configure(
                text=f"已连接 {sv.get('public_ip') or sv.get('host')}"
                     + (f" · {lat}ms" if lat else ""))
        elif recon:
            self.pill_dot.itemconfigure(self._dot, fill=AMBER)
            self.pill_text.configure(text=f"{int(recon) + 1} 秒后重连…")
        else:
            self.pill_dot.itemconfigure(self._dot, fill=RED if sv.get("last_error") else MUTED)
            self.pill_text.configure(text="未连接")

        # 未授权提示条。这里用自己维护的布尔量而不是 winfo_ismapped()：
        # 窗口最小化/隐藏时 ismapped 会返回 0，会导致反复 pack。
        lic = st.get("license") or {}
        need_lic = bool(lic.get("required")) and not bool(lic.get("licensed"))
        if need_lic:
            reason = lic.get("reason") or "需要一次性密钥激活后才能使用"
            self.lic_text.configure(text=f"未授权 · {reason}")
            if not self._lic_bar_shown:
                self.license_bar.pack(fill="x", before=self._stats_row)
                self._lic_bar_shown = True
        elif self._lic_bar_shown:
            self.license_bar.pack_forget()
            self._lic_bar_shown = False

        self._render_update_bar(st.get("update") or {})

        self.stat_vals["up"].configure(text=fmt_rate(stats.get("tx_rate", 0)))
        self.stat_vals["down"].configure(text=fmt_rate(stats.get("rx_rate", 0)))
        self.stat_vals["conn"].configure(
            text=f"{stats.get('tcp_conns', 0) + stats.get('udp_sessions', 0)}")
        self.stat_vals["uptime"].configure(text=fmt_uptime(ag.get("uptime_s", 0)))

        maps = st.get("mappings") or []
        self.lbl_hint.configure(
            text=f"共 {len(maps)} 条映射    ·    控制台 127.0.0.1:{self.engine.web_port}"
            if self.engine.web_port else f"共 {len(maps)} 条映射")
        self.lbl_right.configure(
            text=f"{sv.get('host')}:{sv.get('control_port')}   ·   进程 {ag.get('pid')}")

        seen = set()
        for i, m in enumerate(maps):
            mid = m.get("id")
            seen.add(mid)
            card = self.cards.get(mid)
            if card is None:
                card = MappingCard(self.list_frame, self, m)
                self.cards[mid] = card
            card.grid(row=i, column=0, sticky="ew", pady=(0, 8), padx=(0, 6))
            card.update_view(m)
        for mid in list(self.cards):
            if mid not in seen:
                self.cards.pop(mid).destroy()
        self.list_frame.columnconfigure(0, weight=1)

        if maps:
            self.empty_box.grid_forget()
        else:
            self.empty_box.grid(row=0, column=0, sticky="ew", pady=(0, 8), padx=(0, 6))

        if sv.get("last_error") and not connected:
            self.lbl_right.configure(text=str(sv["last_error"])[:70])

    def _render_logs(self, logs: list) -> None:
        if not logs:
            return
        key = (len(logs), logs[-1].get("ts"), logs[-1].get("msg"))
        if key == self._last_log_key:
            return
        self._last_log_key = key
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for rec in logs[-250:]:
            ts = time.strftime("%H:%M:%S", time.localtime(rec.get("ts", 0)))
            lvl = rec.get("level", "info")
            self.log_text.insert("end", f"{ts}  {rec.get('msg', '')}\n", lvl)
        if self.log_autoscroll.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------ 交互

    def _submit(self, fn, *a, ok_msg=None, **kw):
        try:
            res = self.engine.call(fn, *a, **kw)
            if ok_msg:
                self.toast(ok_msg, "ok")
            return res
        except concurrent.futures.TimeoutError:
            self.toast("操作超时：引擎没有响应", "err")
        except Exception as exc:                     # noqa: BLE001
            self.toast(str(exc), "err", 5000)
        return None

    def on_add(self) -> None:
        AddEditDialog(self, None)

    def on_edit(self, mapping: dict) -> None:
        AddEditDialog(self, mapping)

    def on_toggle(self, mid: str, enabled: bool) -> None:
        self._submit(self.engine.agent.set_enabled, mid, enabled,
                     ok_msg=("已启用，正在注册…" if enabled else "已停用，公网端口已关闭"))

    def on_start_all(self) -> None:
        """一键开始：把所有映射放行并启用。"""
        agent = self.engine.agent
        if agent is None or not agent.mappings:
            self.toast("还没有映射，先点「＋ 新增映射」", "info", 3000)
            return
        n = self._submit(agent.start_all_mappings)
        if n:
            self.toast(f"已启动 {n} 条映射，正在注册…", "ok", 3000)
        else:
            self.toast("所有映射都已经在跑了", "info", 2500)

    def on_stop_all(self) -> None:
        """一键停止：所有映射停用，公网端口立即关闭。"""
        agent = self.engine.agent
        if agent is None or not agent.mappings:
            return
        if not ConfirmDialog(self.root, "全部停止",
                             "确定要停止所有映射吗？\n\n"
                             "停止后所有公网端口立即关闭，朋友会立刻断开。").result:
            return
        n = self._submit(agent.stop_all_mappings)
        if n:
            self.toast(f"已停止 {n} 条映射", "ok", 3000)

    def on_delete(self, mapping: dict) -> None:
        if not ConfirmDialog(self.root, "删除映射",
                             f"确定要删除「{mapping.get('name')}」吗？\n\n"
                             f"删除后公网端口 {mapping.get('remote_port')} 将立即关闭，"
                             f"此操作不可撤销。", danger=True).result:
            return
        self._submit(self.engine.agent.delete, mapping.get("id"), ok_msg="已删除")

    def on_reconnect(self) -> None:
        agent = self.engine.agent
        if agent is None:
            return
        if agent.control_writer:
            try:
                agent.control_writer.close()
            except Exception:
                pass
        self.toast("正在重新连接服务器…", "info")

    def on_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def on_toggle_scroll(self) -> None:
        self.log_autoscroll.set(not self.log_autoscroll.get())
        self.toast("日志自动滚动：" + ("开" if self.log_autoscroll.get() else "关"))

    def on_toggle_log(self) -> None:
        if self.log_open.get():
            self.log_box.pack_forget()
            self.log_toggle.configure(text="▸  运行日志")
            self.log_open.set(False)
        else:
            self.log_box.pack(fill="x", pady=(6, 0))
            self.log_toggle.configure(text="▾  运行日志")
            self.log_open.set(True)

    def _render_update_bar(self, upd: dict) -> None:
        """按更新状态显示/隐藏顶部提示条。

        只做"提示 + 一个按钮"：下载和替换都等用户点了才做，
        因为替换文件必须先退出程序 —— 不能替用户决定什么时候断隧道。
        """
        self._upd_last = dict(upd)      # 「详情」窗口要用最新的说明
        if self._upd_dismissed and not upd.get("downloading"):
            show = False
        else:
            show = bool(upd.get("available")) or bool(upd.get("downloading")) \
                or bool(upd.get("applying"))
        if show:
            downloading = bool(upd.get("downloading"))
            if upd.get("applying"):
                self._upd_text_set("更新包已就绪，正在退出并安装…")
                self.btn_update.configure(text="安装中…", state="disabled")
                self.btn_update_later.configure(state="disabled")
            elif downloading:
                frac = float(upd.get("progress") or 0)
                pct = int(frac * 100)
                size = int(upd.get("size") or 0)
                done_bytes = int(size * frac) if size else 0
                self._upd_text_set(
                    f"正在下载新版本 {upd.get('latest') or ''} … {pct}%"
                    + (f"（{fmt_bytes(done_bytes)} / {fmt_bytes(size)}）"
                       if size else ""))
                self.btn_update.configure(text="下载中…", state="disabled")
                self.btn_update_later.configure(state="disabled")
                self._draw_progress(frac)
            else:
                notes = str(upd.get("notes") or "").strip()
                size = fmt_bytes(upd.get("size") or 0)
                text = (f"有新版本 {upd.get('latest') or '?'}"
                        f"（当前 {upd.get('current') or APP_VER}，{size}）")
                if notes:
                    # 顶栏只放一行，取第一行；想看全文点「详情」
                    first = notes.splitlines()[0].strip()
                    text += "：" + (first[:60] + "…" if len(first) > 60
                                    or len(notes.splitlines()) > 1 else first)
                self._upd_text_set(text)
                self.btn_update.configure(text="立即更新", state="normal")
                self.btn_update_later.configure(state="normal")
                # 有说明才给「详情」按钮
                try:
                    if notes and not self._upd_notes_shown:
                        self.btn_update_notes.pack(side="right", padx=(0, 8))
                        self._upd_notes_shown = True
                    elif not notes and self._upd_notes_shown:
                        self.btn_update_notes.pack_forget()
                        self._upd_notes_shown = False
                except Exception:                    # noqa: BLE001
                    pass

            # 进度条只在下载时出现，放在文字和按钮之间（grid 第 2 列）
            if downloading and not self._upd_prog_shown:
                try:
                    self.upd_prog.grid(row=0, column=2, padx=(14, 0), sticky="w")
                except Exception:                    # noqa: BLE001
                    pass
                self._upd_prog_shown = True
            elif not downloading and self._upd_prog_shown:
                try:
                    self.upd_prog.grid_forget()
                except Exception:                    # noqa: BLE001
                    pass
                self._upd_prog_shown = False
                self._upd_prog_track = None
                self._upd_prog_fill = None
                try:
                    self.upd_prog.delete("all")
                except Exception:                    # noqa: BLE001
                    pass

            if not self._upd_bar_shown:
                self.update_bar.pack(fill="x", before=self._stats_row)
                self._upd_bar_shown = True
        elif self._upd_bar_shown:
            self.update_bar.pack_forget()
            self._upd_bar_shown = False
            if self._upd_prog_shown:
                try:
                    self.upd_prog.grid_forget()
                except Exception:                    # noqa: BLE001
                    pass
                self._upd_prog_shown = False

    def on_update_later(self) -> None:
        """这次先不更新（重新启动客户端后会再问一次）。"""
        self._upd_dismissed = True
        self.toast("已跳过本次更新，重启客户端后会再提示", "info", 3000)
        if self._upd_bar_shown:
            self.update_bar.pack_forget()
            self._upd_bar_shown = False

    def on_show_update_notes(self) -> None:
        """点「详情」：在一个可滚动的窗口里看完整的新版本说明。"""
        upd = self._upd_last or {}
        latest = upd.get("latest") or "新版本"
        cur = upd.get("current") or APP_VER
        size = fmt_bytes(upd.get("size") or 0)
        head = f"McLink {latest}    （当前 {cur}，{size}）"
        try:
            UpdateNotesDialog(self.root, f"新版本 {latest}",
                              head, str(upd.get("notes") or ""))
        except Exception as exc:                     # noqa: BLE001
            self.toast(f"打开说明失败：{exc}", "err", 4000)

    def on_update_clicked(self) -> None:
        """用户点了「立即更新」：下载 → 校验 → 交给更新助手替换文件并重启。"""
        agent = self.engine.agent
        if agent is None:
            self.toast("引擎未就绪", "err")
            return
        if self._upd_busy:
            return
        self._upd_busy = True
        self.btn_update.configure(text="下载中…", state="disabled")
        self.btn_update_later.configure(state="disabled")
        self._upd_dismissed = False
        # 亮出"下载到哪儿"，免得用户找不到 —— 路径就在客户端目录下的
        # update-cache\，更新日志也在里面。
        self._upd_text_set(f"更新包会下载到 {os.path.join(HERE, 'update-cache')} …")

        def done(_res, err):
            self._upd_busy = False
            if err:
                self.toast(f"更新失败：{err}", "err", 6000)
                self.btn_update.configure(text="立即更新", state="normal")
                self.btn_update_later.configure(state="normal")
                return
            try:
                log = agent.apply_update()
            except Exception as exc:                 # noqa: BLE001
                self.toast(f"启动更新助手失败：{exc}", "err", 6000)
                self.btn_update.configure(text="立即更新", state="normal")
                self.btn_update_later.configure(state="normal")
                return
            self.toast("更新包已下载，正在退出并安装…", "ok", 4000)
            try:
                mc.LOG.info(f"更新包已保存，更新日志：{log}")
            except Exception:                        # noqa: BLE001
                pass
            # 给界面一点时间把提示画出来，然后自己退出，让助手换文件
            self.root.after(1200, self.quit_app)

        self.engine.submit_async(agent.download_update(), done)

    def on_check_update(self) -> None:
        """设置窗口里的「检查更新」按钮。"""
        agent = self.engine.agent
        if agent is None:
            self.toast("引擎未就绪", "err")
            return
        self.toast("正在检查更新…", "info", 2500)

        def done(res, err):
            if err:
                self.toast(f"检查更新失败：{err}", "err", 5000)
                return
            upd = res if isinstance(res, dict) else {}
            if upd.get("available"):
                self._upd_dismissed = False
                self._render_update_bar(upd)
                self.toast(f"发现新版本 {upd.get('latest')}", "ok", 5000)
            elif upd.get("error"):
                self.toast(f"检查更新失败：{upd['error']}", "err", 5000)
            else:
                self.toast(f"已经是最新版本（{APP_VER}）", "ok", 4000)

        self.engine.submit_async(agent.check_update(force=True), done)

    def on_settings(self) -> None:
        SettingsDialog(self)

    # ------------------------------------------------ 托盘

    def _setup_tray(self) -> None:
        if mclink_tray is None:
            return
        try:
            ico = os.path.join(HERE, "assets", "mclink.ico")
            self.tray = mclink_tray.TrayIcon(
                tooltip=f"{APP_NAME} · 端口映射",
                icon_path=ico if os.path.exists(ico) else None,
                on_command=self.cmd_queue.put)
            self.tray.start()
        except Exception as exc:                     # noqa: BLE001
            self.tray = None
            try:
                mc.LOG.warn(f"托盘图标不可用（不影响使用）: {exc}")
            except Exception:
                pass

    def _sync_tray_tip(self, st: dict) -> None:
        if not self.tray:
            return
        sv = st.get("server", {})
        maps = st.get("mappings") or []
        active = sum(1 for m in maps if m.get("status") == "active")
        self.tray.set_tooltip(
            f"{APP_NAME} · {'已连接' if sv.get('connected') else '未连接'}\n"
            f"{active}/{len(maps)} 条映射生效\n{sv.get('public_ip') or ''}".strip())

    def _poll_commands(self) -> None:
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                if callable(cmd):
                    # 从别的线程丢回主线程执行的任务（tkinter 只能主线程碰）
                    try:
                        cmd()
                    except Exception as exc:         # noqa: BLE001
                        try:
                            mc.LOG.error(f"主线程任务出错: {exc!r}")
                        except Exception:
                            pass
                elif cmd == "show":
                    self.show_window()
                elif cmd == "quit":
                    self.quit_app()
                elif cmd == "web":
                    self.open_web()
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(150, self._poll_commands)

    def post(self, fn) -> None:
        """线程安全地把一个函数排到主线程执行。"""
        self.cmd_queue.put(fn)

    # ------------------------------------------------ 授权（给界面用的 API）

    def license_info(self) -> dict:
        agent = self.engine.agent
        if agent is None:
            return {"licensed": False, "is_admin": False, "username": None,
                    "status": None, "reason": "引擎未就绪", "required": False,
                    "admin_configured": False, "admin_key_saved": False,
                    "device_token": None}
        return agent.license_info()

    def admin_unlock(self, key: str):
        """解锁管理员模式。返回 (ok, error)。"""
        agent = self.engine.agent
        if agent is None:
            return False, "引擎未就绪"
        try:
            ok, err = self.engine.call_async(agent.admin_auth(key), timeout=18)
        except Exception as exc:                     # noqa: BLE001
            return False, f"请求失败：{exc}"
        if ok:
            try:
                self.engine.call(self._remember_admin_key, key)
            except Exception:
                pass
        return (True, None) if ok else (False, err)

    def _remember_admin_key(self, key: str) -> None:
        agent = self.engine.agent
        agent.admin_key = key
        agent.cfg.setdefault("server", {})["admin_token"] = key
        agent.save_config()

    def admin_lock(self) -> None:
        agent = self.engine.agent
        if agent is None:
            return
        try:
            self.engine.call_async(agent.admin_lock_async(), timeout=8)
        except Exception:
            pass

    def admin_call(self, action: str, args: dict, callback) -> None:
        """异步发管理员请求；callback(result, error) 在**主线程**被调用。"""
        agent, loop = self.engine.agent, self.engine.loop
        if agent is None or loop is None:
            self.post(lambda: callback(None, "引擎未就绪"))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                agent.admin_request(action, args or {}), loop)
        except Exception as exc:                     # noqa: BLE001
            self.post(lambda: callback(None, str(exc)))
            return

        def _done(f):
            try:
                res, err = f.result(), None
            except Exception as exc:                 # noqa: BLE001
                res, err = None, (str(exc) or "操作失败")
            self.post(lambda: callback(res, err))

        fut.add_done_callback(_done)

    def activate_license(self, code: str, callback) -> None:
        """异步激活；callback(result, error) 在**主线程**被调用。"""
        agent, loop = self.engine.agent, self.engine.loop
        if agent is None or loop is None:
            self.post(lambda: callback(None, "引擎未就绪"))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(agent.activate_license(code), loop)
        except Exception as exc:                     # noqa: BLE001
            self.post(lambda: callback(None, str(exc)))
            return

        def _done(f):
            try:
                res, err = f.result(), None
            except Exception as exc:                 # noqa: BLE001
                res, err = None, (str(exc) or "激活失败")
            self.post(lambda: callback(res, err))

        fut.add_done_callback(_done)

    def on_activate(self) -> None:
        ActivationDialog(self)

    def on_admin(self) -> None:
        if DIST_MODE:                                # 分发版没有管理员密钥，进不去
            return
        try:
            from mclink_admin import AdminWindow
        except Exception as exc:                     # noqa: BLE001
            self.toast(f"管理员模块加载失败：{exc}", "err", 5000)
            return
        try:
            AdminWindow(self)
        except Exception as exc:                     # noqa: BLE001
            self.toast(f"打开管理员控制台失败：{exc}", "err", 5000)

    def open_web(self) -> None:
        port = self.engine.web_port
        if not port:
            self.toast("网页控制台没有启动", "err")
            return
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")

    def open_url(self, url: str) -> None:
        """用系统默认浏览器打开一个网址（项目主页这类链接用）。"""
        if not str(url).startswith(("http://", "https://")):
            return                       # 只放行 http(s)，避免别的协议被当命令执行
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:                     # noqa: BLE001
            self.toast(f"打开链接失败：{exc}", "err", 4000)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    # ------------------------------------------------ 生命周期

    def on_close(self) -> None:
        if self.tray:
            self.root.withdraw()
            self.toast("已最小化到托盘，右键托盘图标可退出", "info", 3000)
            self.tray.notify(f"{APP_NAME} 仍在后台运行",
                             "隧道保持连接中。右键托盘图标可以退出。")
            return
        self.quit_app()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.toast("正在退出…")
            self.root.update_idletasks()
        except Exception:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        if not self.start_hidden:
            self.show_window()
        self.root.after(150, self._poll_commands)
        # 引擎起不来时不要静默卡住
        self.root.after(1200, self._check_engine)
        self.root.mainloop()

    def _check_engine(self) -> None:
        if self._quitting:
            return
        if not self.engine.ready.is_set() and self.engine.is_alive():
            return
        if self.engine.agent is None:
            self.pill_dot.itemconfigure(self._dot, fill=RED)
            self.pill_text.configure(text="引擎启动失败")
            self.toast(self.engine.fatal or "引擎启动失败，请检查 config.client.json",
                       "err", 9000)


# ---------------------------------------------------------------- 映射卡片

class MappingCard:
    def __init__(self, parent, app: McLinkApp, m: dict):
        self.app = app
        self.mid = m.get("id")
        self.outer = RoundCard(parent, radius=16, pad=14)
        p = tk.Frame(self.outer.body, bg=CARD)
        p.pack(fill="x")

        row1 = tk.Frame(p, bg=CARD)
        row1.pack(fill="x")
        self.dot = tk.Canvas(row1, width=10, height=10, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        self.lbl_name = tk.Label(row1, text="", bg=CARD, fg=TEXT, font=app.f_card_title)
        self.lbl_name.pack(side="left")
        self.lbl_proto = tk.Label(row1, text="", bg=LINE_SOFT, fg=BLUE, font=app.f_tiny,
                                  padx=7, pady=2)
        self.lbl_proto.pack(side="left", padx=8)

        app._btn(row1, "删除", lambda: app.on_delete(m), kind="danger").pack(side="right")
        app._btn(row1, "编辑", lambda: app.on_edit(self._current), kind="ghost").pack(
            side="right", padx=(0, 6))
        self.btn_sw = app._btn(row1, "", self._toggle, kind="ghost", width=7)
        self.btn_sw.pack(side="right", padx=(0, 6))

        row2 = tk.Frame(p, bg=CARD)
        row2.pack(fill="x", pady=(7, 0))
        self.lbl_addr = tk.Label(row2, text="", bg=CARD, fg=TEXT2, font=app.f_mono)
        self.lbl_addr.pack(side="left")
        self.btn_copy = app._btn(row2, "复制连接地址", self._copy, kind="ghost")
        self.btn_copy.pack(side="left", padx=(10, 0))
        self.lbl_err = tk.Label(row2, text="", bg=CARD, fg=RED, font=app.f_tiny)
        self.lbl_err.pack(side="left", padx=(10, 0))

        row3 = tk.Frame(p, bg=CARD)
        row3.pack(fill="x", pady=(6, 0))
        self.lbl_stats = tk.Label(row3, text="", bg=CARD, fg=MUTED, font=app.f_small)
        self.lbl_stats.pack(side="left")

        self._current = dict(m)
        self._last_rates = (0.0, 0.0)

    def grid(self, **kw):
        self.outer.grid(**kw)

    def destroy(self):
        self.outer.destroy()

    def _toggle(self):
        # 点开关 = 打开/关闭这条映射。当前"在跑"就关掉，否则打开。
        # 注意判据要和 update_view 里一致（用 running，不看配置里的 enabled），
        # 否则默认不放行时点一下会变成"关掉"而不是"打开"。
        cur = self._current or {}
        running = bool(cur.get("enabled", True)) and cur.get("status") != "inactive"
        self.app.on_toggle(self.mid, not running)

    def _copy(self):
        addr = self._current.get("connect_addr")
        if addr:
            self.app.copy(addr, "连接地址已复制")
        else:
            self.app.toast("服务器还没分配公网地址", "warn")

    def update_view(self, m: dict) -> None:
        self._current = dict(m)
        name = m.get("name") or "未命名"
        proto = (m.get("proto") or "tcp").upper()
        enabled = bool(m.get("enabled", True))
        status = m.get("status")
        err = m.get("error")
        # "开关是开着的"要看**运行状态**，不能只看配置里的 enabled：
        # 启动时默认不放行（client.auto_start_mappings 关着），这时配置里虽然
        # 是 enabled=true，但映射其实没跑。拿 enabled 当开关状态会让用户点一下
        # 反而把它关掉（双重取反），所以统一用 running 判断。
        running = enabled and status != "inactive"

        self.lbl_name.configure(text=name)
        self.lbl_proto.configure(text=proto,
                                 fg=PURPLE if proto == "UDP" else BLUE)

        color = {"active": GREEN, "pending": AMBER,
                 "error": RED}.get(status, MUTED)
        if not running:
            color = "#c3ccd8"
        self.dot.itemconfigure(self._dot, fill=color)

        local = f"{m.get('local_host')}:{m.get('local_port')}"
        addr = m.get("connect_addr") or f"<公网IP>:{m.get('remote_port')}"
        self.lbl_addr.configure(text=f"{addr}   →   {local}")

        self.btn_sw.configure(text="已启用" if running else "已停用",
                              fg=GREEN if running else MUTED)
        self.lbl_err.configure(text=("⚠ " + str(err)[:64]) if err else "")
        # 卡片描边跟着状态走：出错淡红、停用淡灰、正常浅灰
        self.outer.set_border("#f8d7d7" if err else ("#eef2f7" if not running else LINE))

        rx, tx = float(m.get("rx_rate") or 0), float(m.get("tx_rate") or 0)
        total = fmt_bytes((m.get("rx_bytes") or 0) + (m.get("tx_bytes") or 0))
        self.lbl_stats.configure(
            text=f"↓ {fmt_rate(rx)}    ↑ {fmt_rate(tx)}        "
                 f"连接 {m.get('active_conns', 0)} / 累计 {m.get('total_conns', 0)}        "
                 f"流量 {total}")


# ---------------------------------------------------------------- 新增 / 编辑

TEMPLATES = [
    ("Minecraft Java", "tcp", 25565),
    ("Minecraft 基岩版", "udp", 19132),
    ("泰拉瑞亚", "tcp", 7777),
    ("饥荒联机版", "udp", 10999),
    ("幻兽帕鲁", "udp", 8211),
    ("星露谷物语", "udp", 24642),
]


class BaseDialog(tk.Toplevel):
    def __init__(self, app: McLinkApp, title: str, w: int, h: int):
        super().__init__(app.root)
        self.app = app
        self.result = None
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.geometry(f"{w}x{h}")
        self._center(w, h)
        try:
            self.iconphoto(False, app._icon_img)
        except Exception:
            pass

    def _center(self, w: int, h: int) -> None:
        self.update_idletasks()
        r = self.app.root
        x = r.winfo_rootx() + (r.winfo_width() - w) // 2
        y = r.winfo_rooty() + (r.winfo_height() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def grab(self) -> None:
        try:
            self.grab_set()
            self.wait_window(self)
        except Exception:
            pass

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class AddEditDialog(BaseDialog):
    def __init__(self, app: McLinkApp, mapping: dict | None):
        self.editing = mapping
        super().__init__(app, "编辑端口映射" if mapping else "新增端口映射", 560, 620)
        self.vars = {}
        self._build()
        self.grab()

    def _field(self, parent, key, label, value="", width=26, hint=""):
        tk.Label(parent, text=label, bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(10, 3))
        v = tk.StringVar(value=str(value))
        e = tk.Entry(parent, textvariable=v, font=self.app.f_body, width=width,
                     bg="#fbfcfe", fg=TEXT, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=LINE,
                     highlightcolor=SKY, insertbackground=TEXT)
        e.pack(fill="x", ipady=6, ipadx=6)
        if hint:
            tk.Label(parent, text=hint, bg=CARD, fg=MUTED,
                     font=self.app.f_tiny).pack(anchor="w", pady=(3, 0))
        self.vars[key] = v
        return e

    def _build(self) -> None:
        # 底部按钮**先 pack**：Tk 的 packer 按顺序分配空间，先放的内容区如果用
        # expand=True，窗口一被压小，后 pack 的底栏就会被挤成 0 高度 ——
        # 「保存」按钮看起来就"消失"了。所以先占住底栏，再让内容区吃剩余空间。
        foot = tk.Frame(self, bg=BG, height=64)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        tk.Frame(self, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.btn_save = self.app._btn(foot, "保存", self.save, kind="primary")
        self.btn_save.pack(side="right", padx=(0, 22), pady=16)
        self.app._btn(foot, "取消", self.cancel, kind="ghost").pack(
            side="right", padx=(0, 8), pady=16)
        self.app._btn(foot, "自动选一个空闲端口", self.auto_pick,
                      kind="normal").pack(side="left", padx=(22, 0), pady=16)

        pad = tk.Frame(self, bg=CARD)
        pad.pack(fill="both", expand=True, padx=14, pady=(14, 6))
        box = tk.Frame(pad, bg=CARD)
        box.pack(fill="both", expand=True, padx=12, pady=6)

        # 名称
        self._field(box, "name", "名称",
                    (self.editing or {}).get("name", ""), hint="随便起个能认出来的名字")

        # 协议
        tk.Label(box, text="协议", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(12, 3))
        seg = tk.Frame(box, bg=LINE_SOFT)
        seg.pack(anchor="w")
        self.proto = ((self.editing or {}).get("proto") or "tcp").lower()
        self.proto_btns = {}
        for val, txt in (("tcp", "TCP"), ("udp", "UDP")):
            b = tk.Button(seg, text=txt, font=self.app.f_small, bd=0, relief="flat",
                          padx=20, pady=5, cursor="hand2", highlightthickness=0,
                          command=lambda v=val: self.set_proto(v))
            b.pack(side="left", padx=2, pady=2)
            self.proto_btns[val] = b
        self.set_proto(self.proto)

        # 游戏模板
        tk.Label(box, text="游戏模板（点一下自动填好名称和端口）", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(12, 4))
        tpl_row = tk.Frame(box, bg=CARD)
        tpl_row.pack(fill="x")
        for i, (nm, proto, port) in enumerate(TEMPLATES):
            b = self.app._btn(tpl_row, nm, lambda n=nm, p=proto, pt=port: self.apply_tpl(n, p, pt),
                              kind="normal")
            b.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 6), pady=(0, 6))
        for c in range(3):
            tpl_row.columnconfigure(c, weight=1)

        # 本地
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")
        row.columnconfigure(0, weight=3, uniform="c")
        row.columnconfigure(1, weight=2, uniform="c")
        c1 = tk.Frame(row, bg=CARD)
        c1.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._field(c1, "local_host", "本地地址",
                    (self.editing or {}).get("local_host", "127.0.0.1"))
        c2 = tk.Frame(row, bg=CARD)
        c2.grid(row=0, column=1, sticky="ew")
        self._field(c2, "local_port", "本地端口",
                    (self.editing or {}).get("local_port", ""))

        # 公网端口 + 端口池
        self._field(box, "remote_port", "公网端口",
                    (self.editing or {}).get("remote_port", ""),
                    hint="必须落在下面列出的范围内")
        self.lbl_pool = tk.Label(box, text="", bg=CARD, fg=MUTED, font=self.app.f_tiny)
        self.lbl_pool.pack(anchor="w", pady=(4, 0))
        self.chips = tk.Frame(box, bg=CARD)
        self.chips.pack(fill="x", pady=(4, 0))
        self.lbl_err = tk.Label(box, text="", bg=CARD, fg=RED, font=self.app.f_small)
        self.lbl_err.pack(anchor="w", pady=(8, 0))

        self.refresh_pool()

    # ------------------------------------------------ 模板 / 协议

    def apply_tpl(self, name, proto, port) -> None:
        self.vars["name"].set(name)
        self.set_proto(proto)
        self.vars["local_port"].set(str(port))
        # 优先用这个游戏的约定端口（玩家习惯，MC 系还能免输端口）；
        # 被占用或不在白名单里，才退到端口池挑一个。
        used = self._used()
        if self._port_in_pool(port) and port not in used:
            remote = port
        else:
            free = self._free_port(proto)
            remote = free if free is not None else port
        self.vars["remote_port"].set(str(remote))
        self.refresh_pool()

    def _port_in_pool(self, port: int) -> bool:
        return any(lo <= port <= hi for lo, hi in self._ranges())

    def set_proto(self, v: str) -> None:
        self.proto = v
        for val, b in self.proto_btns.items():
            on = (val == v)
            b.configure(bg=SKY if on else LINE_SOFT,
                        fg="white" if on else TEXT2,
                        activebackground=SKY if on else "#e4ebf5",
                        activeforeground="white" if on else TEXT2)
        self.refresh_pool()

    # ------------------------------------------------ 端口池

    def _ranges(self):
        agent = self.app.engine.agent
        return agent.allowed_ranges() if agent else []

    def _used(self):
        st = self.app.engine.state or {}
        used = set()
        for m in st.get("mappings") or []:
            if self.editing and m.get("id") == self.editing.get("id"):
                continue
            if (m.get("proto") or "").lower() == self.proto:
                used.add(int(m.get("remote_port") or 0))
        return used

    def _free_port(self, proto=None):
        """挑一个空闲端口：优先用最宽的端口池，避免把 19132 这种
        "别的游戏的约定端口"分给不相关的游戏。"""
        proto = proto or self.proto
        used = self._used()
        ranges = sorted(self._ranges(), key=lambda r: r[1] - r[0], reverse=True)
        for lo, hi in ranges:
            for p in range(lo, hi + 1):
                if p not in used:
                    return p
        return None

    def auto_pick(self) -> None:
        f = self._free_port()
        if f is None:
            self.app.toast("端口池里没有空闲端口了", "err")
            return
        self.vars["remote_port"].set(str(f))
        self.refresh_pool()
        self.app.toast(f"已选择端口 {f}", "ok")

    def refresh_pool(self) -> None:
        # 构造过程中 set_proto() 会先被调用，那时端口池控件还没建出来
        if not hasattr(self, "chips"):
            return
        for w in self.chips.winfo_children():
            w.destroy()
        ranges = self._ranges()
        used = self._used()
        if not ranges:
            self.lbl_pool.configure(text="等待服务器下发可用端口范围…")
            return
        total = sum(hi - lo + 1 for lo, hi in ranges)
        self.lbl_pool.configure(
            text=f"服务器允许 {fmt_ranges(ranges)}（共 {total} 个，{self.proto.upper()} 已占用 "
                 f"{len([p for p in used])} 个）")

        # 小范围逐个列出，大范围给一个"自动挑"的按钮
        for lo, hi in ranges:
            span = hi - lo + 1
            if span <= 24:
                for p in range(lo, hi + 1):
                    taken = p in used
                    b = tk.Button(self.chips, text=str(p), font=self.app.f_mono,
                                  bd=0, relief="flat", padx=8, pady=3, cursor="hand2",
                                  highlightthickness=0,
                                  bg="#f2f5f9" if taken else "#eef6ff",
                                  fg=MUTED if taken else SKY,
                                  state="disabled" if taken else "normal",
                                  command=lambda pp=p: (self.vars["remote_port"].set(str(pp)),
                                                        self.refresh_pool()))
                    b.pack(side="left", padx=(0, 5), pady=2)
            else:
                b = tk.Button(self.chips, text=f"{lo} - {hi}（点此自动挑空闲的）",
                              font=self.app.f_tiny, bd=0, relief="flat", padx=10, pady=3,
                              cursor="hand2", highlightthickness=0,
                              bg="#eef6ff", fg=SKY, command=self.auto_pick)
                b.pack(side="left", padx=(0, 5), pady=2)

    # ------------------------------------------------ 保存

    def save(self) -> None:
        agent = self.app.engine.agent
        if agent is None:
            self.app.toast("引擎还没就绪", "err")
            return
        spec = {
            "name": self.vars["name"].get().strip(),
            "proto": self.proto,
            "local_host": self.vars["local_host"].get().strip() or "127.0.0.1",
            "local_port": self.vars["local_port"].get().strip(),
            "remote_port": self.vars["remote_port"].get().strip(),
            "enabled": True if not self.editing else bool(self.editing.get("enabled", True)),
        }
        if self.editing and self.editing.get("id"):
            spec["id"] = self.editing["id"]

        err = agent.validate(spec, spec.get("id"))
        if err:
            self.lbl_err.configure(text="⚠ " + err)
            return
        res = self.app._submit(agent.upsert, spec)
        if res is None:
            self.lbl_err.configure(text="⚠ 保存失败，请查看日志")
            return
        self.app.toast("映射已保存", "ok")
        self.result = res
        self.destroy()


class ActivationDialog(BaseDialog):
    """输入一次性密钥激活本机。授权校验没开启时用不到这个窗口。"""

    def __init__(self, app: McLinkApp):
        super().__init__(app, "激活 McLink", 500, 430)
        self._build()
        self.grab()

    def _build(self) -> None:
        pad = tk.Frame(self, bg=CARD)
        pad.pack(fill="both", expand=True, padx=14, pady=14)
        box = tk.Frame(pad, bg=CARD)
        box.pack(fill="both", expand=True, padx=12, pady=6)

        tk.Label(box, text="输入一次性密钥", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(box, text="向管理员要一串形如  MCLK-XXXX-XXXX-XXXX  的密钥。\n"
                           "24 小时内有效、只能用一次；激活成功后这台电脑就可以长期使用了。",
                 bg=CARD, fg=MUTED, font=self.app.f_tiny,
                 justify="left").pack(anchor="w", pady=(5, 14))

        self.code = tk.StringVar()
        e = tk.Entry(box, textvariable=self.code, font=(MONO, 14), bg="#fbfcfe",
                     fg=TEXT, relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=SKY,
                     insertbackground=TEXT, justify="center")
        e.pack(fill="x", ipady=9)
        e.focus_set()
        e.bind("<Return>", lambda _e: self.submit())

        # ---- 昵称：管理员在控制台里靠这个认出"这是谁" ----
        agent = self.app.engine.agent
        cur_nick = agent.nickname() if agent else ""
        tk.Label(box, text="你的昵称（管理员在控制台里看得到）", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(14, 3))
        self.nick = tk.StringVar(value=cur_nick)
        ne = tk.Entry(box, textvariable=self.nick, font=self.app.f_body, bg="#fbfcfe",
                      fg=TEXT, relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=LINE, highlightcolor=SKY,
                      insertbackground=TEXT)
        ne.pack(fill="x", ipady=6, ipadx=6)
        tk.Label(box, text="填你自己认得出来的名字（比如「小明」）。以后想改去「设置」里改。",
                 bg=CARD, fg=MUTED, font=self.app.f_tiny).pack(anchor="w", pady=(2, 0))

        self.lbl_err = tk.Label(box, text="", bg=CARD, fg=RED, font=self.app.f_small,
                                justify="left", wraplength=420)
        self.lbl_err.pack(anchor="w", pady=(10, 0))

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=26, pady=(0, 16))
        self.btn_ok = self.app._btn(foot, "激活", self.submit, kind="primary")
        self.btn_ok.pack(side="right")
        self.app._btn(foot, "稍后再说", self.cancel,
                      kind="ghost").pack(side="right", padx=(0, 8))
        self.app._btn(foot, "复制本机机器码", self.copy_fp,
                      kind="normal").pack(side="left")

    def copy_fp(self) -> None:
        agent = self.app.engine.agent
        fp = (agent.fingerprint[:24] if agent else "")
        self.app.copy(fp, "机器码已复制（发给管理员方便排查）")

    def submit(self) -> None:
        code = self.code.get().strip()
        if not code:
            self.lbl_err.configure(text="⚠ 请先填写密钥")
            return
        # 昵称先存下来：激活成功后管理员立刻就能看到"这张码是谁在用"
        agent = self.app.engine.agent
        nick = self.nick.get().strip()[:32]
        if agent is not None and nick != agent.nickname():
            try:
                self.app._submit(agent.set_nickname, nick)
            except Exception:                        # noqa: BLE001
                pass
        self.btn_ok.configure(text="激活中…", state="disabled")
        self.lbl_err.configure(text="")
        self.app.activate_license(code, self._done)

    def _done(self, result, error) -> None:
        try:
            alive = bool(self.winfo_exists())
        except Exception:
            alive = False
        if not alive:
            return
        if error:
            self.btn_ok.configure(text="重新激活", state="normal")
            self.lbl_err.configure(text="⚠ " + str(error))
            return
        who = (result or {}).get("username") or "用户"
        # 昵称在上报之前先告诉服务端，管理员那边才看得到
        try:
            agent = self.app.engine.agent
            if agent is not None:
                self.app.engine.submit_async(agent.push_nickname())
        except Exception:                            # noqa: BLE001
            pass
        self.app.toast(f"激活成功，欢迎 {who}！", "ok", 4500)
        self.result = result
        self.destroy()


class SettingsDialog(BaseDialog):
    #: 默认高度。内容装得下就用它，装不下（有滚动区兜底）也不至于太高。
    BASE_H = 620

    def __init__(self, app: McLinkApp):
        # 关键：**在窗口建出来之前**就把高度算好。
        # 之前试过先建 620 再 geometry() 撑高 —— Tk 对已经映射的窗口不生效
        # （实测连 minsize 都改了、winfo_height() 还是 620），结果「关于/鸣谢」
        # 一直在可视区外面，用户根本找不到。所以这里先建个隐藏的窗口量一次。
        h = self._measure_height(app)
        super().__init__(app, "设置", 520, h)
        self._build()
        self._sync_scrollbar()
        self.grab()

    @classmethod
    def _measure_height(cls, app: McLinkApp) -> int:
        """先造一个隐藏的设置窗口量出真实需要的高度，再决定最终高度。"""
        try:
            probe = cls.__new__(cls)
            tk.Toplevel.__init__(probe, app.root)
            probe.app = app
            probe.withdraw()                     # 不显示，只用来量
            probe.configure(bg=BG)
            probe.geometry(f"520x{cls.BASE_H}")
            probe._build()
            probe.update_idletasks()
            body = getattr(probe, "_sc_canvas", None)
            need = 0
            if body is not None:
                need = body.bbox("all")[3] if body.bbox("all") else 0
            # 内容高 + 底栏 + 外边距
            want = int(need) + 64 + 26
            probe.destroy()
            avail = app.root.winfo_screenheight() - 120
            return max(cls.BASE_H, min(want, avail))
        except Exception:                            # noqa: BLE001
            return cls.BASE_H

    def _fit_to_content(self, base_h: int) -> None:
        """按内容实际需要的高度调整窗口（不超过屏幕可用高度）。

        设置项越加越多，写死高度就会把底部（比如「关于/鸣谢」）切掉 ——
        这里量一次 reqheight，装不下就长高，装得下就保持原样。
        """
        try:
            self.update_idletasks()
            need = self.winfo_reqheight()
            avail = self.winfo_screenheight() - 120
            h = max(base_h, min(need, avail))
            if h <= base_h:
                return
            w = self.winfo_width() or 520
            x = self.winfo_x()
            y = max(0, min(self.winfo_y(), max(0, self.winfo_screenheight() - h - 8)))
            # 关键：窗口可能还没真正映射（width/height 还是占位值），
            # 这时 geometry() 会被随后的"首次映射"覆盖掉。用 minsize + geometry
            # 双保险，并排到事件队列末尾再设一次。
            self.minsize(w, h)
            self.geometry(f"{w}x{h}+{max(0, x)}+{y}")
            self.after(0, lambda: self._force_size(w, h, x, y))
        except Exception:                            # noqa: BLE001
            pass

    def _sc_wheel(self, ev) -> None:
        """设置窗口内容区的滚轮滚动。"""
        try:
            self._sc_canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")
        except Exception:                            # noqa: BLE001
            pass

    def _sync_scrollbar(self) -> None:
        """内容变了就刷新一下滚动区域（滚动条本身常驻）。"""
        try:
            self._sc_canvas.configure(scrollregion=self._sc_canvas.bbox("all"))
        except Exception:                            # noqa: BLE001
            pass

    def _force_size(self, w: int, h: int, x: int, y: int) -> None:
        try:
            self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        except Exception:                            # noqa: BLE001
            pass
        self._sync_scrollbar()

    def _build(self) -> None:
        agent = self.app.engine.agent
        cfg = dict(agent.cfg) if agent else {}
        srv = dict(cfg.get("server") or {})
        web = dict(cfg.get("web") or {})
        cli = dict(cfg.get("client") or {})

        # 底栏先 pack（和「新增映射」窗口同一个道理：先占住底部，
        # 否则窗口被压小时「保存并重连」会被内容区挤掉）
        foot = tk.Frame(self, bg=BG, height=64)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        tk.Frame(self, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.btn_save = self.app._btn(foot, "保存并重连", self.save, kind="primary")
        self.btn_save.pack(side="right", padx=(0, 22), pady=16)
        self.app._btn(foot, "取消", self.cancel, kind="ghost").pack(
            side="right", padx=(0, 8), pady=16)
        self.app._btn(foot, "打开网页控制台", self.app.open_web,
                      kind="normal").pack(side="left", padx=(22, 0), pady=16)

        # 中间内容区做成**可滚动**：设置项只会越来越多，写死窗口高度迟早
        # 会把底部（比如「关于/鸣谢」）切掉；而 Tk 在窗口映射之后再调
        # geometry() 改高度是不生效的（实测连 minsize 都改了、height 还是老的）。
        # 所以：外层 canvas + 右侧滚动条，内容装不下就能滚。
        outer = tk.Frame(self, bg=CARD)
        outer.pack(fill="both", expand=True, padx=14, pady=(14, 6))
        self._sc_canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0, bd=0)
        self._sc_bar = tk.Scrollbar(outer, orient="vertical",
                                    command=self._sc_canvas.yview, width=12,
                                    troughcolor=CARD, bg="#cdd6e3", relief="flat",
                                    borderwidth=0, activebackground="#b6c2d2")
        self._sc_canvas.configure(yscrollcommand=self._sc_bar.set)
        self._sc_canvas.pack(side="left", fill="both", expand=True)
        # 滚动条常驻：设置窗口内容一定会超过可视区（现在就已经超了），
        # 常驻还能给用户"这里可以滚"的提示；做成按需显示反而容易漏（首次
        # 布局时 canvas 高度还是 1，判断必然失败）。
        self._sc_bar.pack(side="right", fill="y")
        pad = tk.Frame(self._sc_canvas, bg=CARD)
        self._sc_win = self._sc_canvas.create_window((0, 0), window=pad, anchor="nw")
        pad.bind("<Configure>", lambda _e: self._sc_canvas.configure(
            scrollregion=self._sc_canvas.bbox("all")))
        self._sc_canvas.bind("<Configure>", lambda e: (
            self._sc_canvas.itemconfigure(self._sc_win, width=e.width),
            self._sc_canvas.configure(scrollregion=self._sc_canvas.bbox("all"))))
        # 滚轮：canvas 和内容区都要绑，否则鼠标停在输入框上就滚不动。
        # 子控件的 <MouseWheel> 会冒泡到父级，所以绑这两层够用。
        for _w in (self._sc_canvas, pad):
            _w.bind("<MouseWheel>", self._sc_wheel)

        box = tk.Frame(pad, bg=CARD)
        box.pack(fill="both", expand=True, padx=12, pady=6)

        def field(label, value, show=None):
            tk.Label(box, text=label, bg=CARD, fg=TEXT2,
                     font=self.app.f_small).pack(anchor="w", pady=(10, 3))
            v = tk.StringVar(value=str(value))
            e = tk.Entry(box, textvariable=v, font=self.app.f_body, bg="#fbfcfe", fg=TEXT,
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=SKY,
                         insertbackground=TEXT, show=show)
            e.pack(fill="x", ipady=6, ipadx=6)
            return v

        # ---- 昵称：管理员那边看得到这个 ----
        self.v_nick = field("我的昵称（管理员在控制台里看得到）",
                            cli.get("nickname", ""))
        tk.Label(box, text="留空就用这台电脑的机器名。改了立刻生效，不用重连。",
                 bg=CARD, fg=MUTED, font=self.app.f_tiny).pack(anchor="w", pady=(2, 0))

        self.v_host = field("服务器地址（公网 IP 或域名）", srv.get("host", ""))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="c")
        row.columnconfigure(1, weight=1, uniform="c")
        c1 = tk.Frame(row, bg=CARD)
        c1.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tk.Label(c1, text="服务端端口", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(10, 3))
        self.v_port = tk.StringVar(value=str(srv.get("control_port", 7000)))
        tk.Entry(c1, textvariable=self.v_port, font=self.app.f_body, bg="#fbfcfe", fg=TEXT,
                 relief="flat", bd=0, highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=SKY).pack(fill="x", ipady=6, ipadx=6)
        c2 = tk.Frame(row, bg=CARD)
        c2.grid(row=0, column=1, sticky="ew")
        tk.Label(c2, text="网页控制台端口", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w", pady=(10, 3))
        self.v_web = tk.StringVar(value=str(web.get("port", 8787)))
        tk.Entry(c2, textvariable=self.v_web, font=self.app.f_body, bg="#fbfcfe", fg=TEXT,
                 relief="flat", bd=0, highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=SKY).pack(fill="x", ipady=6, ipadx=6)

        self.v_token = field("密钥（必须与服务端一致）", srv.get("token", ""), show="•")

        self.v_auto = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(box, text="开机自动启动（后台静默运行）", variable=self.v_auto,
                       bg=CARD, fg=TEXT2, font=self.app.f_small, activebackground=CARD,
                       selectcolor="#eef6ff", anchor="w").pack(anchor="w", pady=(14, 0))

        # ---- 自动更新 ----
        self.v_upd = tk.BooleanVar(value=bool(cli.get("auto_update", True)))
        tk.Checkbutton(box, text="自动检查新版本（发现后提示，点了才装）",
                       variable=self.v_upd, bg=CARD, fg=TEXT2, font=self.app.f_small,
                       activebackground=CARD, selectcolor="#eef6ff",
                       anchor="w").pack(anchor="w", pady=(6, 0))

        upd_row = tk.Frame(box, bg=CARD)
        upd_row.pack(fill="x", pady=(8, 0))
        self.lbl_ver = tk.Label(
            upd_row,
            text=f"当前版本 {APP_VER}",
            bg=CARD, fg=MUTED, font=self.app.f_tiny)
        self.lbl_ver.pack(side="left")
        self.app._btn(upd_row, "检查更新", self.app.on_check_update,
                      kind="normal").pack(side="right")

        info = (f"服务端公网 IP：{agent.public_ip if agent else '未知'}    "
                f"可用端口：{fmt_ranges(agent.allowed_ranges()) if agent else '—'}")
        tk.Label(box, text=info, bg=CARD, fg=MUTED, font=self.app.f_tiny,
                 wraplength=420, justify="left").pack(anchor="w", pady=(12, 0))

        # ---- 关于 / 鸣谢 ----
        tk.Frame(box, bg=LINE, height=1).pack(fill="x", pady=(12, 0))
        about = tk.Frame(box, bg=CARD)
        about.pack(fill="x", pady=(8, 0))
        tk.Label(about, text=f"McLink  v{APP_VER}", bg=CARD, fg=TEXT2,
                 font=self.app.f_small).pack(anchor="w")
        tk.Label(about, text="免费开源（MIT），不做任何收费功能。",
                 bg=CARD, fg=MUTED, font=self.app.f_tiny).pack(anchor="w", pady=(2, 0))
        # 项目主页：做成可点的（点了用系统浏览器打开）
        link = tk.Label(about, text="项目主页：github.com/shiyushui/mclink",
                        bg=CARD, fg=SKY, font=self.app.f_tiny, cursor="hand2")
        link.pack(anchor="w", pady=(4, 0))
        link.bind("<Button-1>", lambda _e: self.app.open_url(
            "https://github.com/shiyushui/mclink"))
        # 赞助鸣谢：单独一行，颜色稍亮一点，方便被看见
        tk.Label(about, text="赞助鸣谢：Light Rice Barrel", bg=CARD, fg=SKY,
                 font=self.app.f_small).pack(anchor="w", pady=(6, 0))

        self.lbl_err = tk.Label(box, text="", bg=CARD, fg=RED, font=self.app.f_small)
        self.lbl_err.pack(anchor="w", pady=(6, 0))

    def save(self) -> None:
        agent = self.app.engine.agent
        if agent is None:
            return
        host = self.v_host.get().strip()
        if not host:
            self.lbl_err.configure(text="⚠ 服务器地址不能为空")
            return
        try:
            port = int(self.v_port.get().strip())
            webport = int(self.v_web.get().strip())
        except ValueError:
            self.lbl_err.configure(text="⚠ 端口必须是数字")
            return

        def mutate():
            agent.cfg["server"]["host"] = host
            agent.cfg["server"]["control_port"] = port
            agent.cfg["server"]["data_port"] = port
            agent.cfg["server"]["udp_port"] = port
            agent.cfg["server"]["token"] = self.v_token.get().strip()
            agent.cfg["web"]["port"] = webport
            cli = agent.cfg.setdefault("client", dict(mc.DEFAULT_CLIENT_SECTION))
            cli["nickname"] = self.v_nick.get().strip()[:32]
            cli["auto_update"] = bool(self.v_upd.get())
            agent.save_config()
            return True

        if self.app._submit(mutate) is None:
            return
        # 昵称立刻上报一次，管理员不用等下次重连就能看到
        try:
            self.app.engine.submit_async(agent.push_nickname())
        except Exception:                            # noqa: BLE001
            pass
        try:
            if self.v_auto.get():
                enable_autostart()
            else:
                disable_autostart()
        except Exception as exc:                     # noqa: BLE001
            self.app.toast(f"设置开机自启失败：{exc}", "warn", 4000)
        self.app.toast("设置已保存，正在重新连接…", "ok")
        self.destroy()


class ConfirmDialog(tk.Toplevel):
    """二次确认对话框（删除这类不可撤销的操作）。"""

    def __init__(self, parent, title: str, message: str, danger: bool = False):
        super().__init__(parent)
        self.result = False
        self.title(title)
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(parent)
        # 高度必须按内容量，不能写死：删除用户那条确认文案正文就要 ~150px，
        # 加按钮行一共 ~247px，而原来固定 195px —— 按钮行被顶到窗口外面，
        # 表现就是"确认框里看不到确定按钮"。先建控件、量 reqheight、再定尺寸。
        w = 430

        tk.Label(self, text=message, bg=CARD, fg=TEXT2, font=(FONT, 10),
                 justify="left", wraplength=w - 60).pack(padx=26, pady=(24, 0),
                                                         anchor="w")
        foot = tk.Frame(self, bg=CARD)
        foot.pack(fill="x", padx=26, pady=(16, 18), side="bottom")
        tk.Button(foot, text="确定删除" if danger else "确定", command=self._ok,
                  font=(FONT, 10), bg=RED if danger else SKY, fg="white",
                  bd=0, relief="flat", padx=18, pady=6, cursor="hand2",
                  activebackground="#d63c3c" if danger else "#1ea3ec").pack(side="right")
        tk.Button(foot, text="取消", command=self._no, font=(FONT, 10), bg=LINE_SOFT,
                  fg=TEXT2, bd=0, relief="flat", padx=18, pady=6, cursor="hand2",
                  activebackground="#e4ebf5").pack(side="right", padx=(0, 8))

        self.update_idletasks()
        need = max(150, self.winfo_reqheight())
        try:
            avail = self.winfo_screenheight() - 120
        except Exception:                            # noqa: BLE001
            avail = need
        h = min(need, avail)
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        except Exception:                            # noqa: BLE001
            x = y = 80
        # 别把窗口放到屏幕外面去
        x = max(0, min(x, max(0, self.winfo_screenwidth() - w - 8)))
        y = max(0, min(y, max(0, self.winfo_screenheight() - h - 8)))
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.protocol("WM_DELETE_WINDOW", self._no)
        try:
            self.grab_set()
            self.wait_window(self)
        except Exception:
            pass

    def _ok(self):
        self.result = True
        self.destroy()

    def _no(self):
        self.result = False
        self.destroy()


class UpdateNotesDialog(tk.Toplevel):
    """看完整的新版本说明。

    更新说明可能很长（多行、带换行），顶栏只放得下一行 —— 原来直接
    `notes[:60]` 截断，后面的内容用户根本看不到。这里用可滚动的文本框
    把全文显示出来：内容短就窗口小、内容长就能滚，鼠标滚轮和拖动滚动条都行。
    """

    MAX_BODY_H = 420          # 正文最大高度，超过就滚动

    def __init__(self, parent, title: str, head: str, body: str):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=CARD)
        self.resizable(False, True)
        self.transient(parent)
        w = 560
        body = body or "（这个版本没有写更新说明）"

        tk.Label(self, text=head, bg=CARD, fg=TEXT, font=(FONT, 11, "bold"),
                 justify="left", anchor="w",
                 wraplength=w - 52).pack(fill="x", padx=26, pady=(20, 0))

        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=26, pady=(10, 0))
        txt = tk.Text(wrap, height=1, bg="#fbfcfe", fg=TEXT2, bd=0,
                      highlightthickness=1, highlightbackground=LINE,
                      font=(FONT, 10), wrap="word", padx=10, pady=8)
        sb = tk.Scrollbar(wrap, command=txt.yview, width=12,
                          troughcolor="#fbfcfe", bg="#cdd6e3", relief="flat",
                          borderwidth=0, activebackground="#b6c2d2")
        txt.configure(yscrollcommand=sb.set)

        # 先量出正文真实需要多高，再决定要不要滚动条
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        self.update_idletasks()
        try:
            need_lines = int(txt.count("1.0", "end-1c", "displaylines")[0])
        except Exception:                            # noqa: BLE001
            need_lines = max(1, body.count("\n") + 1)
        line_h = tkfont.Font(font=(FONT, 10)).metrics("linespace")
        content_h = need_lines * line_h + 20
        if content_h > self.MAX_BODY_H:
            txt.configure(height=max(4, self.MAX_BODY_H // max(1, line_h)))
            sb.pack(side="right", fill="y")
        else:
            # 内容不多：高度贴合内容，不显示滚动条
            txt.configure(height=max(1, need_lines))
        txt.pack(side="left", fill="both", expand=True)

        foot = tk.Frame(self, bg=CARD)
        foot.pack(fill="x", padx=26, pady=(16, 18), side="bottom")
        self.btn_notes_ok = tk.Button(foot, text="知道了", command=self.destroy,
                                      font=(FONT, 10), bg=SKY, fg="white", bd=0,
                                      relief="flat", padx=20, pady=6, cursor="hand2",
                                      activebackground="#1ea3ec")
        self.btn_notes_ok.pack(side="right")

        # 滚轮（Windows 是 <MouseWheel>，顺便兼容 X11/Linux 客户端的 Button-4/5）
        def _wheel(ev):
            try:
                txt.yview_scroll(int(-1 * (ev.delta / 120)), "units")
            except Exception:                        # noqa: BLE001
                pass
        txt.bind("<MouseWheel>", _wheel)

        self.update_idletasks()
        need = max(170, self.winfo_reqheight())
        try:
            avail = self.winfo_screenheight() - 120
        except Exception:                            # noqa: BLE001
            avail = need
        h = min(need, avail)
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 4
        except Exception:                            # noqa: BLE001
            x = y = 80
        x = max(0, min(x, max(0, self.winfo_screenwidth() - w - 8)))
        y = max(0, min(y, max(0, self.winfo_screenheight() - h - 8)))
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        try:
            self.grab_set()
            self.btn_notes_ok.focus_set()
        except Exception:                            # noqa: BLE001
            pass


# ---------------------------------------------------------------- 开机自启（注册表 HKCU\Run）

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "McLink"


def _launcher_command() -> str:
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    return f'"{exe}" "{os.path.join(HERE, "mclink_gui.py")}" --hidden'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except Exception:
        return False


def enable_autostart() -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _launcher_command())


def disable_autostart() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_NAME)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 入口

# 配置文件缺失时自动生成的默认配置（首次运行向导用）
DEFAULT_CLIENT_CFG = {
    "server": {
        "host": "1.2.3.4",
        "control_port": 7000,
        "data_port": 7000,
        "udp_port": 7000,
        "token": "",
        "admin_token": "",
        "tls": True,
        "cert_fingerprint": "",
    },
    "web": {"host": "127.0.0.1", "port": 8787, "token": ""},
    # 昵称由用户自己填，管理员在控制台「客户端」页看得到；自动更新只提示不强制
    "client": dict(mc.DEFAULT_CLIENT_SECTION),
    "mappings": [
        {"id": "mc-java", "name": "Minecraft Java 版", "proto": "tcp",
         "local_host": "127.0.0.1", "local_port": 25565, "remote_port": 25565,
         "enabled": True},
    ],
    "log_level": "info",
    "log_file": "logs/client.log",
}


def ensure_config(path: str) -> bool:
    """配置文件不存在就生成一份默认的。返回是否是首次创建。"""
    if os.path.exists(path):
        return False
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CLIENT_CFG, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return True
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="McLink 端口映射桌面版")
    ap.add_argument("-c", "--config",
                    default=os.path.join(HERE, "config.client.json"))
    ap.add_argument("--hidden", action="store_true", help="启动后直接最小化（供开机自启）")
    ap.add_argument("--no-web", action="store_true", help="不启动网页控制台")
    ap.add_argument("--debug", action="store_true", help="保留控制台输出")
    args = ap.parse_args()

    # 配置文件不存在（比如刚解压出来）就自动生成一份，并标记为首次运行
    first_run = ensure_config(args.config)

    # 关键（顺序不能动）：先给进程定一个 AppUserModelID，再设 DPI 感知、再建窗口。
    # 不设的话，任务栏会把窗口归到 python.exe 名下，按钮上的图标和名字都变成 Python。
    if mclink_winicon is not None:
        mclink_winicon.set_app_user_model_id(AUMID)
    else:                                          # 兜底：模块缺失时老办法
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
        except Exception:
            pass

    # 高分屏下让文字清晰
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # ---- 单实例：第二次双击只把已有窗口叫出来，不再开新进程 ----
    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    si = SingleInstance(AUMID, os.path.join(cfg_dir, "instance.port"))
    if not si.acquire():
        if si.signal_existing():
            if args.debug:
                print("McLink 已经在运行了，已把它的窗口叫到前面。", flush=True)
            return
        # 互斥体还在但 IPC 连不上 —— 多半是上次没退干净，那就照常启动
        if args.debug:
            print("检测到旧的实例残留，继续启动新实例。", flush=True)

    # 桌面版"重启自己"时要拉起的是 GUI，不是后台内核。
    # 不设这个的话，控制台里点重启会起一个没有窗口的进程（之前踩过的坑）。
    mc.RESTART_ARGV = ([sys.executable, os.path.abspath(__file__), "-c", args.config]
                       + (["--hidden"] if args.hidden else []))

    holder: dict = {}

    def _on_started(_engine) -> None:
        if si.owns:
            asyncio.ensure_future(si.serve(lambda: holder["app"].post(
                holder["app"].show_window)))

    app = McLinkApp(args.config, enable_web=not args.no_web,
                    start_hidden=args.hidden,
                    on_started=_on_started if si.owns else None)
    holder["app"] = app

    if first_run:
        # 首次运行：直接把设置窗口弹出来，别让用户自己去猜要填什么
        app.root.after(700, app.on_settings)
        app.root.after(1000, lambda: app.toast(
            "首次运行：请填写服务器地址和密钥，然后点「保存并重连」", "warn", 9000))
    if args.debug:
        print(f"{APP_NAME} {APP_VER} - 引擎线程已启动，配置文件 {args.config}", flush=True)
    try:
        app.run()
    finally:
        si.cleanup()


if __name__ == "__main__":
    main()

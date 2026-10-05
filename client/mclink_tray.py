#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 系统托盘图标  (mclink_tray.py)
=====================================
用纯 ctypes 调 Windows Shell API 实现托盘图标，不依赖 pystray / Pillow。

托盘窗口跑在自己的线程里（带独立的消息循环），所有用户操作都通过
`on_command` 回调丢回主线程的队列，由主窗口用 `after()` 轮询处理 ——
这是 tkinter 下唯一安全的跨线程通信方式。

用不了也不会影响主程序：主窗口调用处已经包了 try/except。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- 消息 ----
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1

# ---- Shell_NotifyIcon ----
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01

# ---- 图标加载 ----
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

# ---- 菜单 ----
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

CMD_SHOW, CMD_WEB, CMD_QUIT = 1, 2, 3


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# 关键：64 位下句柄是 64 位，必须显式声明返回类型，否则会被截断成 int32 而崩溃
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = ctypes.c_void_p
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.LoadIconW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class TrayIcon:
    """一个托盘图标 + 右键菜单。start() 之后在后台线程运行。"""

    def __init__(self, tooltip: str = "McLink", icon_path: str | None = None,
                 on_command=None, on_show=None):
        self.tooltip = (tooltip or "McLink")[:127]
        self.icon_path = icon_path
        self.on_command = on_command or (lambda cmd: None)
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._wndproc = None            # 必须持有引用，否则回调被 GC 掉会崩
        self._class_name = f"McLinkTray_{id(self):x}"
        self._ready = threading.Event()
        self._error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------ 对外接口

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mclink-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(6):
            raise RuntimeError("托盘图标初始化超时")
        if self._error:
            raise RuntimeError(self._error)

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def set_tooltip(self, text: str) -> None:
        text = (text or "").replace("\r", "")[:127]
        if text == self.tooltip:
            return
        self.tooltip = text
        with self._lock:
            if self._nid is None:
                return
            self._nid.szTip = text
            self._nid.uFlags = NIF_TIP
            try:
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
            except Exception:
                pass

    def notify(self, title: str, text: str) -> None:
        with self._lock:
            if self._nid is None:
                return
            self._nid.uFlags = NIF_INFO
            self._nid.szInfoTitle = (title or "")[:63]
            self._nid.szInfo = (text or "")[:255]
            self._nid.dwInfoFlags = NIIF_INFO
            try:
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
            except Exception:
                pass

    # ------------------------------------------------ 内部

    def _load_icon(self):
        if self.icon_path:
            h = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _menu(self):
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, CMD_SHOW, "显示主窗口")
        user32.AppendMenuW(hmenu, MF_STRING, CMD_WEB, "打开网页控制台")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, CMD_QUIT, "退出 McLink")
        return hmenu

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            ev = lparam & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_LBUTTONUP):
                hmenu = self._menu()
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetForegroundWindow(hwnd)
                cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                            pt.x, pt.y, 0, hwnd, None)
                user32.PostMessageW(hwnd, 0, 0, 0)      # 让菜单正确消失
                user32.DestroyMenu(hmenu)
                if cmd:
                    self.on_command({
                        CMD_SHOW: "show", CMD_WEB: "web", CMD_QUIT: "quit",
                    }.get(cmd, "show"))
                return 0
            if ev == WM_LBUTTONDBLCLK:
                self.on_command("show")
                return 0
        elif msg == WM_CLOSE:
            self._remove()
            user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _remove(self) -> None:
        with self._lock:
            if self._nid is not None:
                try:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                except Exception:
                    pass
                self._nid = None

    def _run(self) -> None:
        try:
            self._wndproc = WNDPROC(self._wnd_proc)
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err not in (0, 1410):            # 1410 = 类已存在
                    raise OSError(f"RegisterClassW 失败 (err={err})")

            # 建一个不可见的顶层窗口来接收托盘消息
            hwnd = user32.CreateWindowExW(0, self._class_name, "McLink", 0,
                                          0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW 失败 (err={ctypes.get_last_error()})")
            self._hwnd = hwnd
            self._hicon = self._load_icon()

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip
            with self._lock:
                self._nid = nid
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError(f"Shell_NotifyIcon(NIM_ADD) 失败 (err={ctypes.get_last_error()})")

            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:                    # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()
            self._remove()

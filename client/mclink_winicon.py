#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink Windows 窗口/任务栏图标  (mclink_winicon.py)
==================================================
为什么需要这个模块
------------------
McLink 桌面版是被 `McLink.exe` 拉起来的 `pythonw.exe` 进程。Windows 任务栏
按钮显示图标时的查找顺序是：

    1. 窗口自己的图标（WM_SETICON / WM_GETICON）
    2. 窗口类图标（SetClassLongPtrW 的 GCLP_HICON / GCLP_HICONSM）
    3. 进程主模块（也就是 **pythonw.exe**）的图标
    4. 已注册的 AppUserModelID 对应快捷方式的图标

第 3 步就是"任务栏上显示成 Python 图标"的根源 —— 只要前两步是空的，
任务栏就一定画成 Python。

而 tkinter 的 `wm iconbitmap()` / `wm iconphoto()` 在 Tcl/Tk 8.6 的 Windows
实现里**只是往窗口类里放一个图标**，实测（本机 Tk 8.6 / Python 3.14）：

    root.iconbitmap(default="assets/mclink.ico")
    root.iconphoto(True, img)
    -> WM_GETICON(BIG/SMALL) 仍然是空，窗口类图标也可能读不到

也就是说光靠 tkinter 保证不了任务栏图标。这里用 ctypes 直接调 Win32：

    WM_SETICON            -> 设「窗口自己的图标」，任务栏按钮优先看这个
    SetClassLongPtrW      -> 同时补上「窗口类图标」，作为各种角落的兜底

两个都设上，任务栏、Alt-Tab、任务视图就都会用 McLink 自己的图标。

顺序很讲究：一定要在窗口 **已经创建出来**（root.update() 之后拿得到 HWND）
之后再设。窗口还没映射就设，Windows 会在映射时用类图标覆盖掉。

本模块只依赖标准库，非 Windows 平台上所有函数都是空操作。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

__all__ = ["available", "load_icon", "apply_window_icon", "icon_path",
           "SMALL_SIZE", "BIG_SIZE"]

IS_WINDOWS = os.name == "nt"

# 任务栏小图标 / Alt-Tab 大图标的尺寸
SMALL_SIZE = 16
BIG_SIZE = 32

# ---- Win32 常量 ----
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
GCLP_HICON = -14
GCLP_HICONSM = -34

if IS_WINDOWS:                                    # pragma: no cover - 平台相关
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                   ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
    _user32.SendMessageW.restype = ctypes.c_void_p
    _user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetClassLongPtrW.restype = ctypes.c_void_p
    _user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetClassLongPtrW.restype = ctypes.c_void_p
    _user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                         ctypes.c_void_p]
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
else:                                             # pragma: no cover
    _user32 = None

# 已加载的图标句柄。必须一直持有：FreeLibrary/GC 之后句柄失效，
# 任务栏会退回 pythonw.exe 的图标。
_cache: dict[tuple[str, int, int], int] = {}


def available() -> bool:
    """当前平台能不能用 Win32 设图标。"""
    return IS_WINDOWS and _user32 is not None


def icon_path(base_dir: str | None = None, name: str = "mclink.ico") -> str | None:
    """找 assets\\mclink.ico，找不到返回 None。"""
    here = base_dir or os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "assets", name), os.path.join(here, name)):
        if os.path.exists(cand):
            return cand
    return None


def load_icon(path: str, cx: int = 0, cy: int = 0):
    """从 .ico 载入一个图标句柄，失败返回 None。带缓存，别重复 LoadImage。"""
    if not available() or not path:
        return None
    key = (path, cx, cy)
    if key in _cache:
        return _cache[key]
    flags = LR_LOADFROMFILE | (LR_DEFAULTSIZE if not cx and not cy else 0)
    try:
        handle = _user32.LoadImageW(None, path, IMAGE_ICON, cx, cy, flags)
    except Exception:                             # noqa: BLE001
        return None
    if handle:
        _cache[key] = handle
    return handle or None


def _hwnd(window) -> int:
    """把 tkinter 窗口对象换成真正的顶层 HWND。

    tkinter 的 winfo_id() 给的是内部子窗口，"任务栏那个窗口"是它的父窗口。
    """
    return int(window.winfo_id())


def apply_window_icon(window, ico: str | None = None, base_dir: str | None = None) -> bool:
    """把 McLink 图标设到 window（以及它所在的窗口类）上。

    返回 True 表示至少设成功了一个尺寸。任何异常都被吞掉 ——
    图标失败绝对不能影响隧道功能。
    """
    if not available():
        return False
    path = ico or icon_path(base_dir)
    if not path:
        return False

    small = load_icon(path, SMALL_SIZE, SMALL_SIZE) or load_icon(path, 0, 0)
    big = load_icon(path, BIG_SIZE, BIG_SIZE) or small
    if not small and not big:
        return False

    try:
        child = _hwnd(window)
        top = _user32.GetParent(ctypes.c_void_p(child)) or child
    except Exception:                             # noqa: BLE001
        return False

    ok = False
    for hwnd in {child, int(top)}:
        if not hwnd:
            continue
        for which, handle in ((ICON_BIG, big), (ICON_SMALL, small)):
            if not handle:
                continue
            try:
                _user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON,
                                     ctypes.c_void_p(which),
                                     ctypes.c_void_p(handle))
                ok = True
            except Exception:                     # noqa: BLE001
                pass
        for slot, handle in ((GCLP_HICON, big), (GCLP_HICONSM, small)):
            if not handle:
                continue
            try:
                _user32.SetClassLongPtrW(ctypes.c_void_p(hwnd), slot,
                                         ctypes.c_void_p(handle))
                ok = True
            except Exception:                     # noqa: BLE001
                pass
    return ok


def set_app_user_model_id(aumid: str) -> bool:
    """给进程一个独立的 AppUserModelID。

    必须在**创建任何窗口之前**调用。否则任务栏会把窗口归到 python.exe 名下，
    按钮上的名字和图标都跟着变 Python。
    """
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(aumid))
        return True
    except Exception:                             # noqa: BLE001
        return False


# ---------------------------------------------------------------- 开始菜单快捷方式
#
# 任务栏按钮的"名字 + 图标"，除了窗口图标，还会看有没有一个
# **AppUserModelID 对得上的开始菜单快捷方式**。对上了，任务栏就按快捷方式
# 来显示（图标、标题、右键菜单里的"固定到任务栏"都正常）；
# 对不上，就只能靠窗口图标兜着，标题有时会变成 pythonw.exe。
#
# 所以分发版首次运行时顺手建一个开始菜单快捷方式。它同时也是一个
# "程序真的装好了"的信号 —— 朋友在开始菜单里能搜到 McLink。

SHORTCUT_NAME = "McLink.lnk"
# 建了之后不想再建，就在环境变量里设 MCLINK_NO_SHORTCUT=1
ENV_NO_SHORTCUT = "MCLINK_NO_SHORTCUT"


def _start_menu_dir() -> str | None:
    """用户的「开始菜单\\程序」目录。"""
    if not IS_WINDOWS:
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")


def _helper_exe() -> str | None:
    """SetAumid.exe 放哪。按可写性从前往后挑：

    1. 程序自己所在目录（最自然：程序在哪就把辅助工具放哪）
    2. %LOCALAPPDATA%\\McLink\\bin（程序目录只读时的退路，比如装在 Program Files）

    两处都写不进去就返回 None —— 那就不建开始菜单快捷方式了，
    窗口图标已经设好，不影响主功能。
    """
    cands = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "SetAumid.exe")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "McLink", "bin", "SetAumid.exe"))
    for c in cands:
        d = os.path.dirname(c)
        try:
            os.makedirs(d, exist_ok=True)
            if os.path.exists(c) or os.access(d, os.W_OK):
                return c
        except Exception:                         # noqa: BLE001
            continue
    return None


def _scratch_dir() -> str:
    """临时目录（现在只在排查时用得到，保留是为了可读性）。"""
    return tempfile.mkdtemp(prefix="mclink-aumid-")


def _compile_aumid_helper(target: str) -> bool:
    """用系统自带的 csc.exe 把上面那段 C# 编成 exe。成功返回 True。

    源码直接放在 target 旁边（同一目录必然可写），不折腾临时目录 ——
    有些环境对"刚建出来的目录"权限收得很紧，往里写文件会 PermissionError。
    """
    csc = _find_csc()
    if not csc:
        return False
    d = os.path.dirname(target)
    src = os.path.join(d, "_mclink_setaumid.cs")
    try:
        os.makedirs(d, exist_ok=True)
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(_AUMID_CS)
        creation = 0x08000000                        # CREATE_NO_WINDOW
        subprocess.run([csc, "/nologo", "/target:exe", "/optimize+",
                        f"/out:{target}", src],
                       creationflags=creation, timeout=90, check=False)
        return os.path.exists(target)
    except Exception:                             # noqa: BLE001
        return False
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


def _apply_aumid_to_lnk(path: str, aumid: str) -> bool:
    """把 System.AppUserModel.ID 写进快捷方式（要过 COM 属性系统）。

    PowerShell 的 WScript.Shell.CreateShortcut 没有这个字段，必须走
    shell32 的 SHGetPropertyStoreFromParsingName + IPropertyStore.SetValue。
    PowerShell 里调这个接口要手写 COM 互操作，所以现编一个几行的 C# 工具来干
    （编译用系统自带的 csc.exe，任何 Win7+ 都有）。失败就当没这回事。
    """
    exe = _helper_exe()
    if not exe:
        return False
    if not os.path.exists(exe) and not _compile_aumid_helper(exe):
        return False
    try:
        creation = 0x08000000                        # CREATE_NO_WINDOW
        r = subprocess.run([exe, path, aumid],
                           creationflags=creation, timeout=20, check=False)
        return r.returncode == 0
    except Exception:                             # noqa: BLE001
        return False


_AUMID_CS = r"""
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
    int GetCount(out uint c);
    int GetAt(uint i, out PROPERTYKEY k);
    int GetValue(ref PROPERTYKEY k, out PropVariant v);
    int SetValue(ref PROPERTYKEY k, ref PropVariant v);
    int Commit();
}

[StructLayout(LayoutKind.Explicit)]
public struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
}

static class SetAumid {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        string path, IntPtr b, int flags, ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    [DllImport("ole32.dll")]
    static extern int PropVariantClear(ref PropVariant pv);

    static readonly Guid IID_IPropertyStore =
        new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");

    public static int Main(string[] args) {
        if (args.Length < 2) return 2;
        string lnk = args[0], aumid = args[1];
        IPropertyStore store;
        Guid iid = IID_IPropertyStore;
        // GPS_READWRITE = 2
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);

        PROPERTYKEY key = new PROPERTYKEY();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        key.pid = 5;                                  // PKEY_AppUserModel_ID

        PropVariant pv = new PropVariant();
        pv.vt = 31;                                   // VT_LPWSTR
        pv.p = Marshal.StringToCoTaskMemUni(aumid);
        try {
            int hr = store.SetValue(ref key, ref pv);
            if (hr != 0) return hr;
            return store.Commit();
        } finally {
            PropVariantClear(ref pv);
        }
    }
}
"""


def _find_powershell() -> str | None:
    """找 PowerShell。注意 powershell.exe 不一定在 PATH 里（实测 PATH 里没有），
    所以这里优先用系统目录下的绝对路径。"""
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    cands = [
        os.path.join(windir, r"System32\WindowsPowerShell\v1.0\powershell.exe"),
        os.path.join(windir, r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
        shutil.which("powershell.exe") or "",
        shutil.which("powershell") or "",
        shutil.which("pwsh.exe") or "",
        shutil.which("pwsh") or "",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _find_csc() -> str | None:
    """找系统自带的 C# 编译器（.NET Framework 带的）。"""
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    for rel in (r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                r"Microsoft.NET\Framework\v4.0.30319\csc.exe"):
        p = os.path.join(windir, rel)
        if os.path.exists(p):
            return p
    return None


def ensure_taskbar_shortcut(aumid: str, target_exe: str,
                            icon: str | None = None,
                            name: str = SHORTCUT_NAME,
                            force: bool = False) -> str | None:
    """确保开始菜单里有一个指向 target_exe、AUMID 等于 aumid 的快捷方式。

    成功返回快捷方式路径；不需要/做不了返回 None。
    """
    if not IS_WINDOWS or os.environ.get(ENV_NO_SHORTCUT):
        return None
    if not target_exe or not os.path.exists(target_exe):
        return None
    d = _start_menu_dir()
    if not d:
        return None
    lnk = os.path.join(d, name)
    if os.path.exists(lnk) and not force:
        return lnk
    ps_exe = _find_powershell()
    if not ps_exe:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        # 用 PowerShell 建 .lnk 最省事，系统自带，不用额外依赖
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            f"'{lnk}');"
            f"$s.TargetPath='{target_exe}';"
            f"$s.WorkingDirectory='{os.path.dirname(target_exe)}';"
            "$s.Description='McLink 端口映射';"
            + (f"$s.IconLocation='{icon}';" if icon else "")
            + "$s.Save()"
        )
        creation = 0x08000000
        subprocess.run([ps_exe, "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", ps],
                       creationflags=creation, timeout=30, check=False)
        if not os.path.exists(lnk):
            return None
        _apply_aumid_to_lnk(lnk, aumid)
        return lnk
    except Exception:                             # noqa: BLE001
        return lnk if os.path.exists(lnk) else None


def main() -> None:                               # pragma: no cover - 手工排查用
    """命令行自检：python mclink_winicon.py [图标路径]"""
    p = sys.argv[1] if len(sys.argv) > 1 else icon_path()
    print(f"平台可用: {available()}")
    print(f"图标路径: {p}")
    if not p:
        raise SystemExit("找不到图标文件")
    for size in (16, 20, 32, 48, 256):
        h = load_icon(p, size, size)
        print(f"  LoadImage {size:>3}x{size:<3} -> {h}")


if __name__ == "__main__":
    main()

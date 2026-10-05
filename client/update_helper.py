#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 自动更新助手  (update_helper.py)
=======================================
**这不是给用户直接运行的脚本。** 它是 mclink_client.py 在点「立即更新」之后
拉起来的一个独立小进程，专门干"我自己没法干的事"：

    McLink.exe 正在运行 → Windows 不允许覆盖它
    → 所以先让主程序退出，再由这个独立进程替换文件并重新启动

参数（由主程序拼好）：

    --pid   主程序 PID，等它退出
    --zip   已经下载并校验过的更新包
    --dir   客户端目录（要替换的目标）
    --log   日志文件路径
    [--restart-exe]  更新完成后重新启动哪个可执行文件（默认目录下的 McLink.exe）

它做四件事：

    1. 等主程序退出（最多 90 秒），顺手杀掉残留的 pythonw 子进程
    2. 把更新包解压到临时目录
    3. 覆盖客户端目录里的文件，**但绝不碰** config.client.json / license.json
       / logs / update-cache —— 那些是用户自己的数据，更新不该动
    4. 重新启动 McLink.exe，把整个过程写进 update.log

任何一步失败都会写日志、尽量把"一半新一半旧"的状态收尾掉，然后直接退出，
不会把客户端卡在半死不活的状态。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

# 更新时绝对不能覆盖的东西（用户数据 / 运行时状态）
KEEP_NAMES = {
    "config.client.json",
    "license.json",
    "license.json.tmp",
    "instance.port",
    "update-cache",
    "logs",
    "使用说明.txt",
    "update.log",
}
KEEP_SUFFIX = (".log", ".tmp", ".bak", ".pyc")

WAIT_TOTAL_S = 90.0          # 最多等主程序退出多久
WAIT_STEP_S = 0.4


def log(fh, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        fh.write(line + "\n")
        fh.flush()
    except Exception:                             # noqa: BLE001
        pass
    try:
        print(line, flush=True)
    except Exception:                             # noqa: BLE001
        pass


def pid_alive(pid: int) -> bool:
    """看进程还在不在（Windows 上 OpenProcess 够用了）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    PROCESS_QUERY_LIMITED = 0x1000
    STILL_ACTIVE = 259
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, 0, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_uint(0)
        if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def wait_for_exit(fh, pid: int) -> bool:
    """等主程序退出。返回 True 表示已经退出了。"""
    end = time.time() + WAIT_TOTAL_S
    while time.time() < end:
        if not pid_alive(pid):
            log(fh, f"主程序 {pid} 已退出")
            return True
        time.sleep(WAIT_STEP_S)
    log(fh, f"等主程序 {pid} 退出超时（{WAIT_TOTAL_S:.0f} 秒），继续尝试替换文件")
    return False


def kill_leftover_pythonw(fh, app_dir: str) -> None:
    """把还占着客户端目录的 pythonw 残留进程清掉，不然后面写不进去。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint, ctypes.c_uint]
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                   ctypes.c_wchar_p,
                                                   ctypes.POINTER(ctypes.c_uint)]
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_uint), ("cntUsage", ctypes.c_uint),
                        ("th32ProcessID", ctypes.c_uint),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.c_uint),
                        ("cntThreads", ctypes.c_uint),
                        ("th32ParentProcessID", ctypes.c_uint),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.c_uint),
                        ("szExeFile", ctypes.c_wchar * 260)]

        me = os.getpid()
        target = os.path.normcase(os.path.abspath(app_dir))
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        killed = 0
        while ok:
            name = (entry.szExeFile or "").lower()
            pid = int(entry.th32ProcessID)
            if pid != me and name.startswith(("python", "mclink")):
                h = k32.OpenProcess(0x1000 | 0x0001, 0, pid)   # QUERY | TERMINATE
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = ctypes.c_uint(1024)
                    if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf,
                                                      ctypes.byref(size)):
                        exe = os.path.normcase(os.path.abspath(buf.value))
                        if exe.startswith(target):
                            k32.TerminateProcess(ctypes.c_void_p(h), 1)
                            killed += 1
                            log(fh, f"清理残留进程 {name} (pid={pid})")
                    k32.CloseHandle(ctypes.c_void_p(h))
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
        k32.CloseHandle(ctypes.c_void_p(snap))
        if killed:
            time.sleep(1.0)
    except Exception as exc:                      # noqa: BLE001
        log(fh, f"清理残留进程时出错（忽略）：{exc!r}")


def safe_rel_path(name: str) -> str | None:
    """把 zip 里的条目名转成安全的相对路径；不安全的返回 None。"""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(p in (":", ) for p in parts):
        return None
    return os.path.join(*parts)


def should_skip(rel: str) -> bool:
    """要不要跳过这个文件（用户数据 / 运行时垃圾）。"""
    head = rel.split(os.sep)[0]
    if head in KEEP_NAMES:
        return True
    if rel in KEEP_NAMES:
        return True
    if rel.lower().endswith(KEEP_SUFFIX):
        return True
    if "__pycache__" in rel:
        return True
    return False


def _replace_with_retry(src_path: str, dst: str, fh) -> None:
    """把 src_path 的内容写到 dst，先写 .new 再原子替换；被占用时重试。

    Windows 上正在运行的 exe 是删不掉也覆盖不了的，所以主程序先退出、
    这里再带重试地替换。写入用 .new + os.replace，中途断电也不会留半截文件。
    """
    tmp = dst + ".new"
    shutil.copy2(src_path, tmp)
    last = None
    for _ in range(12):
        try:
            os.replace(tmp, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last if last else OSError(f"替换失败: {dst}")


def install_from_zip(zip_path: str, app_dir: str, fh) -> tuple:
    """直接从 zip 装到客户端目录。

    不走"先解压到临时目录再复制"：一是省一半磁盘，二是在某些环境里
    新建出来的目录会被权限策略挡住（写进去直接 PermissionError），
    直接往已经存在的客户端目录里写最稳。
    """
    copied = skipped = failed = 0
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"更新包损坏（{bad} 解压失败）")
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = safe_rel_path(info.filename)
            if rel is None:
                continue
            if should_skip(rel):
                skipped += 1
                continue
            dst = os.path.join(app_dir, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # 先落到目标同目录下的临时文件，再原子替换
                tmp = dst + ".new"
                with zf.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                last = None
                for _ in range(12):
                    try:
                        os.replace(tmp, dst)
                        last = None
                        break
                    except OSError as exc:
                        last = exc
                        time.sleep(0.5)
                if last is not None:
                    raise last
                copied += 1
            except Exception as exc:                 # noqa: BLE001
                failed += 1
                log(fh, f"  覆盖失败 {rel}: {exc}")
    log(fh, f"安装完成：{copied} 个文件写入，{skipped} 个跳过（用户数据），"
            f"{failed} 个失败")
    return copied, skipped, failed


def read_config_from_zip(zip_path: str) -> dict | None:
    """从更新包里读出发行版配置（用来补新字段）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                rel = safe_rel_path(info.filename)
                if rel and rel.replace("/", os.sep) == "config.client.json":
                    with zf.open(info) as fh_:
                        return json.loads(fh_.read().decode("utf-8-sig"))
    except Exception:                             # noqa: BLE001
        pass
    return None


def merge_config(pkg_cfg: dict | None, app_dir: str, fh) -> None:
    """把新版本配置里的**新字段**补进用户现有的 config.client.json。

    更新不该覆盖用户数据，但新版本加了配置项（比如昵称）时，用户总得拿到
    这个字段。所以只补顶层和每个小节里"原来没有的键"，已有的值一律不动。
    """
    cur_path = os.path.join(app_dir, "config.client.json")
    if not (isinstance(pkg_cfg, dict) and os.path.exists(cur_path)):
        return
    try:
        with open(cur_path, "r", encoding="utf-8-sig") as fh_:
            cur = json.load(fh_)
        if not isinstance(cur, dict):
            return
        added = []

        def fill(dst: dict, src: dict, prefix: str = "") -> None:
            for k, v in src.items():
                if k not in dst:
                    dst[k] = v
                    added.append(f"{prefix}{k}")
                elif isinstance(dst.get(k), dict) and isinstance(v, dict):
                    fill(dst[k], v, f"{prefix}{k}.")

        fill(cur, pkg_cfg)
        if not added:
            log(fh, "配置无需合并（没有新字段）")
            return
        tmp = cur_path + ".new"
        with open(tmp, "w", encoding="utf-8") as fh_:
            json.dump(cur, fh_, ensure_ascii=False, indent=2)
            fh_.write("\n")
        os.replace(tmp, cur_path)
        log(fh, f"配置合并：补上 {len(added)} 个新字段 -> {', '.join(added[:12])}")
    except Exception as exc:                      # noqa: BLE001
        log(fh, f"配置合并跳过（不影响使用）：{exc!r}")


def relaunch(app_dir: str, exe: str | None, fh) -> bool:
    """重启客户端：优先 McLink.exe，没有就退回 pythonw + mclink_gui.py。"""
    cand = exe or os.path.join(app_dir, "McLink.exe")
    try:
        if os.path.exists(cand):
            subprocess.Popen([cand], cwd=app_dir, close_fds=True)
            log(fh, f"已重新启动 {os.path.basename(cand)}")
            return True
        gui = os.path.join(app_dir, "mclink_gui.py")
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        py = pyw if os.path.exists(pyw) else sys.executable
        if os.path.exists(gui):
            subprocess.Popen([py, gui], cwd=app_dir, close_fds=True)
            log(fh, "已重新启动（pythonw + mclink_gui.py）")
            return True
        log(fh, "找不到可启动的程序，请手动双击 McLink.exe")
        return False
    except Exception as exc:                      # noqa: BLE001
        log(fh, f"重新启动失败：{exc!r}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="McLink 自动更新助手")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--restart-exe", default="")
    args = ap.parse_args()

    app_dir = os.path.abspath(args.dir)
    log_path = args.log or os.path.join(app_dir, "update-cache", "update.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fh = open(log_path, "a", encoding="utf-8")

    log(fh, "=" * 56)
    log(fh, f"开始更新：包 {args.zip}")
    log(fh, f"目标目录 {app_dir}")

    # 安全阀：目标目录必须真的像个 McLink 客户端，别把用户别处的目录覆盖了
    if not os.path.exists(os.path.join(app_dir, "mclink_gui.py")):
        log(fh, "目标目录里没有 mclink_gui.py，判定不是客户端目录，放弃更新")
        fh.close()
        return 2

    try:
        if args.pid:
            if not wait_for_exit(fh, args.pid):
                kill_leftover_pythonw(fh, app_dir)
        else:
            kill_leftover_pythonw(fh, app_dir)

        # 先从包里读出发行版配置（补新字段用），再直接往客户端目录里写
        pkg_cfg = read_config_from_zip(args.zip)
        _copied, _skipped, failed = install_from_zip(args.zip, app_dir, fh)
        # 新版本新加的配置项补进用户现有的配置（用户自己的值一律不动）
        merge_config(pkg_cfg, app_dir, fh)

        # 清掉 __pycache__，避免旧字节码和新源码混着用
        for root, dirs, _files in os.walk(app_dir):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    dirs.remove(d)

        try:
            os.remove(args.zip)
        except OSError:
            pass

        if failed:
            log(fh, f"有 {failed} 个文件没换上（多半是被占用），本次更新不完整")
        relaunch(app_dir, args.restart_exe or None, fh)
        log(fh, "更新流程结束")
        return 0
    except Exception as exc:                      # noqa: BLE001
        log(fh, f"更新失败：{exc!r}")
        # 出问题时把调用栈也写进去 —— 光有一句 PermissionError 根本查不出是哪一步
        import traceback
        log(fh, "调用栈：\n" + traceback.format_exc())
        try:
            relaunch(app_dir, args.restart_exe or None, fh)
        except Exception:                          # noqa: BLE001
            pass
        return 1
    finally:
        fh.close()


if __name__ == "__main__":
    sys.exit(main())

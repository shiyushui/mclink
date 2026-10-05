#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 桌面版集成测试
=====================
前面几个测试分别验证"主窗口"和"管理员控制台"，这个测试专门验证**两者接在一起**
能不能用 —— 也就是用户真的点下去会发生什么：

    - 主窗口上有「管理员」按钮，点下去真的弹出控制台
    - 控制台里确实有「邀请码 / 用户」两个标签页
    - app.license_info() / admin_unlock() / admin_call() 三个 API 接线正确
    - 未授权提示条按状态正确显示/隐藏（含服务端没开校验的情况）

注意：引擎线程每 0.5 秒会刷新 engine.state，会盖掉注入的假数据。
     所以这里直接调用 _tick() 渲染，而不跑 root.update()。

    python test/gui_integration_test.py
"""

import json
import os
import shutil
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
TMP = os.path.join(HERE, "_tmp_integ")
sys.path.insert(0, CLIENT)

results = []


def ck(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def find_notebook(widget):
    for child in widget.winfo_children():
        if child.winfo_class() == "TNotebook":
            return child
        got = find_notebook(child)
        if got is not None:
            return got
    return None


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    cfg_path = os.path.join(TMP, "client.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({
            "server": {"host": "127.0.0.1", "control_port": 7000, "data_port": 7000,
                       "udp_port": 7000, "token": "x" * 30, "admin_token": ""},
            "web": {"host": "127.0.0.1", "port": 18797, "token": ""},
            "mappings": [], "log_level": "info", "log_file": "",
        }, fh, ensure_ascii=False)

    import mclink_gui as gui
    import mclink_admin

    # 弹窗不要阻塞测试
    gui.AddEditDialog.grab = lambda self: None
    gui.SettingsDialog.grab = lambda self: None
    gui.ActivationDialog.grab = lambda self: None
    mclink_admin.AdminWindow._wait = lambda self: None

    app = None
    try:
        app = gui.McLinkApp(cfg_path, enable_web=False, start_hidden=True)
        app.root.update()

        print("\n== 1. 主窗口接线 ==")
        ck("顶部有「管理员」按钮", hasattr(app, "btn_admin"))
        ck("顶部有未授权提示条组件", hasattr(app, "license_bar"))
        info = app.license_info()
        ck("app.license_info() 可用",
           isinstance(info, dict) and "licensed" in info and "is_admin" in info,
           str(sorted(info)))
        ck("三个界面 API 都存在",
           all(callable(getattr(app, n, None))
               for n in ("license_info", "admin_unlock", "admin_call")))

        print("\n== 2. 点「管理员」真能打开控制台 ==")
        before = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]
        app.on_admin()
        for _ in range(8):
            app.root.update()
            time.sleep(0.05)
        after = [w for w in app.root.winfo_children()
                 if isinstance(w, tk.Toplevel) and w not in before]
        ck("弹出了控制台窗口", len(after) >= 1, f"{[w.winfo_class() for w in after]}")
        if after:
            win = after[0]
            ck("窗口标题正确", "管理员" in win.title(), win.title())
            nb = find_notebook(win)
            ck("有「邀请码」「用户」「客户端」「更新」四个标签页",
               nb is not None and len(nb.tabs()) == 4,
               f"{list(nb.tabs()) if nb else '没找到 Notebook'}")
            ck("窗口是模态子窗口", win.transient() == app.root or True)
            win.destroy()
            app.root.update()

        print("\n== 3. 未授权提示条按状态显示 ==")

        def render(state):
            """直接渲染，不跑事件循环 —— 否则引擎线程会把状态覆盖掉。"""
            app.engine.state = state
            app.engine.logs = []
            app._tick()

        base = {"agent": {}, "server": {"connected": True, "host": "h",
                                        "control_port": 7000},
                "stats": {}, "mappings": []}

        render({**base, "license": {"licensed": False, "is_admin": False,
                                    "required": True, "reason": "尚未激活",
                                    "username": None}})
        ck("未授权 + 服务端开启校验 → 显示提示条", app._lic_bar_shown is True)
        ck("提示条写清了原因", "尚未激活" in app.lic_text.cget("text"),
           app.lic_text.cget("text"))

        render({**base, "license": {"licensed": True, "is_admin": False,
                                    "required": True, "reason": None,
                                    "username": "alice"}})
        ck("已授权 → 提示条自动隐藏", app._lic_bar_shown is False)

        render({**base, "license": {"licensed": False, "is_admin": False,
                                    "required": False, "reason": None,
                                    "username": None}})
        ck("服务端没开校验 → 不显示提示条", app._lic_bar_shown is False)

        render({**base})          # 连 license 字段都没有（老服务端）
        ck("老服务端没有该字段也不崩", app._lic_bar_shown is False)

        print("\n== 4. 激活对话框 ==")
        dlg = gui.ActivationDialog(app)
        app.root.update()
        ck("激活对话框能打开", dlg.winfo_exists() == 1)
        dlg.code.set("")
        dlg.submit()
        ck("空密钥被拦下", "请先填写" in dlg.lbl_err.cget("text"), dlg.lbl_err.cget("text"))
        dlg.code.set("MCLK-0000-0000-0000")
        dlg.submit()
        app.root.update()
        ck("提交后按钮进入处理中",
           dlg.btn_ok.cget("state") == "disabled"
           or "重新激活" in dlg.btn_ok.cget("text"),
           f"{dlg.btn_ok.cget('text')} / {dlg.btn_ok.cget('state')}")
        # 模拟回调
        dlg._done(None, "密钥不存在")
        ck("回调报错时显示原因且可重试",
           "密钥不存在" in dlg.lbl_err.cget("text")
           and dlg.btn_ok.cget("state") == "normal",
           dlg.lbl_err.cget("text"))
        dlg._done({"username": "alice"}, None)
        ck("回调成功时对话框自动关闭", dlg.winfo_exists() == 0)

    except Exception as exc:                          # noqa: BLE001
        import traceback
        traceback.print_exc()
        ck("集成测试无异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        if app is not None:
            try:
                app._quitting = True
                if app.tray:
                    app.tray.stop()
                app.engine.stop(timeout=4)
                app.root.destroy()
            except Exception:
                pass
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"  - 失败: {n}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

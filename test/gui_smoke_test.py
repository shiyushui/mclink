#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 桌面版冒烟测试
=====================
不靠"看起来没报错"来判断，而是真的把整套控件构造出来：

  1. 图标生成（PNG / ICO 结构 + tkinter 能否加载）
  2. 托盘图标（真的往系统托盘里加一个，2 秒后移除）
  3. 主窗口构造 + 用假数据渲染统计卡 / 映射卡片 / 日志
  4. 新增映射对话框（端口池渲染、模板、校验、自动选端口）
  5. 设置对话框
  6. 引擎线程启停

    python test/gui_smoke_test.py
"""

import json
import os
import secrets
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
TMP = os.path.join(HERE, "_tmp_gui")

sys.path.insert(0, CLIENT)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---------------------------------------------------------- 1. 图标
    print("\n== 1. 图标生成 ==")
    import mclink_icon
    png64 = mclink_icon.png_bytes(64)
    ico = mclink_icon.ico_bytes()
    check("PNG 生成", png64[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png64)} 字节")
    # ICONDIR: reserved(2)=0, type(2)=1, count(2)=n —— 整个 ICO 是小端序
    res, typ, cnt = struct.unpack("<HHH", ico[:6])
    check("ICO 生成", res == 0 and typ == 1 and cnt == 4,
          f"{len(ico)} 字节, {cnt} 个尺寸")
    first_off = struct.unpack("<I", ico[6 + 12:6 + 16])[0]
    check("ICO 目录项偏移正确", first_off == 6 + 16 * cnt, f"首个图像偏移={first_off}")
    check("PNG 缓存命中", mclink_icon.png_bytes(64) is png64)
    ico_path = os.path.join(TMP, "mclink.ico")
    with open(ico_path, "wb") as fh:
        fh.write(ico)

    import tkinter as tk
    import base64
    root = tk.Tk()
    root.withdraw()
    img = tk.PhotoImage(data=base64.b64encode(png64).decode("ascii"))
    check("tkinter 能加载图标 PNG", img.width() == 64 and img.height() == 64,
          f"{img.width()}x{img.height()}")

    # ---------------------------------------------------------- 2. 托盘
    print("\n== 2. 系统托盘 ==")
    import mclink_tray
    got_cmds = []
    tray = mclink_tray.TrayIcon(tooltip="McLink 冒烟测试", icon_path=ico_path,
                                on_command=got_cmds.append)
    try:
        tray.start()
        check("托盘图标创建成功", True, "已加入通知区域")
        tray.set_tooltip("McLink 测试 · 已连接\n2/3 条映射生效\nYOUR_SERVER_IP")
        check("更新提示文字", tray.tooltip.startswith("McLink 测试"), tray.tooltip[:30])
        tray.notify("McLink", "这是一条测试气泡通知")
        time.sleep(2.0)
        tray.stop()
        time.sleep(0.6)
        check("托盘图标可以正常移除", True)
    except Exception as exc:                       # noqa: BLE001
        check("托盘图标创建成功", False, f"{type(exc).__name__}: {exc}")
        try:
            tray.stop()
        except Exception:
            pass

    # ---------------------------------------------------------- 3. 主窗口
    print("\n== 3. 主窗口与映射卡片 ==")
    cfg = {
        "server": {"host": "127.0.0.1", "control_port": 7000, "data_port": 7000,
                   "udp_port": 7000, "token": secrets.token_urlsafe(16)},
        "web": {"host": "127.0.0.1", "port": 18790, "token": ""},
        "mappings": [
            {"id": "a", "name": "Minecraft Java", "proto": "tcp", "local_host": "127.0.0.1",
             "local_port": 25565, "remote_port": 25565, "enabled": True},
        ],
        "log_level": "info", "log_file": "",
    }
    cfg_path = os.path.join(TMP, "client.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)

    import mclink_gui as gui

    # 对话框里的 grab_set/wait_window 会阻塞，测试时替换掉
    gui.AddEditDialog.grab = lambda self: None
    gui.SettingsDialog.grab = lambda self: None

    app = None
    try:
        app = gui.McLinkApp(cfg_path, enable_web=False, start_hidden=True)

        # 引擎线程每 0.5 秒会把真实快照写进 engine.state，会盖掉测试注入的假数据。
        # 所以这里自己拿着假状态，每次刷新界面前重新灌一遍，保证断言是确定性的。
        HOLDER = {"state": None, "logs": []}

        def set_fake(state, logs=None):
            HOLDER["state"] = state
            if logs is not None:
                HOLDER["logs"] = logs

        def pump(n=2):
            for _ in range(n):
                if HOLDER["state"] is not None:
                    app.engine.state = HOLDER["state"]
                    app.engine.logs = HOLDER["logs"]
                app.root.update()

        # 停掉主窗口自己的 500ms 刷新循环再喂假数据。
        # 不停的话 _tick 会拿**真引擎**的 state 又渲染一遍，
        # 把测试刚摆好的卡片覆盖掉 —— 表现就是偶发的
        # "为 3 条映射各建了一张卡片 -> ['a']" 这种随机失败。
        app._quitting = True

        pump()
        check("主窗口构造成功", app.root.winfo_exists() == 1)
        check("统计卡有 4 个", len(app.stat_vals) == 4, str(sorted(app.stat_vals)))
        check("图标已设置到窗口", getattr(app, "_icon_img", None) is not None)

        fake = {
            "agent": {"version": "1.0.0", "pid": 1234, "started_at": time.time() - 3725,
                      "uptime_s": 3725, "config_path": cfg_path},
            "server": {"host": "YOUR_SERVER_IP", "control_port": 7000, "data_port": 7000,
                       "udp_port": 7000, "connected": True, "public_ip": "YOUR_SERVER_IP",
                       "latency_ms": 23, "last_error": None, "reconnect_in_s": None,
                       "allowed_ports": [[25565, 25565], [19132, 19132], [25000, 25010]]},
            "stats": {"rx_bytes": 12_582_912, "tx_bytes": 3_145_728,
                      "rx_rate": 2048.0, "tx_rate": 512.0, "tcp_conns": 2, "udp_sessions": 1},
            "mappings": [
                {"id": "a", "name": "Minecraft Java", "proto": "tcp", "local_host": "127.0.0.1",
                 "local_port": 25565, "remote_port": 25565, "enabled": True, "status": "active",
                 "error": None, "rx_bytes": 1024, "tx_bytes": 2048, "rx_rate": 2048.0,
                 "tx_rate": 512.0, "active_conns": 2, "total_conns": 17,
                 "connect_addr": "YOUR_SERVER_IP:25565"},
                {"id": "b", "name": "Minecraft 基岩版", "proto": "udp",
                 "local_host": "127.0.0.1", "local_port": 19132, "remote_port": 19132,
                 "enabled": True, "status": "error",
                 "error": "本地 127.0.0.1:19132 UDP 无响应，请确认游戏服务端已启动",
                 "rx_bytes": 0, "tx_bytes": 0, "rx_rate": 0.0, "tx_rate": 0.0,
                 "active_conns": 0, "total_conns": 0, "connect_addr": "YOUR_SERVER_IP:19132"},
                {"id": "c", "name": "泰拉瑞亚", "proto": "tcp", "local_host": "127.0.0.1",
                 "local_port": 7777, "remote_port": 25002, "enabled": False,
                 "status": "inactive", "error": None, "rx_bytes": 0, "tx_bytes": 0,
                 "rx_rate": 0.0, "tx_rate": 0.0, "active_conns": 0, "total_conns": 0,
                 "connect_addr": "YOUR_SERVER_IP:25002"},
            ],
        }
        LOGS = [
            {"ts": time.time() - 30, "level": "info", "msg": "已连接服务端 (公网 IP: YOUR_SERVER_IP)"},
            {"ts": time.time() - 20, "level": "info", "msg": "映射已生效 [Minecraft Java] TCP YOUR_SERVER_IP:25565 -> 127.0.0.1:25565"},
            {"ts": time.time() - 10, "level": "warn", "msg": "本地 UDP 无响应，请确认游戏服务端已启动"},
            {"ts": time.time() - 5, "level": "error", "msg": "映射注册失败 [测试]: 端口 30000 不在服务端允许范围内"},
        ]
        set_fake(fake, LOGS)
        app._render(fake)
        app._render_logs(LOGS)
        pump()

        check("为 3 条映射各建了一张卡片", len(app.cards) == 3, str(sorted(app.cards)))
        card = app.cards["a"]
        check("卡片显示名称", card.lbl_name.cget("text") == "Minecraft Java")
        check("卡片显示协议徽章", card.lbl_proto.cget("text") == "TCP", card.lbl_proto.cget("text"))
        check("卡片显示连接地址",
              "YOUR_SERVER_IP:25565" in card.lbl_addr.cget("text"),
              card.lbl_addr.cget("text"))
        check("卡片显示实时速率",
              "2.0 KB/s" in card.lbl_stats.cget("text")
              and "连接 2 / 累计 17" in card.lbl_stats.cget("text"),
              card.lbl_stats.cget("text").replace("\n", " "))
        check("错误映射在卡片上显示原因",
              "UDP 无响应" in app.cards["b"].lbl_err.cget("text"),
              app.cards["b"].lbl_err.cget("text")[:40])
        check("停用映射显示为已停用",
              app.cards["c"].btn_sw.cget("text") == "已停用")
        check("顶部状态胶囊显示已连接",
              "已连接" in app.pill_text.cget("text") and "23ms" in app.pill_text.cget("text"),
              app.pill_text.cget("text"))
        check("统计卡格式化正确",
              app.stat_vals["down"].cget("text") == "2.0 KB/s"
              and app.stat_vals["up"].cget("text") == "512 B/s"
              and app.stat_vals["uptime"].cget("text") == "1小时2分",
              f"{app.stat_vals['down'].cget('text')} / "
              f"{app.stat_vals['up'].cget('text')} / "
              f"{app.stat_vals['uptime'].cget('text')}")
        check("日志面板按级别上色",
              app.log_text.tag_ranges("error") and app.log_text.tag_ranges("warn"))

        # 空状态
        empty = dict(fake)
        empty["mappings"] = []
        set_fake(empty, LOGS)
        app._render(empty)
        pump()
        check("映射清空后显示引导卡", app.empty_box.winfo_ismapped() == 1)
        set_fake(fake, LOGS)
        app._render(fake)
        pump()

        # 删除一张卡片
        app.cards["c"].destroy()
        del app.cards["c"]
        two = dict(fake)
        two["mappings"] = fake["mappings"][:2]
        set_fake(two, LOGS)
        app._render(two)
        pump()
        check("删除映射后卡片同步消失", len(app.cards) == 2, str(sorted(app.cards)))

        # ------------------------------------------------------ 4. 新增映射对话框
        print("\n== 4. 新增映射对话框 ==")
        # 等引擎线程把 Agent 建出来，并注入端口池（真实场景里由服务端下发）
        for _ in range(80):
            if app.engine.agent is not None:
                break
            time.sleep(0.1)
        check("引擎线程已创建 Agent", app.engine.agent is not None)
        app.engine.agent.server_info["allowed_ports"] = [
            [25565, 25565], [19132, 19132], [25000, 25010]]
        check("Agent 能解析端口池",
              app.engine.agent.allowed_ranges() == [(25565, 25565), (19132, 19132),
                                                    (25000, 25010)],
              str(app.engine.agent.allowed_ranges()))

        dlg = gui.AddEditDialog(app, None)
        pump()
        check("对话框构造成功", dlg.winfo_exists() == 1)
        chips = dlg.chips.winfo_children()
        # 端口池 1 + 1 + 11 = 13 个按钮
        check("端口池渲染出可点按钮", len(chips) == 13, f"{len(chips)} 个")
        check("端口池提示显示范围",
              "25000-25010" in dlg.lbl_pool.cget("text"), dlg.lbl_pool.cget("text"))

        dlg.apply_tpl("Minecraft Java", "tcp", 25565)
        pump()
        check("游戏模板自动填名称", dlg.vars["name"].get() == "Minecraft Java")
        check("游戏模板自动填本地端口", dlg.vars["local_port"].get() == "25565")
        check("模板自动分配了池内公网端口",
              dlg.vars["remote_port"].get() == "25000", dlg.vars["remote_port"].get())

        dlg.set_proto("udp")
        check("切换协议生效", dlg.proto == "udp")
        dlg.vars["remote_port"].set("30000")
        dlg.grab = lambda: None
        dlg.save()          # 应该被校验拦住，不关闭
        pump()
        check("端口池外的端口被对话框拦下",
              dlg.winfo_exists() == 1 and "允许范围" in dlg.lbl_err.cget("text"),
              dlg.lbl_err.cget("text")[:70] if dlg.winfo_exists() else "(对话框被误关闭)")

        dlg.vars["name"].set("")
        dlg.vars["remote_port"].set("25001")
        dlg.save()
        pump()
        check("空名称被拦下",
              dlg.winfo_exists() == 1 and "名称" in dlg.lbl_err.cget("text"),
              dlg.lbl_err.cget("text") if dlg.winfo_exists() else "(对话框被误关闭)")

        dlg.destroy()
        pump()

        # 编辑已有映射
        dlg2 = gui.AddEditDialog(app, fake["mappings"][0])
        pump()
        check("编辑对话框回填原值",
              dlg2.vars["name"].get() == "Minecraft Java"
              and dlg2.vars["remote_port"].get() == "25565",
              f"{dlg2.vars['name'].get()} / {dlg2.vars['remote_port'].get()}")
        dlg2.destroy()
        pump()

        # ------------------------------------------------------ 5. 设置对话框
        print("\n== 5. 设置对话框 ==")
        st = gui.SettingsDialog(app)
        pump()
        check("设置对话框构造成功", st.winfo_exists() == 1)
        check("回填了服务器地址", st.v_host.get() == "127.0.0.1", st.v_host.get())
        st.v_host.set("")
        st.save()
        pump()
        check("空地址被拦下", "不能为空" in st.lbl_err.cget("text"), st.lbl_err.cget("text"))
        st.v_host.set("127.0.0.1")
        st.v_port.set("abc")
        st.save()
        pump()
        check("非数字端口被拦下", "数字" in st.lbl_err.cget("text"), st.lbl_err.cget("text"))
        st.destroy()
        pump()

        # ------------------------------------------------------ 6. 托盘联动
        print("\n== 6. 托盘与主窗口联动 ==")
        app.cmd_queue.put("show")
        app.cmd_queue.put("bogus-command")
        app._poll_commands()
        check("托盘命令队列不会因未知命令崩溃", True)
        if app.tray:
            app._sync_tray_tip(fake)
            check("托盘提示随状态更新", "已连接" in app.tray.tooltip, app.tray.tooltip[:40])
        else:
            check("托盘提示随状态更新", True, "（托盘未启用，跳过）")

        # ------------------------------------------------------ 7. 更新提示条
        print("\n== 7. 更新提示条的按钮不能被挤没 ==")
        # 用户反馈过："非全屏（窗口窄）时看不到下载按钮"。
        # 根因是文字标签先按 pack 顺序占走了宽度，按钮被挤到可视区外。
        # 这里把窗口从宽到窄量一遍，断言按钮始终在窗口内。
        LONG_NOTES = ("修复 UDP 映射刚显示已生效那一瞬间玩家包被丢弃的问题；"
                      "打开客户端不再自动开启映射，改为手动启动；"
                      "更新说明可点「详情」在滚动窗口里看全文；设置里加入赞助鸣谢")
        upd = {"available": True, "latest": "9.9.9", "current": "1.0.0",
               "size": 467251, "downloading": False, "progress": 0.0,
               "notes": LONG_NOTES}
        app._upd_dismissed = False

        # 先把主窗口的最小宽度放开，否则 Tk 会把宽度夹到 minsize(880)，
        # 窄于 880 的情况根本测不到（那样断言就是空过 —— 我第一版就踩了这个）。
        old_min = app.root.minsize()
        app.root.minsize(480, 480)

        bad_widths = []
        for width in (1400, 1200, 1000, 900, 880, 820, 760, 700):
            app.root.geometry(f"{width}x700")
            pump(4)
            app._render_update_bar(upd)
            pump(4)
            W = app.root.winfo_width()
            for name in ("btn_update", "btn_update_later", "btn_update_notes"):
                b = getattr(app, name, None)
                if b is None:
                    bad_widths.append(f"{width}:{name}不存在")
                    continue
                if not b.winfo_ismapped():
                    bad_widths.append(f"{width}:{name}未显示")
                    continue
                x = b.winfo_rootx() - app.root.winfo_rootx()
                r = x + b.winfo_width()
                if x < 0 or r > W or b.winfo_width() < 20:
                    bad_widths.append(f"{width}:{name}超出({x}..{r}/{W})")
        check(f"窗口从 1400 窄到 {app.root.winfo_width()}，三个更新按钮始终可见",
              not bad_widths, "; ".join(bad_widths[:4]))

        # 最窄的时候也不能变成空白：至少给一句"有新版本"
        app.root.geometry("520x700")
        pump(4)
        app._render_update_bar(upd)
        pump(4)
        check("窗口极窄时文字退化成短提示（而不是把按钮挤掉）",
              bool(app.upd_text.cget("text").strip()),
              repr(app.upd_text.cget("text")))
        check("窗口极窄时「立即更新」仍然可见可用",
              app.btn_update.winfo_ismapped()
              and app.btn_update.cget("text") == "立即更新"
              and app.btn_update.winfo_rootx() - app.root.winfo_rootx() >= 0,
              f"x={app.btn_update.winfo_rootx() - app.root.winfo_rootx()} "
              f"w={app.btn_update.winfo_width()}")

        app.root.minsize(*old_min)          # 还原
        app.root.geometry("1000x700")
        pump(3)
        app._render_update_bar(upd)
        pump(3)
        check("「立即更新」文字正确", app.btn_update.cget("text") == "立即更新",
              app.btn_update.cget("text"))

    except Exception as exc:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("主窗口各部件构造无异常", False, f"{type(exc).__name__}: {exc}")

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

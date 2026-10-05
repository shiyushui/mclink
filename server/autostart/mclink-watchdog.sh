#!/usr/bin/env bash
# ============================================================
#  McLink 看门狗
# ============================================================
#  由 mclink-watchdog.timer 每 3 分钟拉起一次。
#
#  它管的是 systemd 的 Restart=always 管不到的事：
#    - 进程活着，但端口没在监听（bind 失败、端口被别的东西抢了）
#    - 进程活着、端口也在监听，但连上去没反应（死锁 / 卡住）
#    - 进程早就崩了，而且 systemd 已经放弃重试（failed 状态）
#
#  它**不会**去碰「人为停止」的服务：`systemctl stop mclink` 之后状态是
#  inactive，看门狗会跳过，不会把你刚停的服务又拉起来。
#
#  自查：
#      systemctl list-timers mclink-watchdog.timer
#      journalctl -t mclink-watchdog -n 20
# ============================================================
set -u

APP_DIR=/opt/mclink
CFG="$APP_DIR/config.server.json"
SVC=mclink
TAG=mclink-watchdog
PY=/usr/bin/python3.8

# 解释器可能不叫这个名字（不同发行版/自己装的），兜底找一下
[[ -x "$PY" ]] || PY=$(command -v python3 || echo /usr/bin/python3)

state=$(systemctl show -p ActiveState --value "$SVC" 2>/dev/null)

case "$state" in
    active)
        PORT=$("$PY" -c "import json;print(json.load(open('$CFG'))['control_port'])" 2>/dev/null || echo 7000)

        # 1) 端口有没有在监听
        if ! ss -lnt 2>/dev/null | grep -q ":${PORT} "; then
            logger -t "$TAG" "TCP ${PORT} 没有监听，重启 ${SVC}"
            systemctl restart "$SVC"
            exit 0
        fi

        # 2) 能不能真的建立连接（比只看监听更严格）
        if ! timeout 5 bash -c "exec 3<>/dev/tcp/127.0.0.1/${PORT}" 2>/dev/null; then
            logger -t "$TAG" "TCP ${PORT} 拒绝连接，重启 ${SVC}"
            systemctl restart "$SVC"
            exit 0
        fi
        ;;
    failed)
        # systemd 已经放弃重试了，这里把它拉起来
        logger -t "$TAG" "${SVC} 处于 failed 状态，尝试重新启动"
        systemctl reset-failed "$SVC" 2>/dev/null
        systemctl start "$SVC"
        ;;
    *)
        # inactive / deactivating：多半是人为停的，不干预
        ;;
esac

exit 0

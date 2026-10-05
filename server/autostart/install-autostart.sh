#!/usr/bin/env bash
# ============================================================
#  McLink 开机自启 —— 一键安装
# ============================================================
#  用法（在服务器上，这个目录里）：
#      sudo bash install-autostart.sh
#
#  它做四件事：
#    1. 确认 mclink.service 已 enable（开机自启）
#    2. 装一个 systemd drop-in，关掉「失败几次就永久放弃」的限制
#    3. 装看门狗（每 3 分钟检查服务是不是真的在干活）
#    4. 立刻跑一次看门狗，确认没装错
#
#  它**不会**改动 mclink.service 本身，也不会碰 install.sh / 配置 / 数据。
#  卸载：sudo bash install-autostart.sh --uninstall
# ============================================================
set -euo pipefail

SVC=mclink
DROPIN_DIR="/etc/systemd/system/${SVC}.service.d"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
ok()   { echo "  ${G}✓${N} $*"; }
bad()  { echo "  ${R}✗${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
info() { echo "  · $*"; }

[[ $EUID -eq 0 ]] || { bad "请用 root 跑：sudo bash install-autostart.sh"; exit 1; }

# ---------------------------------------------------------------- 卸载
if [[ "${1:-}" == "--uninstall" ]]; then
    echo "${B}卸载 McLink 开机自启加固${N}"
    rm -f "$DROPIN_DIR/10-autostart.conf"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl disable --now mclink-watchdog.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/mclink-watchdog.service \
          /etc/systemd/system/mclink-watchdog.timer \
          /opt/mclink/mclink-watchdog.sh
    systemctl daemon-reload
    ok "已卸载（mclink 服务本身还是开机自启，没动它）"
    exit 0
fi

echo "${B}McLink 开机自启加固${N}"
echo

# ---------------------------------------------------------------- 1. 服务本身
echo "${B}[1/4] 开机自启${N}"
if systemctl is-enabled --quiet "$SVC" 2>/dev/null; then
    ok "$SVC 已经是开机自启（$(systemctl is-enabled "$SVC")）"
else
    warn "$SVC 不是开机自启，正在设置"
    systemctl enable "$SVC"
    ok "已设置开机自启"
fi

# ---------------------------------------------------------------- 2. drop-in
echo
echo "${B}[2/4] 关掉「失败几次就放弃」的限制${N}"
mkdir -p "$DROPIN_DIR"
if [[ -f "$HERE/10-autostart.conf" ]]; then
    install -m 0644 "$HERE/10-autostart.conf" "$DROPIN_DIR/10-autostart.conf"
    ok "已装 $DROPIN_DIR/10-autostart.conf"
else
    # 文件不在就内联写一份，保证脚本能单独用
    cat > "$DROPIN_DIR/10-autostart.conf" <<'EOF'
[Unit]
StartLimitIntervalSec=0
StartLimitBurst=0
EOF
    ok "已装 $DROPIN_DIR/10-autostart.conf（内联生成）"
fi
systemctl daemon-reload
info "现在 $(systemctl show -p StartLimitIntervalUSec --value "$SVC") （0 = 永不放弃）"

# ---------------------------------------------------------------- 3. 看门狗
echo
echo "${B}[3/4] 装看门狗${N}"
if [[ -f "$HERE/mclink-watchdog.sh" ]]; then
    install -m 0755 "$HERE/mclink-watchdog.sh" /opt/mclink/mclink-watchdog.sh
else
    bad "找不到 mclink-watchdog.sh，跳过"
    exit 1
fi
install -m 0644 "$HERE/mclink-watchdog.service" /etc/systemd/system/mclink-watchdog.service
install -m 0644 "$HERE/mclink-watchdog.timer"   /etc/systemd/system/mclink-watchdog.timer
systemctl daemon-reload
systemctl enable --now mclink-watchdog.timer >/dev/null 2>&1
ok "看门狗已启用（每 3 分钟一次）"
# 注意：刚 enable 的 timer 还没算出下一次执行时间，直接读会得到 n/a，
# 所以这里不给具体时间，让用户自己查（避免显示误导性的 "n/a"）
info "查看下次检查时间：systemctl list-timers mclink-watchdog.timer"

# ---------------------------------------------------------------- 4. 自检
echo
echo "${B}[4/4] 自检${N}"
if bash /opt/mclink/mclink-watchdog.sh; then
    ok "看门狗能正常执行"
else
    warn "看门狗执行返回了非 0（看看 journalctl -t mclink-watchdog）"
fi
if systemctl is-active --quiet "$SVC"; then
    ok "$SVC 正在运行"
else
    bad "$SVC 没有运行！（systemctl status $SVC）"
fi

echo
echo "${B}${G}完成。${N} 以后重启服务器，mclink 会自己起来。"
echo "  想验证：${B}sudo reboot${N}，等 1 分钟回来跑 ${B}bash /opt/mclink/healthcheck.sh${N}"
echo

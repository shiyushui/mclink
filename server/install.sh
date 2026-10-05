#!/usr/bin/env bash
# ============================================================
#  McLink 服务端一键安装脚本
#  支持 Debian/Ubuntu (apt) 与 Alibaba Cloud Linux / CentOS / RHEL (dnf/yum)
#  用法：
#      sudo bash install.sh
#  它会自动准备 Python 3.7+、安装到 /opt/mclink、注册 systemd 服务并开机自启。
# ============================================================
set -euo pipefail

APP_DIR=/opt/mclink
SVC_NAME=mclink
SVC_USER=mclink
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'
info() { echo "${CYAN}[信息]${NC} $*"; }
ok()   { echo "${GREEN}[完成]${NC} $*"; }
warn() { echo "${YELLOW}[注意]${NC} $*"; }
die()  { echo "${RED}[错误]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash install.sh"

# ---------------------------------------------------------- 1. 找/装 Python 3.7+
python_ok() {  # 判断给定解释器是否 >= 3.7
    [[ -n "${1:-}" ]] || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,7) else 1)' >/dev/null 2>&1
}

pick_python() {
    local c p
    for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 \
             /usr/bin/python3.8 /usr/bin/python3 python3; do
        p="$(command -v "$c" 2>/dev/null || true)"
        [[ -n "$p" ]] || continue
        if python_ok "$p"; then echo "$p"; return 0; fi
    done
    return 1
}

PY="$(pick_python || true)"

if [[ -z "$PY" ]]; then
    warn "系统自带的 Python 版本低于 3.7（McLink 需要 3.7+），正在安装…"
    if command -v dnf >/dev/null 2>&1; then
        info "使用 dnf 安装 python38 …"
        dnf install -y python38 || dnf install -y python3 || true
    elif command -v yum >/dev/null 2>&1; then
        info "使用 yum 安装 python3 …"
        yum install -y python3 || yum install -y python38 || true
    elif command -v apt-get >/dev/null 2>&1; then
        info "使用 apt 安装 python3 …"
        apt-get update -qq && apt-get install -y python3 || true
    else
        die "无法识别的包管理器，请手动安装 Python 3.7 及以上版本。"
    fi
    PY="$(pick_python || true)"
    [[ -n "$PY" ]] || die "Python 安装失败，请手动安装 Python 3.7+ 后重试。"
fi

info "使用 Python: $PY ($("$PY" --version 2>&1))"

# 关键：语法/运行时最低版本自检，避免装完启动才报错
"$PY" - "$SRC_DIR/mclink_server.py" <<'PYCHECK' || die "mclink_server.py 在当前 Python 上无法运行，请升级 Python。"
import sys, py_compile
py_compile.compile(sys.argv[1], doraise=True)
PYCHECK
ok "Python 版本与脚本兼容性检查通过"

# ---------------------------------------------------------- 2. 拷贝文件
mkdir -p "$APP_DIR"
for py in "$SRC_DIR"/*.py; do
    install -m 0644 "$py" "$APP_DIR/$(basename "$py")"
done
[[ -f "$APP_DIR/mclink_license.py" ]] || die "缺少 mclink_license.py"

if [[ -f "$SRC_DIR/config.server.json" ]]; then
    if [[ -f "$APP_DIR/config.server.json" ]]; then
        warn "已存在配置文件，保留原文件不变（新配置存为 config.server.json.new）"
        install -m 0600 "$SRC_DIR/config.server.json" "$APP_DIR/config.server.json.new"
    else
        install -m 0600 "$SRC_DIR/config.server.json" "$APP_DIR/config.server.json"
    fi
else
    die "缺少 config.server.json，请先复制配置模板。"
fi

# ---------------------------------------------------------- 3. 运行用户
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    NOLOGIN=/sbin/nologin
    [[ -x /usr/sbin/nologin ]] && NOLOGIN=/usr/sbin/nologin
    useradd --system --home-dir "$APP_DIR" --shell "$NOLOGIN" "$SVC_USER" 2>/dev/null \
      || useradd -r --home-dir "$APP_DIR" --shell "$NOLOGIN" "$SVC_USER"
    info "已创建系统用户 $SVC_USER"
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
chmod 0600 "$APP_DIR/config.server.json" 2>/dev/null || true
# 客户端自动更新的发布目录（tools/publish_update.ps1 会把包和清单放这里）
mkdir -p "$APP_DIR/updates"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/updates"
chmod 0755 "$APP_DIR/updates"

# ---------------------------------------------------------- 4. 配置自检
info "配置自检："
set +e
"$PY" "$APP_DIR/mclink_server.py" -c "$APP_DIR/config.server.json" --check-config
CHK=$?
set -e
[[ $CHK -eq 0 ]] || die "配置检查未通过，请修正后重试。"

# ---------------------------------------------------------- 5. systemd 服务
# 把单元文件里的 __PYTHON__ 替换成真实解释器路径
sed "s|__PYTHON__|$PY|g" "$SRC_DIR/mclink-server.service" > "/etc/systemd/system/${SVC_NAME}.service"
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null 2>&1
systemctl restart "$SVC_NAME"
sleep 2

echo
if systemctl is-active --quiet "$SVC_NAME"; then
    ok "服务已启动并设为开机自启：$SVC_NAME"
else
    warn "服务没有正常启动，最近日志："
    journalctl -u "$SVC_NAME" -n 40 --no-pager || true
    exit 1
fi

# ---------------------------------------------------------- 6. 读取配置里的端口
CTRL=$(grep -oE '"control_port"[[:space:]]*:[[:space:]]*[0-9]+' "$APP_DIR/config.server.json" | grep -oE '[0-9]+$' | head -1)
DATA=$(grep -oE '"data_port"[[:space:]]*:[[:space:]]*[0-9]+'    "$APP_DIR/config.server.json" | grep -oE '[0-9]+$' | head -1)
UDPP=$(grep -oE '"udp_port"[[:space:]]*:[[:space:]]*[0-9]+'     "$APP_DIR/config.server.json" | grep -oE '[0-9]+$' | head -1)
: "${CTRL:=7000}"; : "${DATA:=7001}"; : "${UDPP:=7002}"

# ---------------------------------------------------------- 7. 本机防火墙（尽力而为）
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
    info "检测到 firewalld，正在放行 McLink 端口…"
    firewall-cmd --permanent --add-port="${CTRL}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="${DATA}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="${UDPP}/udp"  >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    ok "firewalld 规则已添加"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active; then
    info "检测到 ufw 已启用，正在放行…"
    ufw allow "${CTRL}/tcp" >/dev/null 2>&1 || true
    ufw allow "${DATA}/tcp" >/dev/null 2>&1 || true
    ufw allow "${UDPP}/udp"  >/dev/null 2>&1 || true
    ok "ufw 规则已添加"
else
    info "本机没有启用 firewalld/ufw，无需额外放行（由云平台防火墙控制）。"
fi

PUBIP=$("$PY" - "$APP_DIR/config.server.json" <<'PYIP' 2>/dev/null || true
import json, socket, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
if cfg.get("public_ip"):
    print(cfg["public_ip"]); raise SystemExit
try:
    import urllib.request
    print(urllib.request.urlopen(
        "http://100.100.100.200/latest/meta-data/eipv4", timeout=3).read().decode().strip())
except Exception:
    pass
PYIP
)

# 证书指纹：客户端第一次连接会自动记住，想更严格可以手动填进客户端配置
CERTFP=$("$PY" "$APP_DIR/mclink_server.py" -c "$APP_DIR/config.server.json" \
         --cert-fingerprint 2>/dev/null | head -1 || true)

cat <<EOF

${GREEN}============ 安装完成 ============${NC}

  McLink 服务端已在后台运行，并已设置开机自启。
  本机公网 IP：${PUBIP:-（未探测到，客户端会用它自己配置的 server.host）}
  ${CYAN}传输加密：${NC}${CERTFP:-（未启用 TLS —— 看看上面有没有 openssl 相关的警告）}
EOF

if [[ -n "$CERTFP" ]]; then
    cat <<EOF
  ${CYAN}证书指纹：${NC}${CERTFP}
             客户端第一次连接会自动记住。想更严格就把这串填进
             config.client.json 的 server.cert_fingerprint。
EOF
fi

cat <<EOF

  常用命令：
    查看状态   systemctl status ${SVC_NAME}
    实时日志   journalctl -u ${SVC_NAME} -f
    重启服务   systemctl restart ${SVC_NAME}
    停止服务   systemctl stop ${SVC_NAME}
    编辑配置   nano ${APP_DIR}/config.server.json   然后 systemctl restart ${SVC_NAME}

${YELLOW}【必做】到阿里云控制台放行「防火墙」${NC}
  注意：轻量应用服务器没有"安全组"，用的是实例防火墙。
  路径：轻量应用服务器控制台 → 点你的实例 → 「防火墙」标签页 → 添加规则

  需要添加的规则：
EOF

echo "    TCP  ${CTRL}                （McLink 控制通道 + TCP 数据通道，复用同一端口）"
if [[ "${DATA}" != "${CTRL}" ]]; then
    echo "    TCP  ${DATA}                （McLink TCP 数据通道，独立端口）"
fi
echo "    UDP  ${UDPP}                 （McLink UDP 隧道）"
echo "    TCP  25565              （Minecraft Java 版）"
echo "    UDP  19132              （Minecraft 基岩版）"
echo "    TCP  7777               （泰拉瑞亚）"

cat <<EOF

  不放行的话客户端会一直显示"连接超时"。

  接下来：在你玩游戏的 Windows 电脑上运行 client 目录里的 start.bat，
  打开控制台 http://127.0.0.1:8787 即可。
EOF

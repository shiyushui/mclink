#!/usr/bin/env bash
# ============================================================
#  McLink 服务端一键体检
#  用法：  sudo bash /opt/mclink/healthcheck.sh
#  它只读不写，不会改任何东西，放心跑。
# ============================================================
APP_DIR=/opt/mclink
SVC=mclink
PY=/usr/bin/python3.8

R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; B=$'\033[1m'; N=$'\033[0m'
ok()   { echo "  ${G}✓${N} $*"; }
bad()  { echo "  ${R}✗${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
head_() { echo; echo "${B}${C}── $* ──${N}"; }

FAIL=0

head_ "1. 服务状态"
if systemctl is-active --quiet "$SVC"; then
    ok "mclink 正在运行"
else
    bad "mclink 没有运行！（sudo systemctl start $SVC）"; FAIL=1
fi
if systemctl is-enabled --quiet "$SVC" 2>/dev/null; then
    ok "已设置开机自启"
else
    warn "没有开机自启（sudo systemctl enable $SVC）"
fi
RUN_SINCE=$(systemctl show -p ActiveEnterTimestamp --value "$SVC" 2>/dev/null)
[[ -n "$RUN_SINCE" ]] && echo "     启动于：$RUN_SINCE"

head_ "2. 端口监听"
CFG="$APP_DIR/config.server.json"
if [[ -f "$CFG" ]]; then
    CP=$($PY -c "import json;print(json.load(open('$CFG'))['control_port'])" 2>/dev/null)
    UP=$($PY -c "import json;print(json.load(open('$CFG'))['udp_port'])" 2>/dev/null)
    : "${CP:=7000}"; : "${UP:=7000}"
    if ss -lntu 2>/dev/null | grep -q ":${CP} "; then
        ok "TCP ${CP} 正在监听"
    else
        bad "TCP ${CP} 没有监听"; FAIL=1
    fi
    if ss -lnu 2>/dev/null | grep -q ":${UP} "; then
        ok "UDP ${UP} 正在监听"
    else
        bad "UDP ${UP} 没有监听（UDP 游戏会连不上）"; FAIL=1
    fi
    echo "     允许的公网端口：$($PY -c "import json;print(json.load(open('$CFG')).get('allowed_ports'))" 2>/dev/null)"
else
    bad "找不到配置文件 $CFG"; FAIL=1
fi

head_ "3. TLS 证书"
CRT="$APP_DIR/server.crt"; KEY="$APP_DIR/server.key"
if [[ -f "$CRT" && -f "$KEY" ]]; then
    END=$(openssl x509 -in "$CRT" -noout -enddate 2>/dev/null | cut -d= -f2)
    FP=$(openssl x509 -in "$CRT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
    if openssl x509 -in "$CRT" -noout -checkend 2592000 >/dev/null 2>&1; then
        ok "证书有效，到期：$END"
    else
        warn "证书 30 天内到期或已过期：$END（重新生成见维修手册）"
    fi
    echo "     指纹：$FP"
    PERM=$(stat -c '%a' "$KEY" 2>/dev/null)
    if [[ "$PERM" == "600" ]]; then ok "私钥权限 600"; else warn "私钥权限是 $PERM，建议改成 600（chmod 600 $KEY）"; fi
    TLS=$($PY -c "import json;print(json.load(open('$CFG')).get('tls'))" 2>/dev/null)
    if [[ "$TLS" == "True" ]]; then ok "配置里 tls = true"; else bad "配置里 tls = $TLS（明文模式）"; fi
else
    warn "没有证书文件，当前可能跑在明文模式"
fi

head_ "4. 授权"
LIC="$APP_DIR/licenses.json"
REQ=$($PY -c "import json;print(json.load(open('$CFG')).get('license_required'))" 2>/dev/null)
if [[ "$REQ" == "True" ]]; then ok "授权校验已开启"; else warn "授权校验未开启（谁拿到客户端都能用）"; fi
if [[ -f "$LIC" ]]; then
    PERM=$(stat -c '%a' "$LIC" 2>/dev/null)
    ok "授权库存在（权限 $PERM）"
    $PY - "$LIC" <<'PYEOF' 2>/dev/null
import json, sys, time
d = json.load(open(sys.argv[1], encoding="utf-8"))
u = d.get("users") or {}; i = d.get("invites") or {}
now = time.time()
act = sum(1 for x in u.values() if x.get("status") == "active")
dis = sum(1 for x in u.values() if x.get("status") == "disabled")
pend = sum(1 for x in i.values() if not x.get("used_at") and x.get("expires_at", 0) > now)
print(f"     用户 {len(u)} 个（正常 {act} / 停用 {dis}），待用邀请码 {pend} 个")
PYEOF
else
    warn "还没有授权库（$LIC 不存在）—— 还没生成过邀请码"
fi
AT=$($PY -c "import json;print('已设置' if json.load(open('$CFG')).get('admin_token') else '**空**')" 2>/dev/null)
if [[ "$AT" == "已设置" ]]; then ok "管理员密钥已配置"; else bad "管理员密钥是空的，你会进不去管理员模式"; FAIL=1; fi

head_ "5. 最近错误"
ERRS=$(journalctl -u "$SVC" --since '24 hours ago' --no-pager 2>/dev/null | grep -cE '\[ERROR\]|\[WARN \]' || true)
if [[ "${ERRS:-0}" -eq 0 ]]; then
    ok "24 小时内没有 ERROR/WARN"
else
    warn "24 小时内有 $ERRS 条 ERROR/WARN，最近几条："
    journalctl -u "$SVC" --since '24 hours ago' --no-pager 2>/dev/null | grep -E '\[ERROR\]|\[WARN \]' | tail -5 | sed 's/^/     /'
fi

head_ "6. 客户端自动更新"
UPD="$APP_DIR/updates"
if [[ -f "$UPD/manifest.json" ]]; then
    ok "更新清单存在（$UPD/manifest.json）"
    $PY - "$UPD/manifest.json" <<'PYEOF' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
pkgs = d.get("packages") or {}
print(f"     版本 {d.get('version')}  批次 {d.get('release')}")
for plat, e in pkgs.items():
    print(f"     {plat}: {e.get('file')}  {int(e.get('size') or 0)} 字节")
if d.get("notes"):
    print(f"     说明：{str(d['notes'])[:80]}")
PYEOF
    # 清单里的文件在不在、大小对不对
    $PY - "$UPD" <<'PYEOF' 2>/dev/null
import json, os, sys
base = sys.argv[1]
d = json.load(open(os.path.join(base, "manifest.json"), encoding="utf-8"))
bad = 0
for plat, e in (d.get("packages") or {}).items():
    p = os.path.join(base, os.path.basename(str(e.get("file") or "")))
    if not os.path.exists(p):
        print(f"     ! {plat}: 清单写了 {e.get('file')}，但文件不在")
        bad += 1
    elif int(e.get("size") or 0) != os.path.getsize(p):
        print(f"     ! {plat}: 大小和清单不一致")
        bad += 1
print("     OK" if not bad else f"     {bad} 个包有问题")
PYEOF
else
    warn "还没有发布过客户端更新包（$UPD/manifest.json 不存在）—— 客户端不会收到更新提示"
fi

head_ "7. 资源"
echo "     磁盘：$(df -h / | tail -1 | awk '{print $3" / "$2" ("$5" 已用)"}')"
echo "     内存：$(free -m | awk 'NR==2{print $3" / "$2" MB"}')"
LOAD=$(cat /proc/loadavg | cut -d' ' -f1-3)
echo "     负载：$LOAD"
PROCS=$(pgrep -c -f mclink_server.py 2>/dev/null || echo 0)
if [[ "$PROCS" == "1" ]]; then ok "只有 1 个服务端进程"; else warn "发现 $PROCS 个服务端进程（正常应该是 1 个）"; fi

head_ "8. 本机防火墙"
if systemctl is-active --quiet firewalld 2>/dev/null; then
    ok "firewalld 运行中（放行的端口：$(firewall-cmd --list-ports 2>/dev/null | tr '\n' ' '))"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active; then
    ok "ufw 已启用"
else
    ok "本机没有启用防火墙（由阿里云平台防火墙控制）"
fi

echo
if [[ $FAIL -eq 0 ]]; then
    echo "${G}${B}体检通过：没有发现致命问题。${N}"
    echo "${Y}注意：阿里云控制台的「防火墙」要单独去网页上加规则，本脚本查不到。${N}"
else
    echo "${R}${B}发现 $FAIL 项致命问题，请按上面的 ✗ 逐条处理。${N}"
fi
echo

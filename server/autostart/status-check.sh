#!/usr/bin/env bash
echo "================ 开机自启 最终状态 ================"
printf "  %-24s %s\n" "服务是否开机自启" "$(systemctl is-enabled mclink)"
printf "  %-24s %s\n" "服务当前状态"     "$(systemctl is-active mclink)"
printf "  %-24s %s\n" "失败限制"         "$(systemctl show -p StartLimitIntervalUSec --value mclink)  <- 0 表示永不放弃"
printf "  %-24s %s\n" "看门狗 timer"     "$(systemctl is-enabled mclink-watchdog.timer) / $(systemctl is-active mclink-watchdog.timer)"
printf "  %-24s %s\n" "看门狗下次执行"   "$(systemctl list-timers mclink-watchdog.timer --no-pager | awk 'NR==2{print $1,$2,$3}')"
printf "  %-24s %s\n" "服务启动于"       "$(systemctl show -p ActiveEnterTimestamp --value mclink)"
printf "  %-24s %s\n" "内核启动于"       "$(uptime -s)"
echo
echo "================ 别人的文件确认没被动 ================"
md5sum /opt/mclink/mclink_server.py /opt/mclink/mclink_license.py | sed 's/^/  /'
stat -c '  %y  %n' /opt/mclink/config.server.json /opt/mclink/licenses.json
echo
echo "================ drop-in 目录 ================"
ls -la /etc/systemd/system/mclink.service.d/ | sed 's/^/  /'
echo
echo "================ 端口 ================"
ss -lntu | grep 7000 | sed 's/^/  /'

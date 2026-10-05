# McLink 开机自启（加固）

这个目录是**独立**的，只负责让 `mclink` 服务在服务器重启后可靠地起来。
它**不碰** `install.sh`、`mclink-server.service`、配置和授权数据。

## 背景

`install.sh` 里已经有 `systemctl enable mclink`，所以**开机自启本来就是开着的**。
但实测发现两个缺口：

| 缺口 | 现状 | 后果 |
| --- | --- | --- |
| systemd 会「放弃」 | 默认 `StartLimitBurst=5` / `StartLimitIntervalSec=10s` | 开机时网络/磁盘没就绪、连续失败几次 → systemd **永久放弃**，服务一直躺着不起来 |
| 没有卡死检测 | `Restart=always` 只管「进程退出」 | 进程活着但端口不响应 → 没人管，一直挂着 |

## 做法

- **drop-in**（`10-autostart.conf`）：不改原始 unit，只追加 `StartLimitIntervalSec=0`
- **看门狗**（`mclink-watchdog.timer`）：每 3 分钟检查一次端口是否真的在监听、能不能握手；
  不正常就重启。`systemctl stop mclink` 之后状态是 `inactive`，看门狗会**跳过**，
  不会把你刚停的服务又拉起来

## 安装

```bash
# 把整个 autostart 目录传到服务器
scp -r server/autostart root@<你的服务器>:/root/

# 在服务器上
sudo bash /root/autostart/install-autostart.sh
```

## 卸载

```bash
sudo bash /root/autostart/install-autostart.sh --uninstall
```

卸载后 `mclink` 依然开机自启（那是原始 unit 的 `[Install]` 段管的），
只是少了「永不放弃」和看门狗。

## 自查

```bash
systemctl is-enabled mclink                                    # enabled
systemctl show -p StartLimitIntervalUSec --value mclink        # 0
systemctl list-timers mclink-watchdog.timer                    # 有下次执行时间
journalctl -t mclink-watchdog -n 20                            # 看门狗日志
```

## 实测验证

配置对不对，只有**真重启一次**才算数：

```bash
# 重启前记一下
uptime -s
systemctl is-active mclink

sudo reboot
```

SSH 会断开。等约 1 分钟再连回来，跑：

```bash
uptime -s                                  # 应该是刚才重启的时间（跟重启前不一样）
systemctl is-active mclink                 # active
systemctl show -p StartLimitIntervalUSec --value mclink   # 0
systemctl list-timers mclink-watchdog.timer               # 有下次执行时间
bash /opt/mclink/healthcheck.sh            # 全绿
ss -lntu | grep 7000                       # tcp + udp 都在监听
```

全部通过 = 开机自启真的可靠了。

### 如果重启后服务没起来

```bash
systemctl status mclink -l              # 看失败原因
journalctl -u mclink -b --no-pager | tail -40    # 本次启动的日志
journalctl -t mclink-watchdog -b        # 看门狗有没有动作
```

看门狗最多 3 分钟内会自己把它拉起来，所以先等 3 分钟再判断。

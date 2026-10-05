# McLink 排错手册

排错总原则：**先看客户端日志，再看服务端日志，最后看安全组。**

- 客户端日志：控制台页面底部的日志面板，或 `client/logs/client.log`
- 服务端日志：`journalctl -u mclink -f`

---

## 一、按现象查

### 客户端控制台一直显示"未连接本地代理 (127.0.0.1:8787)"

说明客户端进程没跑起来，或换了端口。

```powershell
# 看进程在不在
Get-Process python, pythonw -ErrorAction SilentlyContinue
# 手动前台跑一次看报错
cd D:\McLink\client
python mclink_client.py
```

常见原因：
- Python 没装好 / 装的时候没勾 `Add to PATH` → `where python` 验证
- `config.client.json` 不是合法 JSON（多了一个逗号、用了中文引号）→ 用
  [JSONLint](https://jsonlint.com/) 校验一下
- 8787 被别的程序占了 → `python mclink_client.py --web-port 9000`

---

### 日志里一直"正在连接服务端…"，然后超时

按顺序排查：

**1）阿里云安全组（最常见，占 90%）**

控制台 → ECS → 安全组 → 入方向，必须有：

| 协议 | 端口 | 授权对象 |
| --- | --- | --- |
| 自定义 TCP | 7000/7000 | 0.0.0.0/0 |
| 自定义 UDP | 7000/7000 | 0.0.0.0/0 |

**2）服务器本机防火墙**

```bash
sudo ufw status
sudo ufw allow 7000/tcp
sudo ufw allow 7000/udp
sudo ufw reload

# Alibaba Cloud Linux / CentOS
sudo firewall-cmd --permanent --add-port=7000/tcp
sudo firewall-cmd --permanent --add-port=7000/udp
sudo firewall-cmd --reload
```

**3）服务端进程在不在**

```bash
systemctl status mclink
journalctl -u mclink -n 50 --no-pager
```

**4）端口到底有没有在监听**

```bash
sudo ss -lntup | grep 7000
```

应该能看到三行。少一行说明服务端启动时就失败了（多半是端口被占）。

**5）从你自己的电脑验证连通性**

```powershell
Test-NetConnection 47.123.45.67 -Port 7000
# TcpTestSucceeded : True 才算通
```

---

### 日志提示"密钥无效，请检查 config.client.json 里的 token"

两边的 token 不一致。核对：

```bash
# 服务器
grep -A3 '"tokens"' /opt/mclink/config.server.json
```

```json
// 客户端 config.client.json
"server": { "token": "这里必须一模一样" }
```

注意：token 是大小写敏感的，前后不要有空格。改完服务器要 `sudo systemctl restart mclink`。

---

### 映射卡片是红色的，提示"公网端口 xxx 监听失败 / 已被占用"

服务端那台机器上已经有人占了这个端口。

```bash
sudo ss -lntup | grep 25565       # TCP
sudo ss -lnup  | grep 19132       # UDP
```

要么换端口，要么把占用的进程停掉。另外注意：**同一个公网端口不能同时被两个客户端
注册**，但一个客户端上 TCP 和 UDP 用同一个端口号是允许的（不冲突）。

---

### 映射卡片提示"端口 xxx 不在服务端允许范围内"

服务端白名单挡下来了。编辑 `/opt/mclink/config.server.json`：

```json
"allowed_ports": [
  [25565, 25570],
  [19132, 19135],
  [7777, 7780],
  [你要的端口, 你要的端口]
]
```

```bash
sudo systemctl restart mclink
```

---

### 朋友连不上，或连上就断

**检查清单，从上往下：**

1. **游戏服务端开着吗** —— 在你电脑上先本地自测：
   ```powershell
   Test-NetConnection 127.0.0.1 -Port 25565     # TCP
   ```
   基岩版用 `netstat -an | findstr 19132` 看有没有 `UDP 0.0.0.0:19132`。

2. **控制台上点"检测本地端口"** —— 红叉就直接说明本地游戏没起来。

3. **公网端口在安全组里放行了吗** —— 每个游戏端口都要单独放行，
   加了新映射就要加新规则。

4. **映射卡片是绿色的吗** —— 灰色/黄色说明没注册成功。

5. **Windows 防火墙** —— 允许你的游戏服务端程序通过防火墙。
   控制面板 → Windows Defender 防火墙 → 允许应用通过防火墙。

6. **公网端口和本地端口填反了吗** —— 控制台卡片上写的是
   `公网 47.x.x.x:25565 → 本地 127.0.0.1:25565`，发给朋友的是**前面那个**。

---

### Java 版能进，基岩版（UDP）进不去

UDP 是单独一条链路，逐条检查：

1. **防火墙放行的是 UDP 7000 吗**（不是 TCP，这是最常见的错误）
2. **安全组放行的是 UDP 19132 吗**（基岩版端口是 UDP，不是 TCP）
3. **服务端 `allowed_ports` 包含 19132 吗**
4. 基岩版服务端 `server.properties` 里的 `server-port=19132` 且
   `server-portv6` 不要和它冲突
5. **控制台看 UDP 映射的"当前连接数"**：
   - 玩家连接时数字变成 1 → 隧道通了，问题在本地游戏服务端
   - 数字始终是 0 → 隧道没通，回看第 1、2 条

控制台显示"本地 UDP 无响应，请确认游戏服务端已启动"，就是第 5 条的后者。

---

### 日志出现"控制通道断开: ConnectionResetError"反复刷

通常是服务器重启了服务，或者中间的 NAT 超时。客户端会自动重连，不用管。
如果**每分钟都断**，检查：

- 服务器内存是否被 OOM Killer 干掉过：`dmesg | tail -30`
- 是否有安全软件在拦长连接

---

## 二、诊断命令速查

```bash
# ---- 服务器端 ----
systemctl status mclink                      # 服务状态
journalctl -u mclink -f                      # 实时日志
journalctl -u mclink --since "10 min ago"    # 最近 10 分钟
sudo ss -lntup | grep mclink                 # 看端口监听
curl -s ifconfig.me                          # 确认公网 IP
python3 /opt/mclink/mclink_server.py -c /opt/mclink/config.server.json --check-config

# ---- 客户端（Windows PowerShell）----
Test-NetConnection 47.123.45.67 -Port 7000   # 控制通道
Test-NetConnection 127.0.0.1 -Port 25565     # 本地游戏
netstat -an | findstr 8787                   # 控制台端口
Get-Content D:\McLink\client\logs\client.log -Tail 50 -Wait
```

调试模式（日志更详细）：
- 服务端：`/opt/mclink/config.server.json` 里 `"log_level": "debug"` 后重启
- 客户端：`config.client.json` 里 `"log_level": "debug"` 后重启

---

## 三、性能与调优

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 卡顿但连接正常 | 服务器上行带宽跑满 | 阿里云按带宽计费，检查实例带宽峰值 |
| 延迟高 | 服务器地域离你和朋友都远 | 选离大家最近的地域（国内选华东/华北） |
| 大量玩家时连接失败 | 文件描述符不够 | `systemctl edit mclink` 里加 `LimitNOFILE=65535` |
| MC 正版验证慢 | 与转发无关 | 检查游戏服务端本身 |

服务器只做转发，**不要在上面跑游戏本体**，1 核 1G 实例足够承接几十个玩家。

---

## 四、重置大法

如果配置被改乱了，回到干净状态：

```bash
# 服务器：恢复默认配置（会丢自定义设置）
sudo cp /opt/mclink/config.server.json.bak /opt/mclink/config.server.json
sudo systemctl restart mclink
```

```powershell
# 客户端：删掉日志和配置里的映射，重新在控制台加
Remove-Item D:\McLink\client\logs\* -Force
```

实在不行，用 `test/e2e_test.py` 在本机跑一遍 —— 它能证明这套代码本身是好的：

```powershell
cd D:\McLink
python test\e2e_test.py
# 结果: 24/24 通过 ⇒ 代码没问题，问题在环境/网络配置
```

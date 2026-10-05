# McLink

> 仓库：<https://github.com/shiyushui/mclink>

用一台有公网 IP 的云服务器，给 Minecraft 之类的游戏做**端口映射 / 内网穿透**。
朋友直接连你的公网地址就能进你的世界，不需要你有公网 IP、不需要路由器做端口转发。

Windows 客户端（Python + tkinter，GUI + 托盘），Linux 服务端（纯标准库 asyncio）。

```
   朋友的游戏客户端                   你的云服务器                 你的电脑
  ┌──────────────┐              ┌──────────────────┐        ┌──────────────┐
  │ 连 YOUR_IP:  │──TCP/UDP───▶ │  McLink 服务端    │◀──────▶│ McLink 客户端 │
  │   25565      │   7000       │  (单个端口 7000)  │  隧道   │  + 游戏服务端 │
  └──────────────┘              └──────────────────┘        └──────────────┘
```

## 特性

- **只占一个端口** —— TCP 7000 同时承载控制通道和 TCP 数据通道，UDP 7000 承载 UDP 隧道；
  防火墙只要放行两条（TCP 7000 / UDP 7000）
- **TLS 加密** —— 控制与数据通道都走 TLS 1.2+，自签证书 + 首次连接记住指纹（TOFU）
- **UDP 也支持** —— 基岩版（19132/UDP）这类游戏可以正常联机，UDP 包带 HMAC 校验
- **授权分发** —— 24 小时一次性邀请码 + 设备绑定，谁在用你一目了然；管理员控制台可停用/解绑/删除
- **客户端自动更新** —— 你在服务端发布新版本，朋友的客户端会弹条提示，点一下就装
- **图形界面 + 系统托盘** —— 端口池一键挑选、映射卡片实时速率、日志面板
- 客户端和服务端**只用 Python 标准库**，没有第三方依赖

## 环境要求

| 端 | 要求 |
| --- | --- |
| 服务端 | Linux（Debian/Ubuntu、Alibaba Cloud Linux、CentOS/RHEL 均可），Python 3.7+，有公网 IP |
| 客户端 | Windows 10/11，Python 3.7+（或使用打包好的 `McLink.exe`） |

## 快速开始

### 1. 部署服务端

把 `server/` 目录传到服务器，然后：

```bash
cd server

# 生成两个密钥（客户端密钥 + 管理员密钥），复制输出备用
python3 -c "import secrets;print('token  :', secrets.token_urlsafe(32));print('admin  :', secrets.token_urlsafe(24))"

# 按模板填配置
cp config.server.example.json config.server.json
vi config.server.json          # 填 tokens[0] 和 admin_token

# 一键安装（建用户、装到 /opt/mclink、注册 systemd、开机自启）
sudo bash install.sh

systemctl status mclink
```

> ⚠️ **`admin_token` 不填你会在授权开启后把自己锁在外面** —— 它同时也是"万能通行证"。

**云平台防火墙**（阿里云安全组等）必须放行：

| 方向 | 协议 | 端口 |
| --- | --- | --- |
| 入方向 | TCP | 7000 |
| 入方向 | TCP | 你要映射的游戏端口（如 25565） |
| 入方向 | UDP | 7000 |
| 入方向 | UDP | 你要映射的游戏端口（如 19132） |

### 2. 部署客户端

把 `client/` 目录放到玩游戏那台 Windows 机器上：

```powershell
cd client
copy config.client.example.json config.client.json
notepad config.client.json      # 填 server.host（你的服务器 IP）和 server.token
```

双击 `McLink.exe`（或 `python mclink_gui.py`）启动。托盘图标右键可显示/隐藏窗口。

> 只有你自己的那台机器才需要在 `config.client.json` 的 `server.admin_token` 里
> 填管理员密钥 —— 填了才能看到「管理员」按钮。**分发给别人的包里这一项必须是空的。**

### 3. 加映射、开始玩

界面里点「＋ 新增映射」→ 选游戏模板（会自动填好名称和端口）→ 保存。
把卡片上的**连接地址**发给朋友，他们直接在游戏里连这个地址就行。

> **关于自动开启**：默认 `client.auto_start_mappings = false` ——
> 打开客户端**不会**自动把映射跑起来，要你点一下卡片上的开关
> （或工具栏的「▶ 启动全部映射」）才开始转发。这样"开软件"不等于"端口对外开着"。
> 想恢复成启动即开启，把配置里这一项改成 `true` 即可。
> 你自己新增/编辑的映射不受影响，保存后立刻生效。

### 4. 有新版时怎么更新

客户端发现新版本会在顶部弹一条提示：

- 顶栏只显示第一行说明，想看全文点 **「详情」** —— 会开一个**可滚动**的窗口，
  说明再长也不会被截断
- 点 **「立即更新」** 才会下载安装；安装要重启客户端，所以什么时候更新由你决定
- 下载中会显示进度条和「已下载 / 总大小」，并提示下载目录
  （客户端目录下的 `update-cache\`）

## 分发给朋友（授权管理）

默认 `license_required: true`，别人必须激活才能用：

1. 你自己的客户端填了 `admin_token` → 界面上出现「管理员」按钮
2. 「邀请码」页 → 输入对方的名字 → 生成一张 24 小时有效、只能用一次的密钥
3. 把 `MCLK-XXXX-XXXX-XXXX` 发给对方，他在客户端点「输入密钥激活」
4. 激活后设备和这个用户名绑定；「用户」页可以随时停用 / 解绑 / 删除
5. 「客户端」页能看谁在用、昵称是什么、从哪个 IP 来、用的哪张邀请码

**发布新版本给朋友们**：改 `client/version.json` 的 `version` / `release`，然后

```powershell
powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Build
```

它会打包 → 上传到服务器 → 写更新清单 → 重启服务端，并打印校验结果。
朋友下次启动客户端会看到「有新版本」提示，点「立即更新」才装（不会偷偷重启掐断他的隧道）。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/USAGE.md](docs/USAGE.md) | 完整部署与使用说明（含游戏端口速查、自动更新详解） |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 按现象排查：连不上、UDP 不通、端口占用… |
| [docs/SELFHOSTING.md](docs/SELFHOSTING.md) | 日常运维、备份、换证书、重置、维修操作 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | 通信协议：帧格式、握手、UDP 隧道、鉴权边界 |

## 开发与自测

客户端和服务端都只用标准库，测试也是：

```bash
cd mc-link-oss
python test/protocol_test.py       # 协议帧与握手
python test/license_test.py        # 邀请码 / 激活 / 设备绑定 / 限流
python test/resilience_test.py     # 断线重连、超时、异常路径
python test/tls_test.py            # TLS 与证书指纹
python test/license_e2e_test.py    # 授权端到端（真起服务端）
python test/e2e_test.py            # 端到端联机
python test/update_e2e_test.py     # 自动更新端到端
python test/admin_smoke_test.py    # 管理员控制台界面
python test/gui_smoke_test.py      # 客户端界面
python test/gui_integration_test.py
```

Windows 上还可以跑 `tools/verify_taskbar_icon.ps1` 检查任务栏图标。

### 当前测试状态

| 套件 | 结果 |
| --- | --- |
| `protocol_test.py` | 16/16 |
| `license_test.py` | 45/45 |
| `resilience_test.py` | 12/12 |
| `tls_test.py` | 17/17（Windows） |
| `gui_smoke_test.py` | 45/45 |
| `gui_integration_test.py` | 18/18 |
| `admin_smoke_test.py` | 113/113 |
| `update_e2e_test.py` | 40/40 |
| `e2e_test.py` | 43/43 |
| `license_e2e_test.py` | 28/28 |
| **合计** | **377 项，全绿** |

`tls_test.py` 的「UDP 收发正常（含首包）」曾经在 Windows 上稳定失败，
排查后确认是**真实缺陷**（不只是测试问题）：客户端收到 `register_ok` 就显示
"已生效"，但 UDP 隧道注册包要等最多 1 秒才发出去，这中间玩家发的包会被服务端丢弃。
修法见 [CHANGELOG 10.2.1](docs/CHANGELOG.md)，现在连跑多次都稳定 18/18。

> 这些测试**不需要**真实服务器，都是本机自起服务端 + 随机端口，可以直接跑。
> 需要 openssl 的用例在缺失时会自动退化（用夹具证书或跳过），不会误报失败。

## 安全设计

- 控制通道与数据通道都走 **TLS 1.2+**，自签证书；客户端首次连接记住指纹，之后强制一致
- 邀请码 48 位随机（`secrets`），24 小时有效、**只能用一次**
- 设备令牌只以 **SHA-256 哈希**存盘，从不保存明文
- 设备指纹 = Windows MachineGuid + 主机名 + MAC，换机器必须由管理员解绑
- 同一个 IP 连续激活失败会**限流**（默认 8 次/分钟）
- 管理台只认 `admin_token`；**分发包里这一项是空的**，且管理入口不会出现在界面上

> ⚠️ `config.server.json`、`config.client.json`、`licenses.json`、TLS 私钥、
> 以及任何 SSH 私钥**都不要提交进仓库**。本仓库的 `.gitignore` 已经排除了它们，
> 仓库里只提供 `*.example.json` 模板。

## 使用须知

- 本工具用于**映射你自己拥有或已获授权的设备**（自己的游戏服务端、自己的开发机等）
- **不要**用它搭建公开代理、绕过网络管制，或访问他人设备
- 请遵守你所在地的法律法规以及云服务商的**服务条款**（部分云厂商禁止开放代理）
- 授权库会记录昵称、IP 与设备指纹哈希用于授权管理和安全审计 —— 如果你要对外提供服务，
  请自行确认符合当地的个人信息保护要求

## 许可证

[MIT](LICENSE)

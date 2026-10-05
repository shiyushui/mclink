# McLink — 用阿里云服务器给 MC 等游戏做端口映射

一套自研的轻量内网穿透 / 端口映射工具。**用你阿里云服务器的公网 IP，把家里的 Minecraft、泰拉瑞亚、饥荒等游戏服务器"搬"到公网上**，朋友直接连 `你的公网IP:端口` 就能进服。

- 🖥️ **桌面软件**：双击 `McLink.exe` 就是一个原生窗口，**不用开浏览器、不用敲命令、没有黑框**；
  关窗口自动缩到系统托盘，隧道在后台继续跑
- 🔐 **全程 TLS 加密**，客户端固定证书指纹防中间人；UDP 用 HMAC 签名，密钥不上网
- 纯 Python 3 标准库，**零第三方依赖**（不用 pip install 任何东西）
- 同时支持 **TCP 和 UDP**，Minecraft Java 版（25565/TCP）和基岩版（19132/UDP）都能用
- 新增映射时直接在**端口池**里挑（或一键自动分配），填范围外的端口当场标红，加新游戏不用再改防火墙
- 家里**不需要公网 IP、不用改路由器**，客户端主动外联，NAT 后面照样用
- 断线自动重连、开机自启、密钥鉴权、端口白名单、实时速率与连接数
- 还附带一个网页控制台（`http://127.0.0.1:8787`），托盘菜单里可以打开，不需要就关掉

---

## 目录

- [它是怎么工作的](#它是怎么工作的)
- [准备工作](#准备工作)
- [第一步：部署服务端（阿里云）](#第一步部署服务端阿里云)
- [第二步：配置云平台防火墙（必做）](#第二步配置云平台防火墙必做)
- [第三步：部署客户端（你的电脑）](#第三步部署客户端你的电脑)
- [第四步：开始联机](#第四步开始联机)
- [常见游戏端口速查](#常见游戏端口速查)
- [让朋友不用输端口（可选）](#让朋友不用输端口可选)
- [分发给朋友（授权管理）](#分发给朋友授权管理)
- [日常运维命令](#日常运维命令)
- [常见问题](#常见问题)
- [文件清单](#文件清单)
- [自测](#自测)

---

## 它是怎么工作的

```
  玩家的电脑                   阿里云服务器 (公网 IP)              你家的电脑 (NAT 后面)
 ┌──────────┐              ┌───────────────────────┐          ┌──────────────────┐
 │ Minecraft│──连 47.x.x.x:25565──▶│  公网端口 25565 监听   │          │  Minecraft 服务端 │
 │  客户端   │              │          │            │          │  127.0.0.1:25565 │
 └──────────┘              │          ▼            │          └────────▲─────────┘
                           │  McLink 服务端 (server)│                   │
                           │   TCP 7000 ◀──────────┼── 客户端主动外联 ──┤
                           │   （控制+数据同端口）    │                   │
                           │   UDP 7000 ◀──────────┼── UDP 隧道保活 ────┤
                           └───────────────────────┘          ┌────────┴─────────┐
                                                              │ McLink 客户端     │
                                                              │ + Web 控制台 8787 │
                                                              └──────────────────┘
```

**McLink 自己只占一个端口号：`7000`**（TCP 承载控制+数据，UDP 承载隧道）。

关键点：**所有连接都由你家电脑主动发起**（先连服务器，服务器再"命令"它回连），
所以你家宽带没有公网 IP、在多层 NAT 后面都没问题。

**为什么游戏端口省不掉**：玩家的 Minecraft 客户端不是 McLink，它只会按朋友输入的那个
`IP:端口` 用游戏自己的协议去连。所以服务器必须在这个端口上真实监听、把流量隧道回家。
只有 MC Java 版能用"协议嗅探"把游戏流量混进 7000（脆弱，不推荐），基岩版/UDP 游戏则做不到。

- **TCP 转发**：玩家连上服务器的 25565 → 服务器通过控制通道通知客户端"回连" →
  客户端**再拨一条 TCP 到同一个 7000 端口**（数据通道复用控制端口，握手时用消息类型分流）
  → 服务器把两条连接对接成一条管道，双向裸转发。
- **UDP 转发**：客户端用一条 UDP socket 定期向服务器的 7000/udp 发保活包（同时把家里的
  NAT 打洞保持住）。玩家发来的包会被加一个 10 字节的小包头（标明是哪个映射、哪个玩家），
  通过这条隧道送到客户端；客户端拆包转发给本地游戏，游戏的回包再原路送回。

---

## 准备工作

| 项目 | 要求 |
| --- | --- |
| 阿里云服务器 | 轻量应用服务器或 ECS 都行，1 核 1G 足够（只做转发，不跑游戏） |
| 服务器系统 | Ubuntu / Debian（`apt`）或 Alibaba Cloud Linux / CentOS / RHEL（`dnf`/`yum`），`install.sh` 会自动识别并准备 Python 3.7+ |
| 你的电脑 | Windows（Linux/macOS 也能用），需要 [Python 3.8+](https://www.python.org/downloads/) |
| 网络 | 服务器要能访问公网；你的电脑要能访问服务器 |

> 装 Python 时**一定要勾选 `Add python.exe to PATH`**，否则双击 `McLink.exe` 会弹窗提示找不到 Python。

---

## 第一步：部署服务端（阿里云）

把 `server` 整个目录传到服务器上（用 [WinSCP](https://winscp.net/)、`scp`、宝塔面板都行）。

> `scp -r server root@你的公网IP:/root/mclink`

然后 SSH 登录服务器，执行：

```bash
cd /root/mclink
sudo bash install.sh
```

脚本会自动完成：检查 Python → 安装到 `/opt/mclink` → 建 systemd 服务 → 设置开机自启 → 放行本机防火墙。
看到 `============ 安装完成 ============` 就成功了。

**安装后确认一下密钥**（`config.server.json` 里的 `tokens`）：

```bash
cat /opt/mclink/config.server.json | grep -A2 tokens
```

> 安装包里已经预置了一个随机密钥，你也可以换成自己的：
> `python3 /opt/mclink/mclink_server.py --gen-token`

---

## 第二步：配置云平台防火墙（必做）

> ⚠️ **这一步不做，客户端会一直"连接超时"，这是新手最容易卡住的地方。**

阿里云两种服务器的入口不一样，对号入座：

<details open>
<summary><b>轻量应用服务器（SWAS）—— 用「防火墙」</b></summary>

登录 [轻量应用服务器控制台](https://swas.console.aliyun.com/) → **服务器** →
点进你的实例 → 上方 **「防火墙」** 标签页 → **添加规则**：

| 应用类型 | 协议 | 端口范围 | 备注 |
| --- | --- | --- | --- |
| 自定义 | TCP | `7000/7000` | McLink（控制 + TCP 数据，一个端口搞定） |
| 自定义 | UDP | `7000/7000` | McLink（UDP 隧道，与上面同号） |
| 自定义 | TCP | `25565/25565` | Minecraft Java 版默认端口 |
| 自定义 | UDP | `19132/19132` | Minecraft 基岩版默认端口 |
| 自定义 | TCP | `25000/26000` | **通用游戏端口池** |
| 自定义 | UDP | `25000/26000` | **通用游戏端口池** |

远程 IP 填 `0.0.0.0/0`（表示任何人可连）。端口范围格式是 `起始/结束`，
单个端口就写 `25565/25565`。

> **这是「放行一次，以后不用再来」的配法。** 加完这 6 条，之后不管装什么新游戏，
> 都从 `25000-26000` 池子里挑一个端口就行，不用再回控制台改防火墙。

#### 关于端口池

McLink 客户端会**从服务端读取允许的端口范围**，在新增映射的窗口里直接列出来：

- 范围小（≤ 24 个）→ 逐个列成可点的小方块，已被占用的显示为划掉
- 范围大 → 显示成一个区块，点一下**自动挑一个空闲端口**填进去
- 手填范围外的数字 → 表单立刻标红「不在服务器允许范围内」，保存都点不了

服务端 `allowed_ports` 是**唯一权威**，客户端只是提前提示。
你也可以改 `/opt/mclink/config.server.json` 里的 `allowed_ports`，
改完 `sudo systemctl restart mclink`；**客户端不用改**，会自动同步新范围。

> ⚠️ **端口池不要和内核临时端口范围重叠。** Linux 用临时端口
> （`cat /proc/sys/net/ipv4/ip_local_port_range`，默认 `32768-60999`）
> 作为出站连接的源端口。池子开进这个区间会偶发 `bind` 失败。25000-26000 是安全的。

#### 想再少几条规则？

只要**游戏端口池**，去掉 25565 / 19132 两条：

| 协议 | 端口范围 |
| --- | --- |
| TCP | `7000/7000` |
| UDP | `7000/7000` |
| TCP | `25000/26000` |
| UDP | `25000/26000` |

代价是玩 Minecraft 时朋友必须输完整端口（`47.1.2.3:25001`）—— 因为 MC 客户端
只在 25565 / 19132 上才会默认不输端口。有域名的可以用
[SRV 记录](#让朋友不用输端口可选) 把端口藏起来。
记得同步把服务端 `allowed_ports` 里的 25565 / 19132 两行删掉。

</details>

<details>
<summary><b>ECS（云服务器）—— 用「安全组」</b></summary>

登录 [ECS 控制台](https://ecs.console.aliyun.com/) → **网络与安全** → **安全组** →
找到实例绑定的安全组 → **配置规则** → **入方向** → **手动添加**：

| 协议 | 端口范围 | 授权对象 |
| --- | --- | --- |
| 自定义 TCP | `7000/7000` | `0.0.0.0/0` |
| 自定义 UDP | `7000/7000` | `0.0.0.0/0` |
| 自定义 TCP | `25565/25565` | `0.0.0.0/0` |

</details>

规则是**跟着你的映射走的**：以后在控制台加了新的公网端口，就要在防火墙里同步放行。

顺便检查一下服务器本机防火墙（`install.sh` 已经尝试自动放行）：

```bash
sudo firewall-cmd --list-all     # Alibaba Cloud Linux / CentOS
sudo ufw status                  # Ubuntu / Debian
```

**怎么判断放行成功了**：在你玩游戏的电脑上开 PowerShell 跑

```powershell
Test-NetConnection 你的公网IP -Port 7000
# TcpTestSucceeded : True 才算通
```

---

## 第三步：部署客户端（你的电脑）

1. 把 `client` 整个目录拷到你玩游戏那台电脑上，比如 `D:\McLink\`。
2. 用记事本打开 `config.client.json`，把 `server.host` 改成**你阿里云服务器的公网 IP**：

   ```json
   "server": {
     "host": "47.123.45.67",       ← 改成你的公网 IP
     "control_port": 7000,
     "data_port": 7000,
     "udp_port": 7000,
     "token": "CHANGE_ME_CLIENT_TOKEN"   ← 必须和服务端一致
   }
   ```

   > `token` 要和服务器 `/opt/mclink/config.server.json` 里 `tokens` 的某一项**完全一致**，
   > 否则服务端会拒绝连接（日志提示"密钥无效"）。
   >
   > `host` 也可以直接在这个桌面程序里改：**设置 → 服务器地址 → 保存并重连**。

3. **双击 `McLink.exe`** —— 桌面版主窗口就打开了，隧道在这个进程里跑，
   **不用开浏览器、不用敲命令、没有黑框**。

   > 第一次用可以先跑 `launcher\create_shortcut.ps1` 在桌面建个快捷方式，
   > 或者干脆把 `McLink.exe` 拖到桌面。

4. 想开机自动在后台跑：主窗口 **设置 → 勾选「开机自动启动」**。
   之后开机就会静默进托盘，不弹窗口。

5. 出问题要排查时，双击 `start_gui_debug.bat`（带控制台，能看到报错）。

### 桌面版长什么样

```
┌────────────────────────────────────────────────────────────────┐
│  ⇄ McLink 端口映射              ● 已连接 YOUR_SERVER_IP · 23ms     │
├────────────────────────────────────────────────────────────────┤
│  ↑上传      ↓下载       ⇅活跃连接      ◷运行时长                │
│  2.0 KB/s   512 B/s    3              1小时2分                  │
│                                                                │
│  [＋ 新增映射]                          共 3 条映射             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ● Minecraft Java 版  [TCP]      [已启用][编辑][删除]      │  │
│  │ YOUR_SERVER_IP:25565  →  127.0.0.1:25565   [复制连接地址]   │  │
│  │ ↓ 2.0 KB/s   ↑ 512 B/s   连接 2 / 累计 17   流量 3.0 KB  │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ▾ 运行日志                        [暂停滚动] [清空]           │
│  19:12:02  已连接服务端 (公网 IP: YOUR_SERVER_IP)                 │
└────────────────────────────────────────────────────────────────┘
```

- **关闭窗口 = 缩到托盘**，隧道继续跑；右键托盘图标可以「显示主窗口 / 打开网页控制台 / 退出」
- 新增映射的窗口里有**游戏模板**和**端口池**（只能选服务器允许的端口，填错了当场标红）
- 网页控制台仍然保留在 `127.0.0.1:8787`，托盘菜单里可以打开，不想要就加 `--no-web`

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-c, --config <路径>` | 指定配置文件 |
| `--hidden` | 启动后直接进托盘（开机自启用的就是它） |
| `--no-web` | 不启动网页控制台，只开桌面窗口 |
| `--debug` | 保留控制台输出，方便看报错 |

也可以不用 exe，直接 `python mclink_gui.py` 启动。

---

## 第四步：开始联机

1. 先在你电脑上**把游戏服务端开起来**（比如 Minecraft 服务端，正常监听 25565）。
2. 打开 McLink 控制台，确认对应映射是**绿色"已连接"**。
3. 在控制台卡片上点复制，把连接地址发给朋友：`47.123.45.67:25565`。
4. 朋友在 Minecraft 里「多人游戏 → 直接连接」填这个地址即可。

控制台上还能看到每条映射的**实时上下行速率、当前连接数、累计流量**，以及实时日志。

---

## 常见游戏端口速查

| 游戏 | 版本/模式 | 协议 | 默认端口 |
| --- | --- | --- | --- |
| Minecraft | Java 版 | TCP | 25565 |
| Minecraft | 基岩版 | UDP | 19132 |
| 泰拉瑞亚 | Terraria | TCP | 7777 |
| 饥荒联机版 | Don't Starve Together | UDP | 10999 |
| 星露谷物语 | Stardew Valley | UDP | 24642 |
| 幻兽帕鲁 | Palworld | UDP | 8211 |
| 我的世界 | 基岩版（备用） | UDP | 19133 |
| CS2 / 求生之路 | Source 引擎 | UDP | 27015 |
| 泰拉瑞亚 | TShock | TCP | 7777 |

服务端 `config.server.json` 的 `allowed_ports` 是**白名单**，默认开放了
`25565-25570`、`19132-19135`、`7777-7780`、`10999-11000`、`27015-27016`。
要用别的端口，先改这个白名单再 `sudo systemctl restart mclink`。

---

## 让朋友不用输端口（可选）

只有 Minecraft Java 版支持：给域名加一条 **SRV 记录**，朋友就能直接输域名联机，不用写端口。

前提：你得有个域名，并把它解析到服务器公网 IP（一条 A 记录）。

然后在 DNS 服务商添加：

| 类型 | 主机记录 | 记录值 |
| --- | --- | --- |
| A | `mc` | `47.123.45.67` |
| SRV | `_minecraft._tcp.mc` | 优先级 `0` 权重 `5` 端口 `25565` 目标 `mc.你的域名.com` |

之后朋友在 Minecraft 里填 `mc.你的域名.com` 就能进服。

---

## 分发给朋友（授权管理）

想把这个软件发给朋友用、又不希望谁拿到都能用，就靠这一套。核心是三样东西：

| 名称 | 谁知道 | 作用 |
| --- | --- | --- |
| **管理员密钥** | 只有你 | 在客户端输入后解锁「管理员模式」，可以生成密钥、管用户 |
| **一次性邀请码** | 你 → 某个朋友 | 形如 `MCLK-7F3A-9B2C-4D1E`，24 小时内有效、**只能用一次** |
| **设备令牌** | 服务端 + 那台电脑 | 激活后自动发放，之后每次连接自动校验，长期有效 |

### 开启步骤

> ℹ️ **新版默认就是开着的**（`config.server.json` 里 `license_required: true`）。
> 2026-10-04 之前它默认是 `false`，等于**谁拿到客户端都不用密钥就能用** ——
> 那是个 bug，已经修掉了。下面这些是万一它被关掉时怎么开回来。

1. **填管理员密钥**。服务端 `config.server.json` 里已经有 `admin_token` 字段
   （部署脚本生成过一个随机值）。想换一个就自己生成：
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **打开授权校验**。确认 `license_required` 是 `true`，然后重启服务端：
   ```bash
   grep '"license_required"' /opt/mclink/config.server.json   # 应该是 true
   sudo systemctl restart mclink
   journalctl -u mclink --since "30 sec ago" | grep "授权校验"  # 应该写"已开启"
   ```
   > ⚠️ **打开之前务必确认 `admin_token` 已经填好**，否则你连自己都进不去。
   > 服务端启动时如果发现没开启会打 `WARN`。

   > **别再改回 `false` 了**：那等于把客户端白送出去。
   > 真要临时关（排查问题），管理员接口现在要显式确认，服务端也会打 `WARN`。

3. **你自己的客户端**在 `config.client.json` 里填上同一个管理员密钥：
   ```json
   "server": { "admin_token": "你的管理员密钥" }
   ```
   也可以在客户端里点「管理员 → 输入管理员密钥」，填一次就记住了。
   填了它你就是管理员，**永远不需要激活**，不会被授权校验拦住。

### 你自己的使用流程

1. 打开 McLink → 点顶部 **「管理员」** → 输入管理员密钥 → 进入管理员模式
2. 切到 **「邀请码」** 标签 → 填用户名（比如 `alice`）、有效期（默认 24 小时）、备注 → **生成邀请码**
3. 把生成的那串 `MCLK-XXXX-XXXX-XXXX` 发给朋友
4. 切到 **「客户端」** 标签看谁在用：他的**昵称**、用户名、**用哪张邀请码激活的**、
   来源 IP、客户端版本、在线状态
5. 切到 **「用户」** 标签可以停用 / 解绑 / 删除；**「更新」** 标签看自动更新的状态

### 朋友的首次使用

1. 解压你发过去的文件夹，双击 `McLink.exe`
2. 主窗口顶部会出现橙色提示条「未授权 · 尚未激活」，点 **「输入密钥激活」**
3. 粘贴你给的密钥，**顺手把自己的昵称填上**（默认是机器名，改了你在管理台就看得见是谁）
   → 激活 → 提示「激活成功，欢迎 alice」
4. 之后这台电脑就一直能用了，开机自启也不用再输

> 昵称随时可以在 **设置 → 我的昵称** 里改，保存立刻生效，管理台马上就更新。
> 它只是个显示名，不参与鉴权 —— 谁能用仍然由"邀请码 + 机器指纹"决定。

### 别让朋友手动换包：客户端自动更新

客户端连上服务端之后会自动问一句"有没有新版本"，有就在顶部弹一条蓝条。
**朋友点「立即更新」才会下载安装**（安装要退出重启，会掐断正在玩的隧道，
所以什么时候装交给用户决定）。整个过程走原来的 7000/TCP 加密通道，
**不需要新开端口、不用改防火墙**。

发布一个新版本，三条命令：

```powershell
cd C:\Users\<你的用户名>\Desktop\mc-link
# 1) 改 client\version.json 里的 version 和 release（批次号）
# 2) 打包 + 上传 + 写清单 + 重启服务端
powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Build
```

之后你电脑上「设置 → 检查更新」就能立刻看到；朋友那边下次启动客户端会看到提示条。
详见 [自动更新与发布](SELFHOSTING.md#8-客户端自动更新)。

### 你能控制什么

| 操作 | 效果 |
| --- | --- |
| **停用** 某个用户名 | 那台电脑**下一次连接**就会被拒绝（正在跑的隧道会在重连时断开） |
| **解绑设备** | 清掉绑定，允许用新密钥重新激活（换电脑、重装系统时用） |
| **删除用户** | 彻底移除记录 |
| **撤销邀请码** | 已发出但还没用的密钥立刻作废 |
| **预先停用** | 对一个还没激活的用户名直接点停用，等于拉黑——那张邀请码就废了 |
| **下发客户端更新** | `tools\publish_update.ps1` 发一个新版本，朋友那边会看到提示条 |

### 安全设计

- 邀请码用 `secrets` 生成（48 bit 熵），**只能用一次、24 小时过期**
- 设备令牌在服务端**只存 SHA-256 哈希**，`licenses.json` 泄露也拿不到能用的令牌
- 所有密钥比较用 `hmac.compare_digest`，防时序侧信道
- 授权文件权限 `0600`，原子写入（先写 `.tmp` 再 replace，断电不会写坏）
- 令牌**绑定设备指纹**（Windows MachineGuid + 机器名 + MAC），把配置整个拷到别的电脑上不认
- 激活失败**按 IP 限流**（每分钟 8 次），防止暴力猜码；管理员密钥猜错同样限流
- 未授权时服务端**拒绝一切端口注册** —— 这是最后一道闸，就算客户端被改也开不了公网端口

### 传输加密（TLS）

**控制通道和数据通道现在全程走 TLS 1.2+**，密钥、邀请码、游戏流量都不再明文上网。

服务端第一次启动时会用 `openssl` 自动签一张 10 年期的自签证书：

```
[INFO ] 已生成自签证书：/opt/mclink/server.crt
[INFO ]   传输加密  TLS 已开启（TLS 1.2+，密钥与邀请码不再明文上网）
[INFO ]   证书指纹  39:4B:5D:CE:55:B2:65:01:E1:3C:84:C4:0A:1B:EA:11:...
```

客户端**第一次连接会自动记住这串指纹**（写进 `config.client.json` 的 `cert_fingerprint`），
之后每次连接都必须一致 —— 中间人即使截获流量也换不了证书，一换就报错：

```
⚠ 服务器证书指纹和上次不一致！可能是服务器换了证书，
   也可能是有人在中间人劫持。确认是你自己换的证书的话，
   把 config.client.json 里的 cert_fingerprint 清空再连。
```

想更严格（不给 TOFU 留任何窗口），可以手动把服务端打印的指纹填进客户端配置：

```json
"server": {
  "tls": true,
  "cert_fingerprint": ""
}
```

也可以用命令拿：

```bash
sudo /usr/bin/python3.8 /opt/mclink/mclink_server.py -c /opt/mclink/config.server.json --cert-fingerprint
```

**UDP 隧道没法用 TLS**（UDP 是无连接的），所以改用 **HMAC 签名**：

- 保活/注册包带上时间戳 + HMAC，**主密钥不再出现在网络上**，还能防重放
- 每个数据包带 8 字节截断 MAC，**伪造的包会被直接丢弃**，进不了你的本地游戏

> 服务端会自动去 PATH 和常见安装目录找 `openssl`。
> Linux 一般自带；Windows 上装了 Git for Windows 也会被找到。
> 万一真找不到，服务端会**打警告并退回明文模式**（不会启动失败），
> 你也可以自己准备一对证书，在配置里填 `tls_cert` / `tls_key` 的路径。
> 换了证书记得把客户端配置里的 `cert_fingerprint` 清空，否则会连不上（这是故意设计的）。

### 关掉授权

别关。`license_required` 改成 `false` 就等于**谁拿到客户端都不用密钥就能用**，
2026-10-04 之前线上就是这个状态，被当成 bug 修掉了。

真要临时关（排查问题用），注意两点：

- `config.server.json` 的 `license_required` **只影响**"首次启动、还没有
  `licenses.json`"的情况；文件已经存在时，真正生效的是
  `licenses.json` 里的 `settings.required` —— 两边都要改。
- 从管理员接口关（`set_required`）现在需要显式确认，服务端会打 `WARN` 提醒你。

```bash
sudo systemctl stop mclink
sudo /usr/bin/python3.8 - <<'EOF'
import json, os
cfg = '/opt/mclink/config.server.json'
if os.path.exists(cfg):
    d = json.load(open(cfg, encoding='utf-8'))
    d['license_required'] = False
    json.dump(d, open(cfg, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('关闭 config.server.json:license_required')

lic = '/opt/mclink/licenses.json'
if os.path.exists(lic):
    d = json.load(open(lic, encoding='utf-8'))
    d.setdefault('settings', {})['required'] = False
    json.dump(d, open(lic, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('关闭 licenses.json:settings.required')
EOF
sudo chown mclink:mclink /opt/mclink/config.server.json /opt/mclink/licenses.json
sudo systemctl start mclink
```

授权数据会留在 `/opt/mclink/licenses.json`，关掉不会丢。排查完请务必开回来。

---

## 日常运维命令

**服务器端**

```bash
systemctl status mclink          # 看状态
journalctl -u mclink -f          # 实时看日志（推荐）
systemctl restart mclink         # 改完配置后重启
systemctl stop mclink            # 停止
nano /opt/mclink/config.server.json   # 改配置
```

**客户端**

| 操作 | 做法 |
| --- | --- |
| 启动桌面版 | 双击 `McLink.exe`（或桌面快捷方式） |
| 带日志排查 | 双击 `start_gui_debug.bat` |
| 开机自启 | 桌面版 **设置 → 勾选「开机自动启动」** |
| 关窗口但保持运行 | 直接关闭窗口 = 缩到托盘；退出请右键托盘图标 → 退出 |
| 建桌面快捷方式 | `powershell -ExecutionPolicy Bypass -File launcher\create_shortcut.ps1` |
| 重新编译 exe | `powershell -ExecutionPolicy Bypass -File launcher\build_launcher.ps1` |
| 换网页控制台端口 | 设置里改，或 `python mclink_gui.py --no-web` 干脆关掉 |

> 老版本那套「后台进程 + 浏览器控制台」还留着：`start.bat` 启动纯后台客户端，
> `install_autostart.ps1` 用计划任务做自启。现在推荐用桌面版，不用管这些。

---

## 常见问题

**Q：客户端日志一直"连接超时"，控制台显示"未连接"**
1. 云平台**防火墙/安全组**没放行 TCP 7000（最常见，占 90%）。
2. 服务器本机防火墙没放行 → `sudo ufw allow 7000/tcp && sudo ufw allow 7000/udp`。
3. `config.client.json` 里的 `host` 填错了，或者填成了内网 IP。
4. 服务端没跑起来 → 服务器上 `systemctl status mclink` 看一眼。

**Q：控制台提示"密钥无效"**
客户端 `server.token` 和服务端 `tokens` 里的值不一致。改完重启两边。

**Q：映射显示红色"公网端口被占用"**
服务器上已经有别的程序（或你自己的另一个映射）占着这个端口。换一个端口，
或者 `sudo ss -lntup | grep 25565` 查一下是谁占了。

**Q：映射显示"端口 xxx 不在服务端允许范围内"**
服务端 `allowed_ports` 白名单没包含这个端口。编辑 `/opt/mclink/config.server.json` 加进去，
然后 `sudo systemctl restart mclink`。

**Q：朋友能连上但是进不去 / 提示连接被重置**
- 你电脑上的**游戏服务端没开**，或者监听的不是 `127.0.0.1:25565`。
  在控制台点一下"检测本地端口"能直接告诉你。
- Windows 防火墙拦了游戏程序，允许它通过专用网络即可。

**Q：基岩版（UDP）连不上，Java 版（TCP）正常**
- 防火墙要放行 **UDP 7000**（不是 TCP），这条最容易漏。
- 服务端 `allowed_ports` 要包含 19132。
- 基岩版客户端显示的是"无法连接"或一直"正在连接"，用控制台看 UDP 映射的"当前连接数"有没有涨：
  涨了说明隧道通了，问题在本地游戏服务端。

**Q：控制台显示"本地 UDP 无响应，请确认游戏服务端已启动"**
基岩版服务端（bedrock_server.exe）没启动，或者监听端口不是 19132。

**Q：会不会有安全风险？**
- 控制通道必须提供正确密钥才能注册端口，端口必须在服务端白名单内。
- 控制台默认**只监听 `127.0.0.1`**，外网打不开。若改成 `0.0.0.0` 让局域网访问，
  务必在 `config.client.json` 的 `web.token` 里设置一个口令。
- 想更严格：白名单只留你真正要用的端口，`tokens` 用 `--gen-token` 重新生成。

更多排查细节见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)，
协议细节见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

---

## 文件清单

```
mc-link-oss/
├── README.md                      项目说明（本仓库入口）
├── LICENSE                        MIT
├── .gitignore                     已排除配置/密钥/日志/构建产物
├── server/                        ← 传到 Linux 服务器
│   ├── mclink_server.py           服务端主程序
│   ├── mclink_license.py          授权管理（邀请码 / 设备令牌 / 用户权限）
│   ├── config.server.example.json 服务端配置模板（复制成 config.server.json）
│   ├── install.sh                 一键安装（systemd + 自启 + 自动装 Python）
│   ├── healthcheck.sh             体检脚本（服务/端口/证书/授权/日志/资源）
│   ├── mclink-server.service      systemd 服务文件
│   └── autostart/                 看门狗与自启（可选）
├── client/                        ← 拷到玩游戏那台 Windows
│   ├── mclink_gui.py              桌面版界面（tkinter，零依赖）
│   ├── mclink_client.py           隧道内核 + 本地 HTTP API + 授权
│   ├── mclink_admin.py            管理员控制台（邀请码/用户/客户端/更新）
│   ├── mclink_tray.py             系统托盘（纯 ctypes）
│   ├── mclink_winicon.py          Win32 窗口/任务栏图标 + 开始菜单快捷方式
│   ├── mclink_icon.py             备用图标生成器
│   ├── update_helper.py           自动更新助手（退出后替换文件并重启）
│   ├── version.json               版本号与批次号
│   ├── config.client.example.json 客户端配置模板（复制成 config.client.json）
│   ├── start_gui_debug.bat        带控制台启动，排查用
│   ├── web/index.html             本地 Web 控制台界面
│   ├── assets/                    图标资源
│   └── launcher/                  启动器与打包脚本（C# 启动器、make_dist.ps1…）
├── docs/
│   ├── USAGE.md                   部署与使用（本文件）
│   ├── SELFHOSTING.md             自建与运维（日常维护、应急恢复、升级）
│   ├── TROUBLESHOOTING.md         按现象排错
│   ├── PROTOCOL.md                通信协议说明
│   └── CHANGELOG.md               变更记录（含踩过的坑）
├── tools/
│   ├── make_release.ps1           打完整包 + 客户端分发包
│   ├── publish_update.ps1         ★ 发布客户端更新到服务器
│   └── verify_taskbar_icon.ps1    排查任务栏图标
└── test/                          本机自起服务端，不需要真实服务器
    ├── protocol_test.py           协议帧与握手（16 项）
    ├── license_test.py            邀请码 / 激活 / 设备绑定 / 限流（45 项）
    ├── resilience_test.py         断线重连 / 超时 / 异常路径（12 项）
    ├── tls_test.py                TLS 与证书指纹 / 中间人拦截（17 项）
    ├── license_e2e_test.py        授权端到端
    ├── e2e_test.py                端到端联机
    ├── update_e2e_test.py         昵称 / 客户端列表 / 自动更新（40 项）
    ├── admin_smoke_test.py        管理员控制台界面（113 项）
    ├── gui_smoke_test.py          客户端界面（45 项）
    ├── gui_integration_test.py    客户端集成（18 项）
    └── fixtures/                  一次性测试证书（TEST-ONLY，见同目录 README）
    └── ui_pool_test.js            控制台端口池逻辑测试（12 项）
```

---

## 自测

十套自动化测试，覆盖功能、容灾、协议一致性、传输安全、授权、界面和自动更新：

```bash
python test/e2e_test.py             # 功能：TCP 并发、UDP 首包、全部 API、SSE、鉴权、CORS、端口池 —— 38 项
python test/resilience_test.py      # 容灾：游戏没开/重启、服务端重启、客户端重启 —— 12 项
python test/protocol_test.py        # 协议：两端包头字节级一致、改一个字节就被发现、密钥不上网 —— 16 项
python test/tls_test.py             # 传输安全：TLS 握手、指纹固定、中间人拦截、UDP 伪造防护 —— 17 项
python test/license_test.py         # 授权模块：邀请码、激活、过期、设备绑定、限流 —— 32 项
python test/license_e2e_test.py     # 授权全流程：未授权拦截→管理员→生成→激活→停用 —— 28 项
python test/update_e2e_test.py      # 昵称、客户端列表、自动更新、管理员回归 —— 31 项
python test/gui_smoke_test.py       # 桌面版：图标/托盘/主窗口/映射卡片/对话框/端口池 —— 45 项
python test/gui_integration_test.py # 桌面版集成：管理员接线、激活弹窗、授权提示条 —— 18 项
python test/admin_smoke_test.py     # 管理员控制台：表单校验、表格、权限、错误处理 —— 113 项
node test/ui_pool_test.js client/web/index.html   # 控制台端口池逻辑 —— 12 项
```

共 **377 项**断言，全绿。部署前先跑一遍，全过说明代码和环境都没问题，
剩下的问题一定在防火墙或网络配置上。

---

## 关于公网 IP 的自动探测

服务端启动时会自动获取公网 IP（用于在控制台上显示"朋友连接地址"），顺序是：

1. **阿里云元数据服务** —— 如果你的 ECS 绑的是**弹性公网 IP（EIP）**，网卡上只有内网地址，
   只有元数据服务能拿到真正的 EIP，所以这一步很关键。
2. **本机出口 IP** —— 适合公网 IP 直接绑在网卡上的情况。
3. 都拿不到就**不猜**，客户端改用 `config.client.json` 里你填的 `server.host` 作为连接地址。

如果控制台显示的公网 IP 不对，直接在 `config.server.json` 里手动填：

```json
"public_ip": "47.123.45.67",
```

然后 `sudo systemctl restart mclink`。

---

## 性能说明

转发本身几乎不消耗 CPU —— 数据只是在内核 socket 缓冲区之间搬。
阿里云服务器只承担带宽：一台 1Mbps 小水管跑 MC 联机（几个人）完全够；
基岩版和 FPS 类游戏对带宽更敏感，按人数预留即可。
服务器只做转发，**不运行游戏本体**，所以 1 核 1G 的最低配实例足矣。

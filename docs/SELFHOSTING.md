# McLink 自建与运维手册

> 这份手册是给**自己搭这个服务的人**看的。新装的话先看 [USAGE.md](USAGE.md)。
> 下面用 `YOUR_SERVER_IP` 之类的占位符，替换成你自己的值即可。
>
> 遇到问题的第一件事：**跑一次体检**。
>
> ```bash
> ssh root@YOUR_SERVER_IP
> bash /opt/mclink/healthcheck.sh
> ```
>
> 它会逐项检查服务、端口、证书、授权、日志、资源、防火墙，并明确标出 `✗` 的问题。

---

## 目录

- [0. 30 秒速查](#0-30-秒速查)
- [1. 部署档案（自己填）](#1-部署档案自己填)
- [2. 日常维护](#2-日常维护)
- [3. 故障排查（按现象）](#3-故障排查按现象)
- [4. 维修操作](#4-维修操作)
- [5. 应急恢复](#5-应急恢复)
- [6. 安全清单](#6-安全清单)
- [7. 升级流程](#7-升级流程)
- [8. 客户端自动更新](#8-客户端自动更新)
- [9. 昵称与客户端列表](#9-昵称与客户端列表)
- [变更记录](CHANGELOG.md)（历史改动与踩过的坑）

---

## 0. 30 秒速查

先对号入座，再跳章节。

| 现象 | 最可能的原因 | 去哪节 |
| --- | --- | --- |
| 客户端一直"正在连接…"然后超时 | **阿里云防火墙没放行 TCP 7000** | [3.1](#31-客户端连不上服务端) |
| 客户端连上了，但朋友连不进游戏 | **游戏端口没放行**（25565 等） | [3.2](#32-朋友连不进游戏) |
| 映射卡片是红色的 | 家里游戏服务端没开 / 本地端口不对 | [3.3](#33-映射卡片出错) |
| 卡片写"客户端未授权" | 没激活，或授权校验关着（不该关） | [3.6](#36-授权相关) |
| 基岩版（UDP）连不上，Java 版正常 | **UDP 7000 或 UDP 游戏端口没放行** | [3.4](#34-只有-udp-游戏连不上) |
| 提示"证书指纹和上次不一致" | 服务器换了证书 | [3.5](#35-证书指纹不匹配) |
| 提示"客户端未授权" | 没激活 / 被停用 / 换了电脑 | [3.6](#36-授权相关) |
| 任务栏上显示成 Python 图标 | 窗口图标没设上（旧版本 bug） | [3.9](#39-任务栏图标显示成-python) |
| 朋友那边看不到"有新版本"提示 | 更新包没发布 / 版本号没升 | [8.1](#81-发布一个新版本) |
| 点了「立即更新」没反应 / 卡住 | 看 `client\update-cache\update.log` | [8.3](#83-更新失败怎么查) |
| 更新说明显示不全（被截断） | 顶栏只放第一行；点「详情」看全文 | [8.4](#84-更新说明看全文) |
| **打开客户端不自动开映射了** | 1.2.6 起默认要手动启动（这是有意的） | [3.10](#310-映射不会自动开启) |
| **基岩版刚进去丢包 / 要重连一次** | 1.2.6 已修（UDP 首包窗口） | [3.11](#311-udp-刚生效那一瞬间丢包) |
| 管理台看不到朋友的昵称 | 昵称要激活时填，或服务端没记上 | [9.2](#92-管理台看到的是什么) |
| 删了用户，他的邀请码还在列表里 | 老版本只删了用户没删码 | [10.1.2](#1012-2026-10-05-补丁删除用户后邀请码还留着) |
| 双击 McLink.exe 没反应 / 开了好几个 | 已有实例在跑（缩在托盘里） | [3.7](#37-桌面版打不开或重复启动) |
| 网页控制台打不开 | 端口被占（会自动顺延到 8788） | [3.8](#38-网页控制台打不开) |
| 服务端起不来 | 配置写坏 / 端口被占 / Python 被删 | [5.2](#52-服务端起不来) |
| 我把自己锁在外面了 | 管理员密钥填错/为空 | [5.1](#51-把自己锁在外面了) |

---

## 1. 部署档案（自己填）

**建议把这一节抄一份存到别处**（密码管理器、手机备忘录）。服务器重装后靠它恢复。

### 服务器

| 项目 | 值 |
| --- | --- |
| 公网 IP | `________________` |
| 云平台 / 地域 | `________________` |
| 系统 | `________________`（决定用 `apt` 还是 `dnf`） |
| 登录方式 | `ssh root@<公网IP>`，或云控制台远程连接 |
| 安装目录 | `/opt/mclink` |
| 客户端密钥 | 存在服务器 `config.server.json` 的 `tokens`；客户端填同一个 |
| 管理员密钥 | 存在 `admin_token`；**只放你自己的机器**，别外发 |
| 授权库 | `/opt/mclink/licenses.json`（权限 600） |
| 更新包 | `/opt/mclink/updates/` |

### 客户端

| 项目 | 值 |
| --- | --- |
| 安装位置 | `________________`（如 `D:\McLink`） |
| 配置文件 | 同目录的 `config.client.json` |
| 更新缓存 | 同目录的 `update-cache\`（含 `update.log`） |

### 速查

```bash
# 服务端状态 / 日志
systemctl status mclink
journalctl -u mclink -n 50 --no-pager

# 跑一次体检（部署后、出问题时都建议跑）
bash /opt/mclink/healthcheck.sh

# 客户端（Windows）
Get-Content <客户端目录>\logs\client.log -Tail 50
```

---

## 2. 日常维护

### 2.1 每天/每周看一眼

```bash
bash /opt/mclink/healthcheck.sh          # 一键体检（只读，放心跑）
systemctl status mclink                  # 服务状态
journalctl -u mclink -f                  # 实时日志（Ctrl+C 退出）
journalctl -u mclink --since "1 day ago" | grep -E "ERROR|WARN"   # 只看异常
```

### 2.2 常用命令

```bash
systemctl restart mclink     # 重启
systemctl stop mclink        # 停止
systemctl start mclink       # 启动
systemctl status mclink      # 状态
```

### 2.2.1 客户端这边的常用操作（Windows）

主界面工具栏：

| 按钮 | 用途 |
| --- | --- |
| **＋ 新增映射** | 加一条端口映射（保存后立刻生效，不受自动开关限制） |
| **▶ 启动全部映射** | 一次性把所有映射开起来（1.2.6 起默认不自动开，见 [3.10](#310-映射不会自动开启)） |
| **■ 全部停止** | 一次性全部停掉（会二次确认） |
| 卡片上的开关 | 单条开/关 |
| 顶栏 **详情** | 看完整的新版本说明（见 [8.4](#84-更新说明看全文)） |

**设置** 窗口里能改：昵称、服务器地址/端口、密钥、开机自启、自动检查更新；
窗口中间的内容区**可以滚动**（设置项多，一屏放不下），右下角还有「关于/鸣谢」。

### 2.3 备份（建议每月一次）

```bash
mkdir -p /root/mclink-backup
cd /opt/mclink
tar czf /root/mclink-backup/mclink-$(date +%Y%m%d).tar.gz \
    config.server.json licenses.json server.crt server.key
ls -lh /root/mclink-backup/
```

下载到本地保存：

```powershell
# 在你电脑上跑
scp -i ~/.ssh/id_ed25519 root@YOUR_SERVER_IP:/root/mclink-backup/*.tar.gz .
```

**恢复**就是把 tar 解回 `/opt/mclink/`，然后 `chown -R mclink:mclink /opt/mclink && systemctl restart mclink`。

### 2.4 检查证书有效期

证书签了 10 年，基本不用管。但体检脚本会检查，快到期会 `!` 提醒。

### 2.5 授权库大了怎么办

`licenses.json` 里会积累已使用的邀请码记录（用于审计）。超过几百个可以清理：

```bash
# 看看有多少
python3.8 -c "import json;d=json.load(open('/opt/mclink/licenses.json'));print('邀请码',len(d['invites']),'用户',len(d['users']))"
```

清理过期的（保留已使用的）：

```bash
systemctl stop mclink
python3.8 - <<'EOF'
import json, time
p = '/opt/mclink/licenses.json'
d = json.load(open(p, encoding='utf-8'))
now = time.time()
before = len(d['invites'])
d['invites'] = {k: v for k, v in d['invites'].items()
                if v.get('used_at') or v.get('expires_at', 0) > now - 86400}
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'邀请码 {before} -> {len(d["invites"])}')
EOF
chown mclink:mclink /opt/mclink/licenses.json && chmod 600 /opt/mclink/licenses.json
systemctl start mclink
```

---

## 3. 故障排查（按现象）

### 3.1 客户端连不上服务端

症状：日志一直"正在连接服务端 …"，最后"连接超时"。

**按顺序查**（90% 是第 1 条）：

1. **阿里云控制台防火墙有没有放行 TCP 7000**
   轻量应用服务器控制台 → 点实例 → 「防火墙」→ 看有没有 `7000` 这一条。
   没有就加：应用类型「自定义」、协议 `TCP`、端口范围 `7000/7000`。

2. **服务端在不在跑**
   ```bash
   systemctl is-active mclink
   ```

3. **服务器上端口有没有在监听**
   ```bash
   ss -lntu | grep 7000        # 应该看到 tcp 和 udp 各一行
   ```

4. **从你电脑验证连通性**
   ```powershell
   Test-NetConnection YOUR_SERVER_IP -Port 7000
   # TcpTestSucceeded : True 才算通
   ```

5. **配置里的地址对不对**
   `client\config.client.json` 的 `server.host` 要是 `YOUR_SERVER_IP`。

### 3.2 朋友连不进游戏

**先确认"你自己的客户端"是绿的**，再看这几条：

1. **游戏端口有没有在阿里云防火墙放行**
   比如 `TCP 25565`。**每个游戏都要单独加一条。**
   想一劳永逸就加 `TCP 25000/26000` + `UDP 25000/26000`，以后从池子里挑。

2. **公网端口和本地端口有没有填反**
   发给朋友的是卡片上写的那个：`YOUR_SERVER_IP:25565`。

3. **游戏服务端开了吗**
   ```powershell
   Test-NetConnection 127.0.0.1 -Port 25565
   ```
   或者点客户端卡片上的「检测本地端口」。

4. **本地端口填对了没**
   MC 服务端 `server.properties` 里的 `server-port` 要和映射的本地端口一致。

5. **Windows 防火墙**有没有拦游戏程序。

### 3.3 映射卡片出错

| 卡片上的字 | 意思 | 怎么办 |
| --- | --- | --- |
| 无法连接本地游戏端口 | 家里游戏没开 / 端口填错 | 把游戏服务端开起来 |
| 公网端口 xxx 监听失败 / 被占用 | 服务器上这个端口被别的程序占了 | 换个端口，或 `ss -lntup \| grep 端口号` 查是谁 |
| 端口 xxx 不在服务端允许范围内 | 超出了 `allowed_ports` 白名单 | 改 `/opt/mclink/config.server.json` 的 `allowed_ports` 后重启 |
| 客户端未授权 | 授权问题 | 见 [3.6](#36-授权相关) |
| 与服务端断开 | 网络抖动，会自动重连 | 等一会儿；超过 1 分钟看 [3.1](#31-客户端连不上服务端) |

### 3.4 只有 UDP 游戏连不上

UDP 是独立的一条链路，逐条查：

1. **阿里云防火墙放行的是 UDP 7000 吗**（不是 TCP！这是最常见的错误）
2. **游戏端口放行的是 UDP 吗**（基岩版 19132 是 UDP，不是 TCP）
3. **`allowed_ports` 里包含这个端口吗**
4. 看客户端控制台 UDP 映射的「当前连接数」：
   - 玩家连的时候数字变 1 → 隧道通了，问题在本地游戏服务端
   - 一直 0 → 隧道没通，回看第 1、2 条
5. 换过宽带/路由器后连不上？重启一下客户端（重建 NAT 打洞）

### 3.5 证书指纹不匹配

客户端报：

```
服务器证书指纹和上次不一致！可能是服务器换了证书，也可能是有人在中间人劫持。
```

**如果确实是你自己换了证书**（比如重装了服务端）：

删掉客户端配置里记的旧指纹，让它重新信任：

```powershell
# 编辑 client\config.client.json，把这行改成空字符串
"cert_fingerprint": ""
```

或者在客户端 **设置 → 保存并重连** 前先手工清掉。

**如果不是你换的** —— 那就是真的有人在中间人，**别清指纹**，先查网络。

换证书后核对新指纹：

```bash
# 服务器上
openssl x509 -in /opt/mclink/server.crt -noout -fingerprint -sha256
```

### 3.6 授权相关

| 提示 | 原因 | 怎么办 |
| --- | --- | --- |
| 客户端未授权：尚未激活 | 没激活过 | 要一个邀请码，点「输入密钥激活」 |
| 客户端未授权：请先在 McLink 里用一次性密钥激活 | 服务端校验开着，这台机器没激活 | 同上；这是**正常拦截**，不是故障 |
| 密钥不存在 | 输错了 / 被撤销了 | 核对大小写（其实不区分），重新要一个 |
| 密钥已过期 | 超过 24 小时 | 重新要一个 |
| 这个密钥已经被使用过了 | 一码只能用一次 | 重新要一个 |
| 用户名 xxx 已被停用 | 被管理员停用了 | 在管理员控制台里改成"正常" |
| 已经绑定过设备了 | 一个用户名只能绑一台 | 管理员控制台点「解绑设备」 |
| 设备不匹配（授权被复制到其他电脑了） | 换了电脑 / 改了机器信息 | 管理员控制台解绑，重新激活 |
| 尝试次数过多 | 连续输错触发了限流 | 等 1 分钟 |

> ✅ **授权校验现在必须是开着的**（`license_required: true`）。
> 关掉它（`false`）= 谁拿到客户端都能用、不需要任何密钥 ——
> 2026-10-04 之前的部署就一直是 `false`，被当成 bug 修掉了，**别再改回去**。
>
> 服务端启动日志里会写清楚：
> `授权校验  已开启（N 个用户，M 个待用邀请码）` 才是对的；
> 如果看到 `未开启 —— 谁拿到客户端都能用`，那是个 `WARN`，赶紧去打开。

怎么确认现在是什么状态：

```bash
grep '"license_required"' /opt/mclink/config.server.json
# 或者问正在跑的服务端要（这才是真正生效的值）
journalctl -u mclink --since "1 day ago" | grep "授权校验" | tail -1
```

**管理员自己永远不会被拦**：客户端 `config.client.json` 里填了 `admin_token`，
服务端认它是管理员就直接放行，不需要激活。所以打开校验不会把自己锁在外面。


### 3.7 桌面版打不开或重复启动

**现象**：双击 `McLink.exe` 没反应，或开了好几个。

**原因**：McLink 是单实例的。第二次双击**不会开新窗口**，而是把**已经在跑的那个**叫到前面来。
如果你之前关了窗口，它其实**缩在系统托盘里**（右下角通知区域，可能要展开小三角才看得到）。

**怎么办**：

1. 看右下角托盘有没有 McLink 的图标（游戏手柄那个），右键 → 「显示主窗口」
2. 真要重开：托盘右键 → 「退出 McLink」，然后再双击

**如果托盘图标也没有、任务管理器里也没有进程，但还是打不开**：

```powershell
# 看有没有残留进程
Get-Process pythonw -ErrorAction SilentlyContinue

# 有就全杀掉
Get-Process pythonw | Stop-Process -Force

# 删掉可能残留的单实例锁文件，再启动
Remove-Item C:\Users\<你的用户名>\Desktop\mc-link\client\instance.port -Force
```

**还是打不开**，就用带日志的方式启动，看报错：

```
双击 client\start_gui_debug.bat
```

### 3.8 网页控制台打不开

`http://127.0.0.1:8787` 打不开，但桌面版正常。

**原因**：8787 被别的程序占了。McLink 会**自动顺延**到 8788、8789…（最多试 6 个）。
桌面版不受影响（网页控制台只是附加功能）。

**怎么办**：

1. 看客户端日志第一行，它会写实际的端口：
   ```
   [INFO ] 控制台已就绪: http://127.0.0.1:8788
   ```
2. 或者点托盘右键 → 「打开网页控制台」，它会用正确的端口打开
3. 想固定端口：改 `config.client.json` 的 `web.port`

### 3.9 任务栏图标显示成 Python

**现象**：任务栏（或 Alt-Tab）上 McLink 的图标是 Python 那条蛇，名字也可能写成
`pythonw.exe`。

**原因**：McLink 是 `McLink.exe` 拉起来的 `pythonw.exe` 进程，Windows 任务栏找图标的
顺序是「窗口图标 → 窗口类图标 → 进程主程序图标」。旧版本的
`mclink_gui.py` 只用 tkinter 的 `iconbitmap()` / `iconphoto()` 设图标，
而 Tk 8.6 在 Windows 上这两个调用**实际没有把 WM_SETICON 设上**（实测
`WM_GETICON` 一直是空），于是任务栏一路退到第 3 步 —— pythonw.exe 的图标。

**已经修好了**：`mclink_winicon.py` 用 ctypes 直接调 Win32：

- `WM_SETICON`（BIG/SMALL 两个都设）—— 任务栏按钮优先看这个
- `SetClassLongPtrW`（GCLP_HICON / GCLP_HICONSM）—— 补齐窗口类图标兜底

**怎么确认生效**（不用肉眼，直接问窗口要）：

```powershell
# McLink 开着的时候跑（在哪台机器上都行，脚本自带判据）
powershell -ExecutionPolicy Bypass -File C:\Users\<你的用户名>\Desktop\mc-link\tools\verify_taskbar_icon.ps1
```

输出里 `WM_GETICON BIG/SMALL` 都非空、而且和 `assets\mclink.ico` 的哈希一致，
就说明任务栏会画对。修好之前这两个值都是 `<null>`。

肉眼判断更简单：**双击 McLink.exe 之后，任务栏那个图标应该是绿色圆角方块 + 两个白箭头**
（和托盘图标、和 exe 文件本身的图标一样），不是 Python 的蛇。

**要是还显示 Python**：

1. 确认 `client\assets\mclink.ico` 在（分发时漏了这个文件就会退回去）
2. 确认 `client\mclink_winicon.py` 在（少了它也能跑，但没有 Win32 那一步）
3. Windows 有图标缓存，重启一次 explorer 再试：
   ```powershell
   Stop-Process -Name explorer -Force    # 会自动重启
   ```
4. 开始菜单里的 McLink 快捷方式被删了，任务栏按钮的标题也可能退化成
   `pythonw.exe` —— 分发版首次运行会自动建一个
   （`%APPDATA%\Microsoft\Windows\Start Menu\Programs\McLink.lnk`，
   它的 `System.AppUserModel.ID` = `McLink.PortMapper.Desktop.1`）。
   不想让它建：设环境变量 `MCLINK_NO_SHORTCUT=1`。

### 3.10 映射不会自动开启

**现象**：打开客户端之后，映射卡片是灰的（"已停用"），端口没在监听。
**这不是坏了** —— 1.2.6 起默认就是要你手动开。

**为什么这么改**：以前映射从配置里读出来 `enabled: true` 就直接注册，
等于"双击一下软件，端口就对外开着"，不知道的人容易莫名其妙暴露服务。

**怎么开**：

- 点卡片右边的开关（**已停用 → 已启用**）
- 或者点工具栏的 **「▶ 启动全部映射」** 一次全开
- 只想全关：**「■ 全部停止」**（会二次确认）

**想改回"启动就自动开"**：把 `client\config.client.json` 里

```json
"client": { "auto_start_mappings": true }
```

改成 `true` 重启客户端即可。**分发给别人的包里建议保持 false**。

**注意**：你自己在界面里**新增或编辑**的映射不受这个开关限制 ——
那是你主动加的东西，保存后立刻就会注册。

### 3.11 UDP 刚生效那一瞬间丢包

**现象**（1.2.6 之前）：基岩版（UDP）联机时，刚打开客户端或刚重连之后，
头一两秒发进去的包没反应，重连一次或者等一会儿才正常。

**原因**（两个问题叠加）：

1. **客户端**：收到服务端的 `register_ok` 就把映射标成"已生效"，但 UDP 隧道注册包
   只是**排进了队列**，而那个循环是**每秒 tick 一次** —— 最长 1 秒空窗里，
   本地界面显示正常、服务端却还没登记隧道地址，玩家这几百毫秒发的包全被丢。
2. **服务端**：公网 UDP 端口一绑定就报 `active`，不管隧道有没有就绪，
   等于对外报了个**假的可用状态**。

**已经修好了**（1.2.6）：

- 客户端拿到 `register_ok` 后**立刻**发一次隧道注册包，不再等下一轮
- 服务端新增"隧道已验证"标记：**真的收到过玩家包并成功转发**之后才报 `active`，
  否则报 `pending`（界面显示"等待中"而不是"已生效"）

**怎么确认你装的是修好的版本**：

```powershell
# 客户端日志里应该出现（而且是在"映射已生效"前后紧接着）
已发送 1 个 UDP 隧道保活包 -> <服务器>:7000
```

如果这段和"映射已生效"之间隔了 1 秒以上，说明还是旧版本。

---

### 3.14 界面中文看着糊

**现象**：品牌区副标题之类的小字中文糊成一团、笔画粘在一起。

**原因**：`f_tiny` 用的是 **8 点**。Tk 的正数点是 point，8 点算下来汉字只有
**12px 高**，1080p 上中文就糊了。

**修法**（1.2.8）：整张字号表上调一档，副标题改用 `f_small`（10 点，15px）：

| 用途 | 之前 | 现在 |
| --- | --- | --- |
| f_title | 14 | 15 |
| f_card_title | 11 | 12 |
| f_body | 10 | 11 |
| f_small | 9 | 10 |
| f_tiny | 8 | 9 |
| f_stat | 17 | 18 |

**自己改字号**：`client/mclink_gui.py` 里 `self.f_tiny = (FONT, 9)` 这一组。
⚠️ 字号放大会把对话框撑高，改完要重量一遍
（四个对话框都是"按内容定尺寸"或"固定尺寸"，撑过头会切按钮）。

### 3.15 openssl 探测到跑不起来的程序

**现象**：装完 Git for Windows 后 `tls_test.py` 从 18/18 掉到 3/18，
报"证书和私钥就绪"失败；严重时**服务端会静默退回明文模式**。

**原因**：`find_openssl()` 原来只判断 `os.path.exists()`。Git 自带两个 openssl：

```
Git\usr\bin\openssl.exe      rc=3221225794  "couldn't create signal pipe, Win32 error 5"   ← 坏的
Git\mingw64\bin\openssl.exe  rc=0           "OpenSSL 3.5.7"                                ← 好的
```

它返回了坏的 `usr/bin` 那个 → 调用方以为能签证书 → 签名失败 →
**默默退回明文**（你以为开了 TLS，其实没有）。测试那边则一直等证书出现直到超时。

**修法**（1.2.8）：新增 `_openssl_works()`，每个候选**真的跑一次
`openssl version`**（returncode 0 且输出含 "OpenSSL"）才算找到，否则试下一个。

**怎么确认**：

```powershell
cd mc-link\server
python -c "import mclink_server as s; print(s.find_openssl())"
```

返回的路径应该是**能跑起来**的那个。手动验一下：

```powershell
& "C:\Program Files\Git\mingw64\bin\openssl.exe" version
```

> 这里有个通用教训：**探测外部工具要验证"能不能用"，不是"在不在"**。
> 这个 bug 同时表现为"测试挂掉"和"安全问题"（静默降级成明文），
> 所以别把它当成单纯的测试问题。

## 4. 维修操作

### 4.1 改管理员密钥

```bash
# 1) 改服务端
nano /opt/mclink/config.server.json     # 改 admin_token
systemctl restart mclink

# 2) 改你自己的客户端
#    编辑 client\config.client.json 的 server.admin_token，重启客户端
```

⚠️ **两边必须一致**。服务端改了、客户端没改，客户端就进不了管理员模式。

### 4.2 改密钥（客户端接入密钥 `tokens`）

这个密钥是**所有客户端连接服务器的通行证**，和"管理员密钥"是两回事。

```bash
# 生成一个新的
python3.8 -c "import secrets; print(secrets.token_urlsafe(32))"

# 填到 /opt/mclink/config.server.json 的 tokens 数组，重启
systemctl restart mclink
```

**然后所有客户端都要改** `config.client.json` 的 `server.token`，否则连不上。
所以一般不用动它。

### 4.3 改端口池

```bash
nano /opt/mclink/config.server.json
#  "allowed_ports": [[25565, 25565], [19132, 19132], [25000, 26000]]
systemctl restart mclink
```

客户端**不用改**，重连后会自动同步新范围。

> ⚠️ 端口池不要和内核临时端口范围重叠（`cat /proc/sys/net/ipv4/ip_local_port_range`，
> 默认 `32768-60999`）。

### 4.4 重新生成 TLS 证书

```bash
# 停服务、删旧证书
systemctl stop mclink
rm -f /opt/mclink/server.crt /opt/mclink/server.key

# 启动时自动重签
systemctl start mclink
sleep 3
journalctl -u mclink --since "1 min ago" | grep 证书指纹
```

**然后必须清空所有客户端的 `cert_fingerprint`**，否则它们会拒绝连接（这是故意的防中间人设计）。

想手动签（比如换个 CN 或加域名）：

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /opt/mclink/server.key -out /opt/mclink/server.crt \
  -days 3650 -subj "/CN=你的域名" \
  -addext "subjectAltName=DNS:你的域名,IP:YOUR_SERVER_IP"
chown mclink:mclink /opt/mclink/server.key /opt/mclink/server.crt
chmod 600 /opt/mclink/server.key
systemctl restart mclink
```

### 4.5 彻底重装服务端

```bash
# 备份（重要！）
cd /opt/mclink
tar czf /root/mclink-backup/before-reinstall.tar.gz \
    config.server.json licenses.json server.crt server.key

# 停止并清理
systemctl stop mclink
systemctl disable mclink
rm -rf /opt/mclink /etc/systemd/system/mclink.service
systemctl daemon-reload

# 重新装（把新的 server 目录传到 /root/mclink 后）
cd /root/mclink && bash install.sh

# 恢复数据
cd /opt/mclink && tar xzf /root/mclink-backup/before-reinstall.tar.gz
chown -R mclink:mclink /opt/mclink
chmod 600 /opt/mclink/config.server.json /opt/mclink/licenses.json /opt/mclink/server.key
systemctl restart mclink
```

### 4.6 查端口被谁占了

```bash
ss -lntup | grep 25565      # TCP
ss -lnup  | grep 19132      # UDP
# 或者
fuser -n tcp 25565
```

### 4.7 邀请码/用户管理

这是**日常操作**，优先用图形界面：客户端 → 顶部「管理员」→ 输入管理员密钥。

命令行做法（服务端）：

```bash
python3.8 - <<'EOF'
import json, time
p = '/opt/mclink/licenses.json'
d = json.load(open(p, encoding='utf-8'))
print('=== 用户 ===')
for u in d.get('users', {}).values():
    seen = u.get('last_seen')
    seen = time.strftime('%Y-%m-%d %H:%M', time.localtime(seen)) if seen else '从未'
    print(f"  {u['username']:<16} {u.get('status'):<9} 最后在线 {seen}")
print('=== 待用邀请码 ===')
now = time.time()
for k, v in d.get('invites', {}).items():
    if not v.get('used_at') and v.get('expires_at', 0) > now:
        left = int((v['expires_at'] - now) / 60)
        print(f"  {k}  -> {v['username']}  还剩 {left} 分钟")
EOF
```

**改数据一定要先停服务**，改完再启动，否则会被内存里的数据覆盖：

```bash
systemctl stop mclink
# ... 改 ...
chown mclink:mclink /opt/mclink/licenses.json && chmod 600 /opt/mclink/licenses.json
systemctl start mclink
```

---

## 5. 应急恢复

### 5.1 把自己锁在外面了

**情况 A：忘了管理员密钥**

```bash
grep admin_token /opt/mclink/config.server.json
```

**情况 B：管理员密钥是空的 / 想直接换一个**

```bash
systemctl stop mclink
python3.8 - <<'EOF'
import json, secrets
p = '/opt/mclink/config.server.json'
d = json.load(open(p, encoding='utf-8'))
d['admin_token'] = secrets.token_urlsafe(32)
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('新管理员密钥：', d['admin_token'])
EOF
chown mclink:mclink /opt/mclink/config.server.json && chmod 600 /opt/mclink/config.server.json
systemctl start mclink
```

**情况 C：开了 `license_required` 但自己没激活**

不用慌 —— **管理员密钥本身就是万能通行证**，填了它就直接放行，不需要激活。

**情况 D：彻底进不去，先恢复可用**

> ⚠️ 下面这招是**临时**的：关掉授权校验 = 谁拿到客户端都能用。
> 它只影响 `licenses.json` 里的开关；等你进得去了，按
> [7.2](#72-授权校验默认就该是开着的) 立刻开回来。

```bash
# 临时关掉授权校验
systemctl stop mclink
python3.8 - <<'EOF'
import json
p = '/opt/mclink/licenses.json'
try:
    d = json.load(open(p, encoding='utf-8'))
except Exception:
    d = {"version": 1, "users": {}, "invites": {}}
d.setdefault('settings', {})['required'] = False
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('授权校验已关闭')
EOF
chown mclink:mclink /opt/mclink/licenses.json && chmod 600 /opt/mclink/licenses.json
systemctl start mclink
```

之后在客户端「管理员」里重新打开开关。

### 5.2 服务端起不来

**第一步：看报错**

```bash
systemctl status mclink -l
journalctl -u mclink -n 50 --no-pager
```

**常见原因和处理**：

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `config.server.json` JSON 解析失败 | 配置写坏了（多个逗号、中文引号） | 用备份覆盖，或手工修 |
| `Address already in use` | 7000 端口被别的程序占了 | `ss -lntup \| grep 7000` 查出来，停掉它 |
| `No module named mclink_license` | 少文件 | `ls /opt/mclink/*.py`，缺了就重新上传 |
| `ModuleNotFoundError` / 语法错误 | Python 版本不对 | `ls /usr/bin/python3.8`；`grep ExecStart /etc/systemd/system/mclink.service` |
| 权限相关的 Permission denied | 文件属主不对 | `chown -R mclink:mclink /opt/mclink` |

**语法自检**（不启动服务）：

```bash
/usr/bin/python3.8 /opt/mclink/mclink_server.py -c /opt/mclink/config.server.json --check-config
```

### 5.3 配置写坏了

```bash
# 看有没有自动备份（install.sh 会留一份）
ls -la /opt/mclink/config.server.json*

# 有备份就用备份
cp /opt/mclink/config.server.json.bak.* /opt/mclink/config.server.json

# 没有备份就手工最小化重建
systemctl stop mclink
cat > /opt/mclink/config.server.json <<'EOF'
{
  "bind": "0.0.0.0",
  "control_port": 7000,
  "data_port": 7000,
  "udp_port": 7000,
  "tokens": ["把原来那个密钥填回来"],
  "admin_token": "把管理员密钥填回来",
  "public_ip": "YOUR_SERVER_IP",
  "allowed_ports": [[25565, 25565], [19132, 19132], [25000, 26000]],
  "tls": true,
  "license_required": true,
  "log_level": "info"
}
EOF
chown mclink:mclink /opt/mclink/config.server.json && chmod 600 /opt/mclink/config.server.json
systemctl start mclink
```

原来的密钥可以从客户端配置里抄：`client\config.client.json` 的 `server.token`。

### 5.4 完全回滚到明文模式（TLS 出问题时）

如果 TLS 出了搞不定的问题，想先恢复可用：

```bash
systemctl stop mclink
sed -i 's/"tls": true/"tls": false/' /opt/mclink/config.server.json
systemctl start mclink
journalctl -u mclink --since "30 sec ago" | grep 传输加密
```

**所有客户端**也要跟着改：`config.client.json` 的 `server.tls` 改成 `false`。

> ⚠️ 明文模式下密钥和邀请码会被人看到，**只当临时措施**，问题解决后记得改回来。

### 5.5 授权库损坏

```bash
systemctl stop mclink
cp /opt/mclink/licenses.json /opt/mclink/licenses.json.broken
python3.8 -c "import json;json.load(open('/opt/mclink/licenses.json'))" || echo "确实坏了"

# 重建一个空的（所有人需要重新激活）
cat > /opt/mclink/licenses.json <<'EOF'
{"version": 1, "settings": {"required": false}, "users": {}, "invites": {}}
EOF
chown mclink:mclink /opt/mclink/licenses.json && chmod 600 /opt/mclink/licenses.json
systemctl start mclink
```

---

## 6. 安全清单

每月花 2 分钟过一遍：

- [ ] `bash /opt/mclink/healthcheck.sh` 没有 `✗`
- [ ] `ls -la /opt/mclink/config.server.json /opt/mclink/licenses.json /opt/mclink/server.key`
      —— 三个都应该是 `-rw-------`（600）
- [ ] 阿里云防火墙里没有多余放行的端口
- [ ] 服务端 `license_required` 是 `true`（要分发的话）
- [ ] 客户端报错日志里没有"指纹不一致"
- [ ] `journalctl -u mclink --since "7 days ago" | grep -c "鉴权失败"` —— 有大量说明有人在扫
- [ ] 备份还在，而且能解压

**如果怀疑密钥泄露**：

1. 换 `tokens`（客户端接入密钥），所有客户端跟着改
2. 换 `admin_token`
3. 重新生成证书 + 所有客户端清空 `cert_fingerprint`
4. 在管理员控制台里把可疑用户停用/删除

---

## 7. 升级流程

### 7.1 升级服务端

```bash
# 1) 先备份
cd /opt/mclink
tar czf /root/mclink-backup/before-upgrade-$(date +%Y%m%d-%H%M).tar.gz \
    config.server.json licenses.json server.crt server.key

# 2) 上传新文件（在你电脑上）
#    scp -i ~/.ssh/id_ed25519 server\*.py root@YOUR_SERVER_IP:/root/mclink/

# 3) 安装并重启
install -m 0644 /root/mclink/*.py /opt/mclink/
chown -R mclink:mclink /opt/mclink
systemctl restart mclink
sleep 3
bash /opt/mclink/healthcheck.sh
```

> 配置文件**不要**被新版本覆盖（`install.sh` 会保留已有的配置，
> 新配置存成 `config.server.json.new`）。要合并新字段就手工改。

### 7.2 授权校验（默认就该是开着的）

**现在 `license_required` 必须是 `true`**，`config.server.json` 里已经是了，
服务端启动日志也会写 `授权校验  已开启（…）`。

要是哪天它变成 `false` 了（比如手滑改回去），按这个改回来：

```bash
systemctl stop mclink
python3.8 - <<'EOF'
import json
p = '/opt/mclink/config.server.json'
d = json.load(open(p, encoding='utf-8'))
print('原来:', d.get('license_required'))
d['license_required'] = True
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
EOF
chown mclink:mclink /opt/mclink/config.server.json && chmod 600 /opt/mclink/config.server.json
systemctl start mclink
sleep 2
journalctl -u mclink --since "30 sec ago" | grep "授权校验"
```

真正生效的值由两部分决定，注意优先级：

| 位置 | 说明 |
| --- | --- |
| `config.server.json` 的 `license_required` | 首次启动（还没有 `licenses.json` 时）用这个 |
| `licenses.json` 的 `settings.required` | 一旦文件存在，**以它为准**（管理员在界面上开关就是改这里） |

所以改完 `config.server.json` 后，如果 `licenses.json` 已经存在，要把两边都改：

```bash
systemctl stop mclink
python3.8 - <<'EOF'
import json, os
p = '/opt/mclink/licenses.json'
if os.path.exists(p):
    d = json.load(open(p, encoding='utf-8'))
    d.setdefault('settings', {})['required'] = True
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('licenses.json settings.required -> True')
else:
    print('licenses.json 还不存在，只改 config.server.json 就够了')
EOF
chown mclink:mclink /opt/mclink/licenses.json 2>/dev/null; chmod 600 /opt/mclink/licenses.json 2>/dev/null
systemctl start mclink
```

> **开之前一定确认管理员密钥已配置**，否则你连自己都进不去。
> 服务端也会挡这个误操作（没配 admin_token 时拒绝开启）。

### 7.3 关闭授权校验（别关）

关掉它 = 谁拿到客户端都能用、不需要密钥。**分发场景下这就是个漏洞**，
2026-10-04 之前线上就是 `false`，已经被当成 bug 修掉了。

真要临时关（比如排查问题时），管理员接口现在要求显式确认
（`set_required` 带 `confirm_off: true`），而且服务端会打 `WARN`：
`注意：现在任何人都能用这个客户端，不需要密钥！`

已有的用户和邀请码数据不会丢。排查完记得按 [7.2](#72-授权校验默认就该是开着的) 打开。

### 7.4 制作分发包给朋友

在你电脑上：

```powershell
cd C:\Users\<你的用户名>\Desktop\mc-link\client
powershell -ExecutionPolicy Bypass -File launcher\make_dist.ps1 -Zip
```

产物在 `mc-link\dist\`。它会：

- **自动清空 `admin_token`**（绝不能让朋友拿到管理员密钥）
- **在配置里写 `"dist": true`** —— 客户端看到它就**不显示「管理员」按钮**。
  （分发版没有管理员密钥，那个入口点开只会报"管理员密钥不正确"，
  留着纯属误导，所以直接藏掉。这就是"分发包里有管理员选项但无效"那个问题的修法。）
- **删掉你自己的 `license.json`**（激活信息，不能带出去）
- 附一份给朋友的「使用说明.txt」

> **前提：服务端 `license_required` 必须是 `true`**，否则朋友拿到包不用激活也能用。
> 现状已经是 `true`，用这条命令确认：
> ```powershell
> ssh -i ~/.ssh/id_ed25519 root@YOUR_SERVER_IP "grep license_required /opt/mclink/config.server.json"
> ```

---

## 8. 客户端自动更新

朋友不用再手动收新包：客户端连上服务端之后会自己问一句"有没有新版本"，
有就在顶上弹一条蓝条 —— **点了才下载安装，不点就一直干自己的活**。
（不做"强制自动装"是故意的：装的时候必须退出重启，会掐断正在玩的隧道，
什么时候重启得用户自己说了算。）

### 8.1 发布一个新版本

> **现状（2026-10-05）**：已经发布过一版 —— 服务器上下发的是 **1.2.0（批次 20261005）**。
> 还停在 1.1.0 的客户端下次启动就会看到「有新版本」提示。
> 你自己这台已经是 1.2.0，不会被提示（可以在「设置 → 检查更新」里确认）。

在你自己电脑上，三条命令：

```powershell
cd C:\Users\<你的用户名>\Desktop\mc-link

# 1) 先把版本号改了：client\version.json 的 version 和 release
#    release 是批次号（年月日）。两个都比一遍，release 优先，
#    这样同一天改两版也能分出来。

# 2) 打包 + 上传 + 写清单 + 重启服务端，一条命令搞定
powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Build
```

不想让它自己打包，就先打再发：

```powershell
cd client
powershell -ExecutionPolicy Bypass -File launcher\make_dist.ps1 -Zip
cd ..
powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Zip client\dist\McLink-<日期>.zip
```

发布脚本会做这些事：

| 步骤 | 说明 |
| --- | --- |
| 校验 | 确认 zip 里真的有 `mclink_gui.py`（防止误传完整包）、且**不含** `license.json` |
| 算指纹 | 本地先算 `sha256`，写进清单，作为客户端下载后的校验依据 |
| 上传 | `scp` 到服务器 `/opt/mclink/updates/mclink-client-windows.zip` |
| 写清单 | 清单在本地拼好后 base64 传上去落盘（远端不再嵌 here-doc） |
| 重启 | `systemctl restart mclink`，并打印启动日志里的「客户端更新」一行 |
| 校对 | 再跑一次 `sha256sum` / `stat`，和本地值对照，不一致会提示 |

> **两个已经踩过的坑，改脚本时别再犯**：
> 1. 清单不要用「远端 here-doc + 变量展开」生成 —— `$SIZE` / `$(date)` 很容易
>    没被替换，写出一个坏 JSON。现在改成**本地拼好、base64 传上去**。
> 2. 不要把脚本用管道喂给 `ssh`（`... | ssh host bash -s`）：Windows PowerShell
>    往原生命令 stdin 写文本时会按 CRLF 重新编码，远端 bash 会一直报
>    `$'\r': command not found`，最后一行还可能直接语法错误。
>    现在改成写一个 **LF 行尾的临时文件**，用 `cmd /c "... < 文件"` 重定向。
>    远端脚本最后会 `echo MCLINK_RC=<退出码>`，脚本靠这一行判断成败
>    （`cmd` / `ssh` 的退出码在这一串嵌套里不可靠）。

常用参数：

```powershell
-Server root@YOUR_SERVER_IP     # 换服务器
-NoRestart                   # 只上传写清单，不重启
-Key <私钥路径>               # 默认用 ~/.ssh/id_ed25519
```

### 8.2 更新是怎么走网络的

**没有新端口、没有新防火墙规则**：检查和下载都走原来那条 7000/TCP
加密控制通道。

| 消息 | 方向 | 作用 |
| --- | --- | --- |
| `update_check` | 客户端 → 服务端 | 报上自己的版本 + 批次 |
| `update_info` | 服务端 → 客户端 | 有没有新版、版本号、大小、sha256、更新说明 |
| `update_fetch` | 客户端 → 服务端 | 要一段（默认 128 KB） |
| `update_chunk` | 服务端 → 客户端 | 一段 base64 数据 |

450 KB 左右的包大概 4 轮就拉完了。客户端拿到全部字节后**先校验 sha256**，
对不上直接丢弃并报错 —— 不会装坏包。

下载完成、用户点了「安装」之后：主程序把 zip 落到 `client\update-cache\`，
拉起一个**独立的小进程** `update_helper.py`，然后自己退出。助手干这些：

1. 等主程序退出（最多 90 秒），顺手清掉还占着目录的 pythonw 残留
2. 先 `testzip()` 把包验一遍，再**逐个文件**直接写进客户端目录
   （先写 `.new` 再原子替换，exe 被占用时带重试；不先解压到别处，
   省一半磁盘，也避开"新建目录写不进去"的环境）
3. **绝不碰**：`config.client.json`、`license.json`、`logs\`、`update-cache\`、
   `使用说明.txt`、`*.log`、`*.tmp`、`__pycache__`
4. 把新版本新增的**配置项**补进用户现有的配置（用户自己的值一个都不动）
5. 清掉 `__pycache__`，重新启动 `McLink.exe`

> 用户**不需要重新激活**：更新不动 `config.client.json` 和 `license.json`，
> 设备令牌还在，起来就接着用。

服务端侧的文件布局：

```
/opt/mclink/updates/
├── manifest.json                 # 清单：版本、批次、说明、sha256、大小
└── mclink-client-windows.zip     # 客户端包
```

### 8.3 更新失败怎么查

**先看客户端日志**（它最详细）：

```
client\update-cache\update.log
```

里面逐步写了：等主程序退出 → 解压了多少文件 → 覆盖了多少个、跳过哪些 →
有没有文件因为被占用而失败 → 有没有重新启动成功。

**再看服务端有没有认到清单**：

```bash
cat /opt/mclink/updates/manifest.json
journalctl -u mclink --since "10 min ago" | grep "客户端更新"
```

启动日志里应该有一行：

```
[INFO ]   客户端更新  windows 1.3.0（447564 字节）
```

如果写的是「没有已发布的更新包」，说明清单没写成功或路径不对。

**常见现象**：

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 朋友那边一直没提示 | 版本号没升（`version.json` 没改） | 改了重新发；release 也一起改 |
| 提示了但点了报 sha256 不一致 | 上传中断 / 包被改过 | 重新 `publish_update.ps1` |
| 装完还是旧版本 | `McLink.exe` 当时被占用没换上 | 看 update.log 的"覆盖失败"；退出客户端再让它自动更一次 |
| 装完要求重新激活 | 不该发生 | 检查 `license.json` 是否被误删；助手明确不碰它 |
| 更新说明只显示一行 | 顶栏放不下，这是设计 | 点「详情」看全文，见 [8.4](#84-更新说明看全文) |
| 下载卡在某百分比不动 | 网络抖动 / 服务端重启了 | 看进度条和「已下载/总大小」；重开客户端再点一次 |
| 想关掉自动检查 | — | 客户端「设置」里取消勾选，或配置 `client.auto_update: false` |

### 8.4 更新说明看全文

顶栏**只放得下一行**，所以它只显示更新说明的**第一行 + 省略号**。
想看全文点顶栏右边的 **「详情」** 按钮 —— 会弹一个窗口：

- 正文用可滚动的文本框显示，**说明再长也不会被截断**（多行、多段都行）
- 窗口高度按内容自动定：内容少就小窗，内容多就能滚（鼠标滚轮可用）
- 标题栏写着目标版本号、当前版本和包大小

**说明从哪来**：`client\version.json` 的 `notes` 字段。发布时
`publish_update.ps1` 会把它写进服务器上的 `manifest.json`，客户端通过
`update_info` 拿到。

**写发布说明的建议**：第一行写得概括一点（因为只有它会显示在顶栏上），
细节放后面几行：

```json
"notes": "修复 UDP 首包丢失；不再自动开映射\n\n详细：\n- 客户端拿到 register_ok 立刻发隧道包\n- 服务端新增隧道已验证标记"
```

> 老版本（1.2.5 及以前）没有「详情」按钮，顶栏直接截断 60 个字符 ——
> 那些版本看不到全文，让对方更新即可。

### 8.5 手动装（更新通道坏了时的兜底）

跟以前一样：把 zip 发给朋友，解压覆盖（**不要覆盖 `config.client.json`
和 `license.json`**），重启客户端即可。

---

## 9. 昵称与客户端列表

### 9.1 昵称是怎么来的

昵称**由用户自己在客户端里填**，不涉及账号密码：

1. 第一次激活时，激活窗口里就有一个「你的昵称」输入框（默认填机器名）
2. 之后随时可以在 **设置 → 我的昵称** 里改，保存即生效、不用重连

客户端把昵称放在两个地方告诉服务端：握手时的 `nickname` 字段，
以及改完之后立刻补一条 `hello_meta`。服务端把它写进授权库里对应的用户记录。

> 昵称只是个显示名，**不是身份**。谁能用仍然由"邀请码 + 机器指纹"决定，
> 所以随便填也不影响安全 —— 但它确实让管理员在列表里认得出"这是谁"。

### 9.2 管理台看到的是什么

客户端 →「管理员」→ **客户端** 标签页，每一行是一个客户端：

| 列 | 含义 |
| --- | --- |
| 在线 | ● 在线 / ○ 离线（离线的是曾经连过、现在没连） |
| 昵称 | 用户自己填的那个 |
| 用户名 | 激活时绑定的用户名（邀请码里指定的） |
| 邀请码 | **哪张邀请码**把他激活的，显示成 `MCLK-****-****-AB12`（掩码，不泄露完整码） |
| 来源 IP | 服务端看到的来源地址（不是他的内网 IP） |
| 客户端版本 | 用来一眼看出谁还没升级 |
| 机器名 | 他的电脑名 |
| 最后在线 | 最后一次连上来的时间 |

「更新」标签页还会显示**在线客户端的版本分布**（例如 `1.0.0: 4, 1.3.0: 1`），
发布新版本之后可以用它确认大家是不是都升上来了。

### 9.3 常见的几个问题

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 昵称显示 `—` | 老客户端（1.2.0 之前）不上报昵称 | 让对方更新（正好用第 8 节那套） |
| 昵称还是机器名 | 对方没填，留空了 | 让他去「设置」里填；留空就是机器名，这是设计 |
| 列表里没有某个朋友 | 他没在线过，或还没激活 | 看「用户」页；两边都是空的说明他没连上来 |
| 邀请码那列写 `—` | 管理员自己这种"没走邀请码"的 | 正常 |

---

# ============================================================
#  制作"分发包"—— 打包一份可以发给朋友的客户端
# ============================================================
#  用法（在 client 目录下）：
#      powershell -ExecutionPolicy Bypass -File launcher\make_dist.ps1
#      powershell -ExecutionPolicy Bypass -File launcher\make_dist.ps1 -Zip
#
#  它会做这些事：
#    - 复制运行需要的文件到 dist\McLink-<日期>\
#    - **清掉 admin_token**（绝不能让朋友拿到管理员密钥）
#    - **在配置里写 "dist": true**，界面上的「管理员」入口会因此消失
#      （分发版没有管理员密钥，留着那个按钮也点不进去）
#    - **删掉 license.json**（你自己的激活信息，不能带出去）
#    - 删掉日志、__pycache__、源码目录
#    - 可选：打成一个 zip
# ============================================================
param(
    [switch]$Zip,
    [string]$OutDir,
    [string]$Name = "McLink"
)

$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$client = Split-Path -Parent $here
$root   = Split-Path -Parent $client

if (-not $OutDir) {
    $stamp  = Get-Date -Format "yyyyMMdd"
    $OutDir = Join-Path $root "dist\$Name-$stamp"
}

# ---- 需要带上的东西 ----
$files = @(
    "McLink.exe",
    "mclink_gui.py",
    "mclink_client.py",
    "mclink_tray.py",
    "mclink_icon.py",
    "mclink_winicon.py",
    "update_helper.py",
    "version.json",
    "start_gui_debug.bat"
)
$dirs = @("assets", "web")

Write-Host "[信息] 输出目录: $OutDir"
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ---- 复制文件 ----
foreach ($f in $files) {
    $src = Join-Path $client $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $OutDir $f)
        Write-Host "  + $f"
    } else {
        Write-Host "  ! 缺少 $f（跳过）" -ForegroundColor Yellow
    }
}
foreach ($d in $dirs) {
    $src = Join-Path $client $d
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $OutDir $d) -Recurse
        # 源图不用发给别人
        Remove-Item (Join-Path $OutDir "$d\source-icon.jpg") -Force -ErrorAction SilentlyContinue
        Write-Host "  + $d\"
    }
}

# ---- 处理配置：保留服务器地址和主密钥，清掉管理员密钥 ----
$cfgSrc = Join-Path $client "config.client.json"
$cfgDst = Join-Path $OutDir "config.client.json"
if (Test-Path $cfgSrc) {
    $cfg = Get-Content $cfgSrc -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $cfg.server.host -or $cfg.server.host -eq "1.2.3.4") {
        Write-Host ""
        Write-Host "[警告] config.client.json 里的 server.host 还是占位符 1.2.3.4！" -ForegroundColor Yellow
        Write-Host "       先把服务器地址和 token 填好，再重新打这个包。" -ForegroundColor Yellow
    }
    $cfg.server.admin_token = ""
    if ($cfg.server.PSObject.Properties.Name -contains "_admin_token") {
        $cfg.server._admin_token = "（分发版已清空，这里不需要填）"
    }
    # 分发标记：客户端看到它就隐藏「管理员」按钮 —— 没有管理员密钥，那个入口
    # 点开也只会报"管理员密钥不正确"，不如直接不显示。
    if ($cfg.PSObject.Properties.Name -contains "dist") {
        $cfg.dist = $true
    } else {
        $cfg | Add-Member -NotePropertyName "dist" -NotePropertyValue $true
    }
    # 昵称清空：管理员自己的昵称不该出现在发给朋友的包里，
    # 朋友第一次激活时自己填。
    if ($cfg.PSObject.Properties.Name -contains "client") {
        if ($cfg.client.PSObject.Properties.Name -contains "nickname") {
            $cfg.client.nickname = ""
        }
    } else {
        $cfg | Add-Member -NotePropertyName "client" -NotePropertyValue (
            [pscustomobject]@{ nickname = ""; auto_update = $true
                               update_interval_min = 30 })
    }
    # 分发版：**不自动开启映射**。否则朋友双击一下软件，端口就对外开着了。
    # 他要自己点卡片上的开关（或「启动全部映射」）才开始转发。
    if ($cfg.client.PSObject.Properties.Name -contains "auto_start_mappings") {
        $cfg.client.auto_start_mappings = $false
    } else {
        $cfg.client | Add-Member -NotePropertyName "auto_start_mappings" `
                                 -NotePropertyValue $false
    }
    # 关键：**不能带 BOM**。Python 的 json.load 遇到 BOM 会直接抛
    # "Unexpected UTF-8 BOM"，朋友拿到包双击就起不来。
    # PowerShell 5.1 的 `Set-Content -Encoding UTF8` 是会加 BOM 的，所以这里手动写。
    $json = $cfg | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($cfgDst, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "  + config.client.json（已清空 admin_token / 昵称，写入 dist 标记，UTF-8 无 BOM）"
}

# ---- 确保不会带出你自己的授权信息 ----
foreach ($junk in @("license.json", "license.json.tmp", "logs", "__pycache__",
                    "launcher", "start.bat", "install_autostart.ps1",
                    "SetAumid.exe", "_mclink_setaumid.cs")) {
    $p = Join-Path $OutDir $junk
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
# 就算源目录有，也一定不能被复制进来
Get-ChildItem $OutDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $OutDir -Recurse -Directory -Filter "mclink-aumid-*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---- 附一个简短的说明 ----
$readme = @"
McLink 端口映射 —— 使用说明
================================

1) 双击 McLink.exe 打开。
2) 第一次用需要激活：点橙条上的「输入密钥激活」，把管理员给你的那串
   MCLK-XXXX-XXXX-XXXX 粘进去，点「激活」。
   密钥 24 小时内有效、只能用一次；激活成功后这台电脑就能一直用了，
   以后不用再输。换电脑或重装系统要重新找管理员要一个。
3) 点「＋ 新增映射」，选一个游戏模板（或自己填端口），保存。
4) 把卡片上的「连接地址」复制给一起玩的朋友，他们直接连就行。

关于「管理员」
--------
这个包里没有管理员密钥，界面上的「管理员」入口也**没有做出来** ——
它不是坏了，是这个版本故意不给的。发密钥、停用用户这些事只有管理员能做，
需要的话直接找他要。

常见问题
--------
Q: 提示"服务器证书指纹和上次不一致"，连不上。
A: 说明管理员换过服务器证书。用记事本打开 config.client.json，
   找到这一行：   "cert_fingerprint": "394b5dce..."
   把引号中间清空，变成：  "cert_fingerprint": ""
   保存后重新启动即可。（这只是让你重新信任新证书，不是出错。）

Q: 双击 McLink.exe 没反应。
A: 它可能已经缩到右下角的系统托盘里了（游戏手柄图标）。
   在托盘图标上右键 → 「显示主窗口」。真要重开就先右键 → 「退出 McLink」。

Q: 任务栏上的图标/名字不对。
A: 首次运行会自动在开始菜单里放一个 McLink 快捷方式（这样任务栏才能正确显示
   图标和名字）。如果当时被安全软件拦了，手动跑一次
   launcher 里的 create_shortcut.ps1，或者重启一次 McLink。

Q: 想换台电脑用 / 重装了系统。
A: 联系管理员，让他给你「解绑设备」，然后用新密钥重新激活。

出问题：双击 start_gui_debug.bat，把窗口里的内容截图发给管理员。

注意：这个文件夹里的 config.client.json 不要随便改，
      里面的服务器地址和密钥是管理员配好的。

这份包只给朋友用，里面不含管理员密钥，请勿转发到公开场合。
"@
Set-Content -Path (Join-Path $OutDir "使用说明.txt") -Value $readme -Encoding UTF8
Write-Host "  + 使用说明.txt"
# 顺手体检一下：配置必须是合法 JSON 且不带 BOM
try {
    $raw = [System.IO.File]::ReadAllBytes($cfgDst)
    if ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) {
        throw "config.client.json 带了 UTF-8 BOM，客户端会起不来！"
    }
    $null = $cfg   # ConvertFrom-Json 已经在上面做过，这里确认对象可用
    Write-Host "  ✓ 配置自检通过（无 BOM，JSON 合法）" -ForegroundColor Green
} catch {
    Write-Host "  ✗ 配置自检失败：$_" -ForegroundColor Red
    exit 1
}

# ---- 打包 ----
$size = (Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ""
Write-Host ("[完成] 分发包已生成：{0}  ({1:N0} KB)" -f $OutDir, ($size / 1KB)) -ForegroundColor Green

if ($Zip) {
    $zipPath = "$OutDir.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $zipPath
    Write-Host ("[完成] 压缩包：{0}  ({1:N0} KB)" -f $zipPath,
                ((Get-Item $zipPath).Length / 1KB)) -ForegroundColor Green
    Write-Host "       把这个 zip 发给朋友即可。"
} else {
    Write-Host "       把整个文件夹打包发给朋友即可（加 -Zip 参数可以直接出 zip）。"
}
Write-Host ""
Write-Host "提醒：这个包能不能用，最终由服务端说了算。" -ForegroundColor Yellow
Write-Host "      确认服务端 /opt/mclink/config.server.json 里 license_required 是 true，" -ForegroundColor Yellow
Write-Host "      否则别人拿到这个包不用激活也能用（那正是本次修掉的 bug）。" -ForegroundColor Yellow
Write-Host "      检查： ssh root@<服务器> 'grep license_required /opt/mclink/config.server.json'" -ForegroundColor DarkGray

# ============================================================
#  打发布包
#  用法（在 mc-link 目录下）：
#      powershell -ExecutionPolicy Bypass -File tools\make_release.ps1
#      powershell -ExecutionPolicy Bypass -File tools\make_release.ps1 -OutDir D:\
#
#  会生成两个包：
#    1. McLink-完整包-<日期>.zip
#       服务端 + 客户端 + 源码 + 文档 + 测试，用来归档 / 换机器 / 灾备
#    2. McLink-客户端分发包-<日期>.zip
#       只给朋友用的客户端，**已清空管理员密钥**、不带你自己的授权信息
#
#  ⚠️ 两个包都**不含 SSH 私钥**（deploy-key/）。那是能登录你服务器的凭据，
#     放进 zip 再同步到网盘就危险了。需要的话自己单独保管。
# ============================================================
param(
    [string]$OutDir,
    [switch]$SkipClient
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root   = Split-Path -Parent $root                    # mc-link/
$parent = Split-Path -Parent $root                    # 上一级（默认放这儿）

if (-not $OutDir) { $OutDir = $parent }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd"
$stage = Join-Path $env:TEMP "mclink-rel-$stamp"

function Say($m) { Write-Host $m }

# ---------------------------------------------------------- 0. 打包前体检
# 带中文的 .ps1 必须是 UTF-8 **带 BOM**。Windows PowerShell 5.1 对没有 BOM 的
# 文件按本地代码页（中文系统上是 GBK）解析，中文全变乱码，还会把
# "（{1:N0} KB）" 这种字符串解析错，直接报一堆语法错误。
# 编辑器（或某些工具）保存时容易把 BOM 丢掉，所以这里打包前自动补上。
function Repair-Ps1Bom {
    $fixed = 0
    Get-ChildItem $root -Recurse -Include *.ps1 -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -notmatch "\\dist\\" } |
      ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
            $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { return }
        $text = [System.IO.File]::ReadAllText($_.FullName,
                    (New-Object System.Text.UTF8Encoding $false))
        [System.IO.File]::WriteAllText($_.FullName, $text,
                    (New-Object System.Text.UTF8Encoding $true))
        $fixed++
        Say ("      ! {0} 缺少 UTF-8 BOM，已自动补上（否则中文会乱码）" -f $_.Name)
      }
    return $fixed
}
$bomFixed = Repair-Ps1Bom
if ($bomFixed -gt 0) { Say "      已修正 $bomFixed 个脚本的编码" }

# ---------------------------------------------------------- 1. 完整包
Say "[1/2] 准备完整包…"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
$dst = Join-Path $stage "mc-link"
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# 不要进包的东西。
# 注意用 robocopy 而不是 Get-ChildItem -Recurse：沙箱残留的 .piptmp 带着
# 拒绝 ACL，PowerShell 递归遍历会直接抛异常中断整个打包。
# 另外把验证/测试用的临时目录也排掉 —— 之前 .verify-friend（解压出来做验证的
# 那份客户端）就被整包打进去了，导致包里出现两份 config.client.json。
$xd = @("deploy-key", ".ssh", ".piptmp", ".ctmp", "dist", "logs", "__pycache__", ".git",
        ".verify-friend", ".verify", "fixtures-out")
$xf = @("instance.port", "license.json", "license.json.tmp", "*.pyc", "*.log",
        "desktop.ini",
        # 客户端首次建开始菜单快捷方式时会现编的辅助小工具，不用进包
        "SetAumid.exe", "_mclink_setaumid.cs")
# 名字以 _tmp 开头的测试目录（robocopy 不支持通配目录，单独列出来）
Get-ChildItem $root -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "_tmp*" -or $_.Name -like ".verify*" } |
    ForEach-Object { $xd += $_.Name }

$rcArgs = @($root, $dst, "/E", "/R:0", "/W:0", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
foreach ($d in $xd) { $rcArgs += "/XD"; $rcArgs += $d }
foreach ($f in $xf) { $rcArgs += "/XF"; $rcArgs += $f }
& robocopy @rcArgs | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) { throw "robocopy 复制失败（退出码 $rc）" }
Say "      已复制（robocopy 退出码 $rc，0-7 都算成功）"

# 包内说明
$note = @"
McLink 完整包  ——  生成于 $(Get-Date -Format 'yyyy-MM-dd HH:mm')
================================================================

这个包里有什么
--------------
  server\      服务端（传到阿里云服务器用）
  client\      客户端（你自己电脑上用的，含 McLink.exe）
  docs\        协议说明、排错手册、维护与维修手册
  test\        10 套自动化测试，全绿
  README.md    从零开始的完整部署指南

这个包里【没有】什么（故意的）
----------------------------
  deploy-key\  SSH 私钥 —— 那是能直接登录你服务器的凭据，
               放进 zip 再同步到网盘会很危险。请单独保管。
  license.json 你自己电脑的激活信息，每台机器一份，没必要打包
  logs\、__pycache__\、instance.port  运行时产生的垃圾

拿到新机器怎么用
----------------
  服务端：把 server\ 传上去，sudo bash install.sh
  客户端：装好 Python 3.8+，双击 client\McLink.exe

别忘了
------
  * 服务器上要恢复 config.server.json（含 admin_token 和 tokens）
  * 服务端 license_required 必须是 true，否则别人不用密钥就能用
  * 客户端要填 client\config.client.json 的 server.host 和 token
  * 阿里云控制台防火墙要放行 TCP/UDP 7000，以及游戏端口
  * 换过 TLS 证书的话，客户端要清空 cert_fingerprint

出问题先看：docs\维护与维修手册.md
"@
Set-Content -Path (Join-Path $dst "包内说明.txt") -Value $note -Encoding UTF8

$zip1 = Join-Path $OutDir "McLink-完整包-$stamp.zip"
if (Test-Path $zip1) { Remove-Item $zip1 -Force }
Compress-Archive -Path $dst -DestinationPath $zip1 -CompressionLevel Optimal
Say ("      完成: {0}  ({1:N0} KB)" -f $zip1, ((Get-Item $zip1).Length / 1KB))

# ---------------------------------------------------------- 2. 客户端分发包
if (-not $SkipClient) {
    Say "[2/2] 制作客户端分发包（给朋友用）…"
    $mk = Join-Path $root "client\launcher\make_dist.ps1"
    if (Test-Path $mk) {
        $distDir = Join-Path $root "dist"
        & $mk -OutDir $distDir -Zip | Out-Host
        # make_dist 生成的 zip 是 "<OutDir>.zip"（和 OutDir 同级），不在目录里
        $inner = "$distDir.zip"
        if (Test-Path $inner) {
            $zip2 = Join-Path $OutDir "McLink-客户端分发包-$stamp.zip"
            if (Test-Path $zip2) { Remove-Item $zip2 -Force }
            Copy-Item $inner $zip2 -Force
            Say ("      完成: {0}  ({1:N0} KB)" -f $zip2, ((Get-Item $zip2).Length / 1KB))
        } else {
            Say "      ! 没找到 $inner"
        }
        Remove-Item $distDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $inner -Force -ErrorAction SilentlyContinue
    } else {
        Say "      ! 找不到 client\launcher\make_dist.ps1，跳过分发包"
    }
}

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

Say ""
Say "输出目录：$OutDir"
Get-ChildItem $OutDir -Filter "McLink-*.zip" | Sort-Object Name |
    ForEach-Object { Say ("  {0,-40} {1,8:N0} KB" -f $_.Name, ($_.Length / 1KB)) }

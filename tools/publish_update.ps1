# ============================================================
#  发布一个客户端更新包到服务器（客户端自动更新用）
# ============================================================
#  用法（在 mc-link 目录下）：
#      powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Zip <zip路径>
#
#      # 不带 -Zip 就自己先打一个：
#      powershell -ExecutionPolicy Bypass -File tools\publish_update.ps1 -Build
#
#  它会做这些事：
#    1. 校验这个 zip 确实是个客户端分发包（里面有 mclink_gui.py）
#    2. scp 上传到服务器的 /opt/mclink/updates/mclink-client-windows.zip
#    3. 在服务器上算 sha256 + 大小，写好 updates\manifest.json
#    4. 重启 mclink 服务
#  之后朋友那边的客户端下次启动就会看到「有新版本」的提示。
#
#  ⚠️ 发布前记得改 client\version.json 里的 version / release，
#     不然版本号没变，客户端不会认为有新版本。
# ============================================================
param(
    [string]$Zip,
    [switch]$Build,
    [string]$Server = "root@YOUR_SERVER_IP",
    [string]$Key,
    [string]$RemoteDir = "/opt/mclink",
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root   = Split-Path -Parent $here                    # mc-link/
$client = Join-Path $root "client"

if (-not $Key) { $Key = Join-Path $HOME ".ssh\id_ed25519" }

function Find-Ssh([string]$exe) {
    $cands = @(
        (Join-Path $env:WINDIR "System32\OpenSSH\$exe"),
        (Join-Path $env:WINDIR "System32\$exe"),
        (Join-Path $env:ProgramFiles "OpenSSH\$exe")
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 $exe（Windows 自带 OpenSSH 一般在这里：$env:WINDIR\System32\OpenSSH\）"
}

$ssh = Find-Ssh "ssh.exe"
$scp = Find-Ssh "scp.exe"
$kh = Join-Path $env:TEMP "mclink-known-hosts"
$sshOpts = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=$kh")

# ---- 1. 先给自己打一个包 ----
if ($Build -and -not $Zip) {
    Write-Host "[1/5] 打包客户端…" -ForegroundColor Cyan
    $mk = Join-Path $client "launcher\make_dist.ps1"
    $out = Join-Path $root ("dist\McLink-" + (Get-Date -Format "yyyyMMdd"))
    & $mk -OutDir $out -Zip | Out-Host
    $Zip = "$out.zip"
}
if (-not $Zip) { throw "请用 -Zip 指定要发布的 zip，或者加 -Build 让我现打一个。" }
if (-not (Test-Path $Zip)) { throw "找不到文件：$Zip" }
$zipItem = Get-Item $Zip
Write-Host ("[1/5] 待发布的包：{0}  ({1:N0} KB)" -f $zipItem.Name, ($zipItem.Length / 1KB)) `
    -ForegroundColor Cyan

# ---- 2. 校验它确实是个客户端包 ----
Write-Host "[2/5] 校验包内容…" -ForegroundColor Cyan
Add-Type -AssemblyName System.IO.Compression.FileSystem
$names = @()
$za = [System.IO.Compression.ZipFile]::OpenRead($zipItem.FullName)
try {
    $names = $za.Entries | ForEach-Object { $_.FullName.Replace('\', '/') }
} finally { $za.Dispose() }

$need = @("mclink_gui.py", "mclink_client.py")
foreach ($n in $need) {
    if (-not ($names | Where-Object { $_ -eq $n -or $_ -like "*/$n" })) {
        throw "这个 zip 里没有 $n，看起来不是客户端分发包（是不是给了完整包？）"
    }
}
if ($names | Where-Object { $_ -like "*license.json*" }) {
    throw "包里有 license.json —— 那是私人的激活信息，不能发布出去！"
}
$hasCfg = $names | Where-Object { $_ -eq "config.client.json" -or $_ -like "*/config.client.json" }
if ($hasCfg) {
    Write-Host "      ! 注意：包里带 config.client.json（更新助手不会覆盖用户已有的那份）" `
        -ForegroundColor Yellow
}
Write-Host ("      ✓ 看起来是客户端包（{0} 个条目）" -f $names.Count) -ForegroundColor Green

# ---- 3. 版本号 ----
$verFile = Join-Path $client "version.json"
$ver = "0.0.0"; $rel = ""
if (Test-Path $verFile) {
    $v = Get-Content $verFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $ver = [string]$v.version
    $rel = [string]$v.release
    $notes = [string]$v.notes
} else {
    Write-Host "      ! 没有 client\version.json，版本号按 0.0.0 处理" -ForegroundColor Yellow
    $notes = ""
}
Write-Host ("[3/5] 版本 {0}（批次 {1}）" -f $ver, $rel) -ForegroundColor Cyan
$sha = (Get-FileHash $zipItem.FullName -Algorithm SHA256).Hash.ToLower()
Write-Host "      sha256 $sha" -ForegroundColor Cyan

# ---- 4. 上传 ----
Write-Host "[4/5] 上传到 $Server …" -ForegroundColor Cyan
$remoteZip = "$RemoteDir/updates/mclink-client-windows.zip"
& $ssh @sshOpts $Server "mkdir -p $RemoteDir/updates" | Out-Null
& $scp @sshOpts $zipItem.FullName "${Server}:$remoteZip"
if ($LASTEXITCODE -ne 0) { throw "scp 上传失败（退出码 $LASTEXITCODE）" }
Write-Host "      ✓ 已上传 $remoteZip" -ForegroundColor Green

# ---- 5. 服务器上写清单 + 重启 ----
Write-Host "[5/5] 写清单并重启服务端…" -ForegroundColor Cyan

# 清单在本地拼好再传上去：远端 shell 里再嵌 here-doc/变量展开太容易踩坑
# （第一版就栽在这儿：$SIZE / $(date) 没被替换，反而把 JSON 写坏了）。
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$manifest = [ordered]@{
    version      = $ver
    release      = $rel
    notes        = $notes
    published_at = $stamp
    packages     = [ordered]@{
        windows = [ordered]@{
            file    = "mclink-client-windows.zip"
            version = $ver
            release = $rel
            size    = $zipItem.Length
            sha256  = $sha
        }
    }
}
$manifestJson = $manifest | ConvertTo-Json -Depth 6
$manifestB64 = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes($manifestJson))

$rcLines = @(
    "set -e",
    "cd $RemoteDir/updates",
    "echo '$manifestB64' | base64 -d > manifest.json",
    "chown -R mclink:mclink $RemoteDir/updates 2>/dev/null || true",
    "chmod 644 mclink-client-windows.zip manifest.json",
    'echo "--- manifest.json ---"',
    "cat manifest.json",
    'echo "--- 服务端看到的包 ---"',
    "ls -la mclink-client-windows.zip",
    "sha256sum mclink-client-windows.zip"
)
if (-not $NoRestart) {
    $rcLines += "systemctl restart mclink"
    $rcLines += "sleep 3"
    $rcLines += "systemctl is-active mclink"
    $rcLines += 'journalctl -u mclink --since "30 sec ago" --no-pager | grep -E "客户端更新|已启动" || true'
}
# 每一行都单独清掉 \r，并且**不再用管道喂给 ssh**：
# PowerShell 往原生命令的 stdin 写文本时会按平台默认行尾重新编码，
# 到了远端 bash 就变成 "$'\r': command not found"（第一版踩了两次）。
# 改成写一个明确用 LF 的临时文件，再让 cmd 做字节级的 stdin 重定向。
function Repair-Cr([string]$s) { return ($s -replace "[`r`n]", " ") }

# 远端脚本最后会 echo 一行 MCLINK_RC=<退出码>，我们从输出里把它抠出来。
# 为什么不看 cmd /c 的退出码：cmd 经过嵌套引号之后拿到的是 ssh 的退出码，
# 而 ssh 会把远端命令的退出码再往后传，实测拿到的 0 不可靠；
# 让远端自己报最稳。
function Invoke-Remote([string]$script, [switch]$Show) {
    $tmpFile = [System.IO.Path]::GetTempFileName()
    try {
        $body = $script.TrimEnd() + "`n" +
                'rc=$?; echo "MCLINK_RC=$rc"; exit $rc' + "`n"
        [System.IO.File]::WriteAllText($tmpFile, $body,
            (New-Object System.Text.UTF8Encoding $false))
        $argLine = (@($sshOpts) + @($Server, "bash -s")) -join " "
        $out = & cmd /c "`"$ssh`" $argLine < `"$tmpFile`" 2>&1"
        $rc = 0
        foreach ($line in @($out)) {
            if ("$line" -match '^MCLINK_RC=(\d+)') { $rc = [int]$Matches[1] }
        }
        if ($Show) {
            foreach ($line in @($out)) {
                if ("$line" -notmatch '^MCLINK_RC=') { Write-Host $line }
            }
        }
        return $rc
    } finally {
        Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
    }
}

$remote = (($rcLines | ForEach-Object { Repair-Cr $_ }) -join "`n") + "`n"
$rc = Invoke-Remote $remote -Show
if ($rc -ne 0) {
    Write-Host ""
    Write-Host "      ! 远端脚本退出码 $rc —— 去看上面的输出，哪一步报错就停在哪一步" -ForegroundColor Yellow
    throw "发布失败：服务端写清单/重启没成功"
}

# ---- 收尾核对：服务器上文件的 sha256 必须和我们发的那个一致 ----
Write-Host ""
Write-Host "--- 校对（服务器上的实际值） ---" -ForegroundColor Cyan
Write-Host ("期望 sha256: {0}" -f $sha)
Write-Host ("期望大小  : {0} 字节" -f $zipItem.Length)
$verify = (@(
    "cd $RemoteDir/updates",
    "sha256sum mclink-client-windows.zip",
    "stat -c%s mclink-client-windows.zip"
) | ForEach-Object { Repair-Cr $_ }) -join "`n"
Invoke-Remote $verify -Show | Out-Null

Write-Host ""
Write-Host "[完成] 更新包已发布。" -ForegroundColor Green
Write-Host "       版本 $ver（批次 $rel）"
Write-Host "       朋友那边重启客户端就会看到「有新版本」提示。"
Write-Host "       自己这台想立刻看到：客户端 → 设置 → 检查更新。"

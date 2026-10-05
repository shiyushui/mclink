# ============================================================
#  把 mc-link 推到 GitHub —— 装完 Git 之后跑这一个脚本就行
#
#  用法（在你自己的 PowerShell 窗口里，不要在沙箱里跑）：
#      powershell -ExecutionPolicy Bypass -File push_to_github.ps1
#
#  它会：
#    1. 先做一遍"推送前体检"：确认没有密钥/服务器信息会被提交
#    2. git init → add → commit
#    3. 关联 https://github.com/shiyushui/mclink.git
#    4. push
#
#  推送前请确认：LastLogin 那步会要你登录 GitHub（浏览器或令牌）
# ============================================================

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Remote = "https://github.com/shiyushui/mclink.git"

Write-Host ""
Write-Host "=== 0. 检查 git ===" -ForegroundColor Cyan
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Host "找不到 git。先装：" -ForegroundColor Red
    Write-Host "    winget install --id Git.Git -e" -ForegroundColor Yellow
    Write-Host "（装完关掉这个窗口重开，让 PATH 生效）"
    exit 1
}
Write-Host "  git: $($git.Source)"
git --version

Set-Location $Repo
Write-Host ""
Write-Host "=== 1. 推送前体检（这一步最重要，别跳过）===" -ForegroundColor Cyan

$forbidden = @()
# 1a. 这些文件**根本不该出现在待提交列表里**（.gitignore 应已排除）
$mustIgnore = @("server/config.server.json", "client/config.client.json",
                "licenses.json", "server/licenses.json", "client/licenses.json",
                "server/server.key", "server/server.crt")
foreach ($p in $mustIgnore) {
    if (Test-Path $p) {
        Write-Host "  存在（应被 .gitignore 排除）: $p" -ForegroundColor DarkGray
    }
}
# 1b. 扫私钥文件头。
#     只有这一条"形状"检查是可靠的 —— 私钥一定以这个头开头。
#     我原来还加了一条"揪长随机串"的启发式，实测两边都不可靠
#     （文档里的字母表示例会误报，字符集简单的真 token 会漏报），
#     所以删掉了：**别用启发式当安全闸门**。
#     真正把关的是第 3 步打印出来的待提交文件清单。
#     注意：这里不能把真实密钥当字符串写进来 —— 脚本本身也要提交。
$files = Get-ChildItem -Recurse -File | Where-Object {
    $_.FullName -notmatch '\\\.git\\' -and
    $_.Extension -notin @('.zip', '.exe', '.ico', '.png', '.jpg', '.pyc')
}
foreach ($f in $files) {
    # 仓库里这对是**故意公开**的一次性测试证书（见 test/fixtures/README.md）
    if ($f.Name -eq "TEST-ONLY.key" -or $f.Name -eq "TEST-ONLY.crt") { continue }
    $txt = Get-Content $f.FullName -Raw -ErrorAction SilentlyContinue
    if ($null -eq $txt) { continue }
    if ($txt -match 'BEGIN (OPENSSH |RSA |EC |DSA |)PRIVATE KEY') {
        $forbidden += "$($f.FullName.Replace($Repo + '\', '')) 含私钥"
    }
}
if ($forbidden.Count -gt 0) {
    Write-Host "  *** 体检不通过，先处理这些再推： ***" -ForegroundColor Red
    $forbidden | ForEach-Object { Write-Host "    ! $_" -ForegroundColor Red }
    exit 1
}
Write-Host "  没有发现私钥文件 / 不该带的配置 ✓" -ForegroundColor Green
Write-Host "  提醒：机器检查只看两件事 —— 私钥文件头、文件名黑名单。" -ForegroundColor DarkGray
Write-Host "        密钥长什么样机器判断不了，所以第 3 步那份清单请亲眼过一遍。" -ForegroundColor DarkGray

Write-Host ""
Write-Host "=== 2. 初始化仓库并暂存 ===" -ForegroundColor Cyan
if (-not (Test-Path ".git")) { git init -b main | Out-Null }
git add -A

Write-Host ""
Write-Host "=== 3. 将要提交的文件（请亲眼过一遍）===" -ForegroundColor Cyan
$staged = git diff --cached --name-only
$staged | ForEach-Object { "  $_" }
Write-Host "  共 $($staged.Count) 个文件"

$bad = $staged | Where-Object {
    $_ -match 'config\.server\.json$|config\.client\.json$|licenses\.json$|id_ed25519|id_rsa$|\.key$|\.zip$|\.exe$'
} | Where-Object { $_ -ne "test/fixtures/TEST-ONLY.key" }
if ($bad) {
    Write-Host "  *** 有不该提交的文件： ***" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "    ! $_" -ForegroundColor Red }
    Write-Host "  先 git reset 然后检查 .gitignore"
    exit 1
}
Write-Host "  没有密钥/构建产物 ✓" -ForegroundColor Green

Write-Host ""
$msg = Read-Host "=== 4. 提交信息（直接回车用默认）==="
if (-not $msg) { $msg = "McLink: Windows 端口映射客户端 + Linux 服务端" }
git commit -m $msg

Write-Host ""
Write-Host "=== 5. 关联远程并推送 ===" -ForegroundColor Cyan
$existing = git remote
if ($existing -contains "origin") { git remote set-url origin $Remote }
else { git remote add origin $Remote }
git remote -v
Write-Host ""
Write-Host "  正在推送…（首次会让你登录 GitHub）" -ForegroundColor Yellow
git push -u origin main

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host "  仓库地址: https://github.com/shiyushui/mclink"

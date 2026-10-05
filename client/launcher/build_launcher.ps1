# ============================================================
#  编译 McLink.exe 启动器
#  用法：  powershell -ExecutionPolicy Bypass -File launcher\build_launcher.ps1
#  产物：  client\McLink.exe （无控制台窗口，带 McLink 图标）
# ============================================================
$ErrorActionPreference = "Stop"

$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$client = Split-Path -Parent $here
$src    = Join-Path $here "McLink.cs"
$out    = Join-Path $client "McLink.exe"
$icon   = Join-Path $client "assets\mclink.ico"

# ---- 图标没有就先生成 ----
if (-not (Test-Path $icon)) {
    Write-Host "[信息] 图标不存在，正在生成…"
    Push-Location $client
    & python mclink_icon.py
    Pop-Location
}
if (-not (Test-Path $icon)) { throw "图标生成失败：$icon" }

# ---- 找 csc.exe（.NET Framework 自带，任何 Windows 都有）----
$candidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework64\v3.5\csc.exe"
)
$csc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) { throw "找不到 csc.exe，无法编译启动器（可改用 start_gui.bat 启动）" }

Write-Host "[信息] 编译器 : $csc"
Write-Host "[信息] 源码   : $src"
Write-Host "[信息] 图标   : $icon"
Write-Host "[信息] 输出   : $out"

& $csc /nologo /target:winexe /optimize+ /codepage:65001 `
       "/win32icon:$icon" "/out:$out" `
       /reference:System.dll /reference:System.Windows.Forms.dll `
       $src

if ($LASTEXITCODE -ne 0) { throw "编译失败（退出码 $LASTEXITCODE）" }
if (-not (Test-Path $out)) { throw "没有生成 $out" }

$f = Get-Item $out
Write-Host ""
Write-Host "[完成] 已生成 $($f.FullName)  ($([math]::Round($f.Length/1KB,1)) KB)" -ForegroundColor Green
Write-Host "       双击它即可启动 McLink 桌面版。"

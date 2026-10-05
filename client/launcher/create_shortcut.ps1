# ============================================================
#  创建桌面快捷方式
#  用法（在 client 目录下）： 右键本文件 → 使用 PowerShell 运行
#  或：  powershell -ExecutionPolicy Bypass -File launcher\create_shortcut.ps1
#
#  也可以更简单：直接把 McLink.exe 拖到桌面，
#  然后右键 → 属性 → 更改图标 → 选 assets\mclink.ico
# ============================================================
param(
    [string]$Name = "McLink 端口映射"
)

$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$client = Split-Path -Parent $here
$exe    = Join-Path $client "McLink.exe"
$icon   = Join-Path $client "assets\mclink.ico"

if (-not (Test-Path $exe)) {
    Write-Host "[错误] 没找到 McLink.exe，请先运行 launcher\build_launcher.ps1 编译。" -ForegroundColor Red
    exit 1
}

$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("CommonDesktopDirectory")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

$made = 0
foreach ($dir in $targets) {
    $lnkPath = Join-Path $dir "$Name.lnk"
    try {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($lnkPath)
        $lnk.TargetPath = $exe
        $lnk.WorkingDirectory = $client
        if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
        $lnk.Description = "McLink 端口映射 - 让朋友直接连进你的游戏"
        $lnk.Save()
        Write-Host "[完成] $lnkPath" -ForegroundColor Green
        $made++
    } catch {
        Write-Host "[跳过] $lnkPath —— $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if ($made -eq 0) {
    Write-Host ""
    Write-Host "没能自动创建。手动做法：把 McLink.exe 拖到桌面即可，" -ForegroundColor Yellow
    Write-Host "想换图标就右键快捷方式 → 属性 → 更改图标 → 选 assets\mclink.ico。"
} else {
    Write-Host ""
    Write-Host "以后双击桌面上的「$Name」就能启动 McLink。" -ForegroundColor Green
}

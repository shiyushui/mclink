# ============================================================
#  McLink  Windows 开机自启安装脚本
#  用法（在 client 目录下右键"用 PowerShell 运行"，或执行）：
#      powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#  卸载：
#      powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall
# ============================================================
param(
    [switch]$Uninstall,
    [string]$TaskName = "McLink"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] 已删除开机自启任务：$TaskName" -ForegroundColor Green
    } else {
        Write-Host "[信息] 没有找到任务 $TaskName，无需删除。" -ForegroundColor Yellow
    }
    return
}

# ---- 找 Python ----
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Write-Host "[错误] 没找到 python.exe，请先安装 Python 并勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}
# 用 pythonw.exe 可以静默后台运行、不弹黑框
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

$script = Join-Path $here "mclink_client.py"
$cfg    = Join-Path $here "config.client.json"

if (-not (Test-Path $script)) { Write-Host "[错误] 找不到 $script" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $cfg))    { Write-Host "[错误] 找不到 $cfg" -ForegroundColor Red; exit 1 }

Write-Host "[信息] Python : $pyw"
Write-Host "[信息] 脚本   : $script"
Write-Host "[信息] 配置   : $cfg"

# ---- 注册计划任务 ----
$action = New-ScheduledTaskAction -Execute $pyw `
    -Argument "`"$script`" -c `"$cfg`"" `
    -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "McLink 端口映射客户端（开机自动启动）" | Out-Null

Write-Host ""
Write-Host "[OK] 开机自启已装好！任务名：$TaskName" -ForegroundColor Green
Write-Host "     下次开机 / 登录后会自动在后台运行，控制台 http://127.0.0.1:8787"
Write-Host ""
Write-Host "现在立刻启动一次： Start-ScheduledTask -TaskName $TaskName"
Write-Host "停止运行：         Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "取消自启：         powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall"

# 检查 McLink 主窗口到底用了哪个图标（排查"任务栏显示 Python 图标"用）
#
# 用法（在 McLink 已经启动的情况下）：
#   powershell -ExecutionPolicy Bypass -File tools\verify_taskbar_icon.ps1
#   powershell -ExecutionPolicy Bypass -File tools\verify_taskbar_icon.ps1 -ClientDir D:\McLink
#
# 判据：主窗口的 WM_GETICON BIG/SMALL 都不为空，而且和 assets\mclink.ico
# 里对应尺寸的哈希一致。修好之前这两个值都是空的（Windows 于是退回
# pythonw.exe 的 Python 图标）。
param(
    [string]$ClientDir,
    [string]$ProcName = "pythonw"
)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $ClientDir) { $ClientDir = Split-Path -Parent $here }
$ico = Join-Path $ClientDir "assets\mclink.ico"

Add-Type -AssemblyName System.Drawing
Add-Type -Namespace McLinkIconCheck -Name Native -MemberDefinition @'
public delegate bool EnumProc(IntPtr h, IntPtr l);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr wp, IntPtr lp, uint flags, uint timeout, out IntPtr result);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern IntPtr GetClassLongPtr(IntPtr h, int idx);
'@

function Get-IconTag([IntPtr]$h) {
    if ($h -eq [IntPtr]::Zero) { return "<null>" }
    try {
        $ic = [System.Drawing.Icon]::FromHandle($h)
        $bmp = $ic.ToBitmap()
        $b = New-Object System.Drawing.Bitmap $bmp.Width, $bmp.Height, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($b)
        $g.DrawImage($bmp, 0, 0, $bmp.Width, $bmp.Height)
        $g.Dispose()
        $ms = New-Object System.IO.MemoryStream
        $b.Save($ms, [System.Drawing.Imaging.ImageFormat]::Bmp)
        $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash($ms.ToArray())
        $tag = "$($bmp.Width)x$($bmp.Height)/" + [BitConverter]::ToString($sha).Replace('-', '').Substring(0, 12)
        $ic.Dispose(); $bmp.Dispose(); $b.Dispose(); $ms.Dispose()
        return $tag
    } catch { return "ERR" }
}

$procs = @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) {
    Write-Host "[错误] 没有找到 $ProcName 进程 —— 先把 McLink 打开再跑这个脚本。" -ForegroundColor Red
    exit 1
}
$targets = @($procs | ForEach-Object { [uint32]$_.Id })
Write-Host "检查进程: $($targets -join ', ')"

$script:rows = New-Object System.Collections.ArrayList
$cb = [McLinkIconCheck.Native+EnumProc]{
    param($h, $l)
    $wp = 0
    [McLinkIconCheck.Native]::GetWindowThreadProcessId($h, [ref]$wp) | Out-Null
    if ($targets -contains [uint32]$wp) {
        $cls = New-Object System.Text.StringBuilder 256
        [McLinkIconCheck.Native]::GetClassName($h, $cls, 256) | Out-Null
        if ($cls.ToString() -eq 'TkTopLevel') {
            $ttl = New-Object System.Text.StringBuilder 256
            [McLinkIconCheck.Native]::GetWindowTextW($h, $ttl, 256) | Out-Null
            $big = [IntPtr]::Zero; $sm = [IntPtr]::Zero
            [McLinkIconCheck.Native]::SendMessageTimeout($h, 0x7F, [IntPtr]1, [IntPtr]::Zero, 2, 2000, [ref]$big) | Out-Null
            [McLinkIconCheck.Native]::SendMessageTimeout($h, 0x7F, [IntPtr]0, [IntPtr]::Zero, 2, 2000, [ref]$sm) | Out-Null
            [void]$script:rows.Add([pscustomobject]@{
                Title    = $ttl.ToString()
                WM_BIG   = (Get-IconTag $big)
                WM_SMALL = (Get-IconTag $sm)
                Cls_BIG  = (Get-IconTag ([McLinkIconCheck.Native]::GetClassLongPtr($h, -14)))
                Cls_SMALL = (Get-IconTag ([McLinkIconCheck.Native]::GetClassLongPtr($h, -34)))
            })
        }
    }
    return $true
}
[McLinkIconCheck.Native]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null

Write-Host ""
Write-Host "=== McLink 主窗口 ==="
if ($script:rows.Count -eq 0) { Write-Host "  (没找到 TkTopLevel 窗口)" }
foreach ($r in $script:rows) {
    Write-Host "  标题: $($r.Title)"
    Write-Host "    WM_GETICON  BIG=$($r.WM_BIG)  SMALL=$($r.WM_SMALL)"
    Write-Host "    窗口类图标  BIG=$($r.Cls_BIG)  SMALL=$($r.Cls_SMALL)"
}

Write-Host ""
Write-Host "=== 参考：assets\mclink.ico ==="
if (Test-Path $ico) {
    foreach ($sz in 16, 32) {
        try {
            $i = New-Object System.Drawing.Icon($ico, $sz, $sz)
            Write-Host ("  {0}x{0} -> {1}" -f $sz, (Get-IconTag $i.Handle))
            $i.Dispose()
        } catch { Write-Host "  $sz 读取失败" }
    }
} else {
    Write-Host "  [错误] 找不到 $ico" -ForegroundColor Red
}

Write-Host ""
if ($script:rows.Count -gt 0 -and $script:rows[0].WM_BIG -ne "<null>" -and $script:rows[0].WM_SMALL -ne "<null>") {
    Write-Host "[OK] 窗口图标已设置 —— 任务栏应该显示 McLink 自己的图标。" -ForegroundColor Green
} else {
    Write-Host "[问题] WM_GETICON 还是空的，任务栏会退回 pythonw.exe 的 Python 图标。" -ForegroundColor Yellow
    Write-Host "       检查 assets\mclink.ico 和 mclink_winicon.py 是否都在。" -ForegroundColor Yellow
}

# 从任意图片生成多尺寸 ICO（需要系统自带的 csc + .NET 图形库）
# 用法: powershell -ExecutionPolicy Bypass -File launcher\build_icon.ps1 <图片> [输出.ico]
param(
    [Parameter(Mandatory=$true)][string]$Image,
    [string]$Out
)
$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$client = Split-Path -Parent $here
if (-not $Out) { $Out = Join-Path $client "assets\mclink.ico" }

$csc = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) { throw "找不到 csc.exe" }

$tool = Join-Path $env:TEMP "IcoFromImage.exe"
& $csc /nologo /target:exe /optimize+ /codepage:65001 "/out:$tool" `
       /reference:System.dll /reference:System.Drawing.dll `
       (Join-Path $here "IcoFromImage.cs")
if ($LASTEXITCODE -ne 0) { throw "编译 IcoFromImage 失败" }

& $tool $Image $Out (Join-Path $env:TEMP "mclink-preview.png")
if ($LASTEXITCODE -ne 0) { throw "生成图标失败" }
Write-Host "[完成] $Out" -ForegroundColor Green
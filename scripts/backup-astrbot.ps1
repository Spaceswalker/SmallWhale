# backup-astrbot.ps1
# 每天把 AstrBot\data 打成一个 zip，只保留最近 7 天。建议任务计划每天 03:00 跑。

$ErrorActionPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "_config.ps1")

$src = Join-Path $XJ_ASTRBOT "data"
$dst = Join-Path $XJ_ASTRBOT "_backup"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$stage = Join-Path $dst ("stage-" + $stamp)

robocopy $src $stage /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = Join-Path $dst ("data-" + $stamp + ".zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip)
Remove-Item $stage -Recurse -Force
Get-ChildItem $dst -Filter "data-*.zip" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force

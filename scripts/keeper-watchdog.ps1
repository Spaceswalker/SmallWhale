# keeper-watchdog.ps1
# 兜底看门狗：每 2 分钟确认 astrbot-keeper 自己在跑、AstrBot 的 6185 在听。
# 建议用「任务计划程序」每 2 分钟跑一次这个脚本（比常驻进程更不容易一起死掉）。

$ErrorActionPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "_config.ps1")

$log = Join-Path $PSScriptRoot "keeper-watchdog.log"
function Log([string]$m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 }
function Test-Port([int]$port) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return (($c | Measure-Object).Count -gt 0)
}
function Get-Keeper {
  $pat = '*astrbot-keeper.ps1*'
  $p = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like $pat }
  return ($p | Select-Object -First 1).ProcessId
}

$kp = Get-Keeper
Log ("tick: port={0} keeperPid={1}" -f (Test-Port $XJ_ASTRBOT_PORT), $kp)

if (-not $kp) {
  $vbs = Join-Path $PSScriptRoot "astrbot-keeper.vbs"
  if (Test-Path $vbs) { Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $vbs + '"'); Log "keeper started" }
  Start-Sleep -Seconds 8
}

if (-not (Test-Port $XJ_ASTRBOT_PORT)) {
  $starter = Join-Path $PSScriptRoot "start-astrbot.vbs"
  if (Test-Path $starter) { Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $starter + '"'); Log "astrbot start requested" }
}

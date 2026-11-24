# astrbot-keeper.ps1
# 每 60 秒检查一次：AstrBot 的 6185、NapCat 的 6099、登录账号是不是小鲸。
# 挂了就拉起来；账号不对就尝试重新登录（最多 3 次，防止死循环）。
# 由 astrbot-keeper.vbs 以隐藏窗口方式启动，建议放进「任务计划程序」开机运行。

$ErrorActionPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "_config.ps1")

$mutex = New-Object System.Threading.Mutex($false, "Global\XiaoJingKeeperMutex")
if (-not $mutex.WaitOne(0)) { exit }

$log = Join-Path $PSScriptRoot "astrbot-keeper.log"
function Log($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 }
Log "keeper started (astrbot + napcat + account)"

function Test-Port($port) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return (($c | Measure-Object).Count -gt 0)
}

$napMiss = 0; $astMiss = 0
$lastNapTry = [DateTime]::MinValue; $lastAstTry = [DateTime]::MinValue
$lastAcctFix = [DateTime]::MinValue; $acctFails = 0

while ($true) {
  Start-Sleep -Seconds 60

  # ---- AstrBot（6185）----
  if (-not (Test-Port $XJ_ASTRBOT_PORT)) {
    $astMiss++
    Log ("{0} down (miss={1})" -f $XJ_ASTRBOT_PORT, $astMiss)
    if ($astMiss -ge 2 -and ((Get-Date) - $lastAstTry).TotalSeconds -ge 120) {
      $lastAstTry = Get-Date
      $starter = Join-Path $PSScriptRoot "start-astrbot.vbs"
      if (Test-Path $starter) { Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $starter + '"') }
      Log "astrbot restart requested"
      Start-Sleep -Seconds 30
      $astMiss = 0
    }
  } else { $astMiss = 0 }

  # ---- NapCat（6099）----
  if (-not (Test-Port 6099)) {
    $napMiss++
    Log ("6099 down (miss={0})" -f $napMiss)
    if ($napMiss -ge 2 -and ((Get-Date) - $lastNapTry).TotalSeconds -ge 180) {
      $lastNapTry = Get-Date
      $fw = Join-Path $XJ_NAPCAT "framework\start-framework.vbs"
      if (Test-Path $fw) { Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $fw + '"') }
      Log "napcat restart requested"
      Start-Sleep -Seconds 30
      $napMiss = 0
    }
  } else { $napMiss = 0 }

  # ---- 账号必须是 $XJ_BOT_QQ ----
  if (Test-Port 6099) {
    $acct = ""
    try {
      $r = Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:" + $XJ_ONEBOT_HTTP + "/get_login_info") -ContentType "application/json" -Body '{}' -TimeoutSec 6
      $acct = [string]$r.data.user_id
    } catch { $acct = "" }
    if ($acct -ne "" -and $acct -ne $XJ_BOT_QQ) {
      Log ("wrong account {0} (expect {1})" -f $acct, $XJ_BOT_QQ)
      if (((Get-Date) - $lastAcctFix).TotalSeconds -ge 600 -and $acctFails -lt 3) {
        $acctFails++
        $lastAcctFix = Get-Date
        Log ("account fix attempt {0}/3: restart napcat" -f $acctFails)
        Get-Process -Name "QQ" -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 5
        $fw = Join-Path $XJ_NAPCAT "framework\start-framework.vbs"
        if (Test-Path $fw) { Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $fw + '"') }
      }
    } elseif ($acct -eq $XJ_BOT_QQ) {
      $acctFails = 0
    }
  }
}

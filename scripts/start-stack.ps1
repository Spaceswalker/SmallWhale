# 开机一键拉起：QQ(NapCat) → AstrBot
# 用法：powershell -File start-stack.ps1 [-DryRun] [-NoQQ] [-Timeout 180]
param(
  [switch]$DryRun,          # 只检查、不真的启动（用来试配置）
  [switch]$NoQQ,            # 不碰 QQ / NapCat（已经手动开过时用）
  [int]$Timeout = 180        # 每一步最多等多少秒
)

. (Join-Path $PSScriptRoot '_config.ps1')

$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'start-stack.log'

function Log([string]$msg) {
  $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
  Add-Content -Path $log -Value $line -Encoding UTF8
  Write-Host $line
}
function PortOpen([int]$port) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne(600)
    if ($ok) { $c.EndConnect($iar) } 
    $c.Close()
    return $ok
  } catch { return $false }
}
function WaitPort([int]$port, [string]$name, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    if (PortOpen $port) { Log "$name 已就绪（:$port）"; return $true }
    Start-Sleep -Milliseconds 800
  }
  Log "$name 等待超时（:$port，${seconds}s）"
  return $false
}
function EvalShell([string]$script) {
  try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $script) } catch { }
}

Log '=== 开始拉起 ==='

# 1) QQ / NapCat：启动 QQ 主程序，NapCat 是注入进 QQNT 的，QQ 起来它就在
if (-not $NoQQ) {
  if (PortOpen 6099) {
    Log 'NapCat WebUI(:6099) 已在运行，跳过 QQ 启动'
  } else {
    $qq = $XJ_QQ_EXE
    if (-not $qq) { $qq = Join-Path $XJ_QQNT 'QQ.exe' }
    if (Test-Path $qq) {
      Log "启动 QQ：$qq"
      if (-not $DryRun) { Start-Process -FilePath $qq -WindowStyle Normal } else { Log '(DryRun) 跳过启动 QQ' }
      if (-not (WaitPort 6099 'NapCat WebUI' $Timeout)) {
        Log '警告：NapCat 没起来。检查 QQ 是否“记住密码/自动登录”，或手动扫码一次'
      }
    } else {
      Log "警告：找不到 QQ：$qq（改 scripts/_config.ps1 里的 XJ_QQ_EXE）"
    }
  }
}

# 2) AstrBot：keeper 会常驻，这里先同步起一次，保证 6199 有服务端
if (PortOpen 6185) {
  Log 'AstrBot(:6185) 已在运行'
} else {
  Log '拉起 AstrBot'
  $vbs = Join-Path $PSScriptRoot 'start-astrbot.vbs'
  if ($DryRun) { Log '(DryRun) 跳过启动 AstrBot' }
  elseif (Test-Path $vbs) { Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $vbs + '"') -WindowStyle Hidden }
  else { EvalShell 'astrbot-keeper.ps1' }
  [void](WaitPort 6185 'AstrBot WebUI' $Timeout)
}

Log '=== 完成 ==='

# qq-update-guard.ps1
# 目的：让 QQ 停在 NapCat 适配的那个版本（默认见 _config.ps1 的 $XJ_QQ_VERSION）。
# 原理：QQNT 的 versions\config.json 里，启动器只看 hotUpdate 的 ready 标记来切版本；
#       把它清掉，启动器就一直停在 curVersion（不动安装文件、不碰账号数据）。
# 重要：QQ 自己也读这个文件 —— 所以这里不做 JSON 重新序列化，只在原文上改字段值，
#       写回必须是 UTF-8 无 BOM，否则 QQ 会报「文件损坏 / get config fail」。

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_config.ps1")

$versions = Join-Path $XJ_QQNT "versions"
$cfgPath = Join-Path $versions "config.json"
$log = Join-Path $PSScriptRoot "qq-update-guard.log"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$msg) {
  try { Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) -Encoding UTF8 } catch {}
}

try {
  if (-not (Test-Path $cfgPath)) { exit 0 }
  $bytes = [IO.File]::ReadAllBytes($cfgPath)
  $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
  $raw = [IO.File]::ReadAllText($cfgPath, [Text.Encoding]::UTF8)
  if ($hasBom) { $raw = $raw.TrimStart([char]0xFEFF) }
  try { $null = $raw | ConvertFrom-Json } catch { Write-Log "config.json parse failed, skip"; exit 0 }

  $new = [regex]::Replace($raw, '("ready"\s*:\s*)true', '${1}false')
  $new = [regex]::Replace($new, '("pendingVersion"\s*:\s*)"[^"]*"', '${1}""')

  if ($new -ne $raw) {
    [IO.File]::WriteAllText($cfgPath, $new, $utf8NoBom)
    Write-Log ("hotUpdate ready cleared (keep " + $XJ_QQ_VERSION + ")")
  }
} catch {
  Write-Log ("error: " + $_.Exception.Message)
}

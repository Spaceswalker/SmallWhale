# 提交助手：把「类型 + 一句话」拼成规范提交说明
#
# 用法：
#   .\scripts\commit.ps1 fix "修开始菜单图标不显示"
#   .\scripts\commit.ps1 docs "补 NapCat 排错步骤" -NoAdd
#
# 它会做三件事：
#   1) 默认先 git add -A（加 -NoAdd 跳过）
#   2) 校验类型是不是六种之一
#   3) 拼成 "<type>: <message>" 再提交

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Type,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Message,

    [switch]$NoAdd
)

$ErrorActionPreference = 'Stop'
$allowed = @('feat', 'fix', 'docs', 'style', 'refactor', 'chore')

$Type = $Type.ToLower().Trim()
if ($allowed -notcontains $Type) {
    Write-Host "类型不对：$Type" -ForegroundColor Red
    Write-Host ("可用的六种：" + ($allowed -join ' / ')) -ForegroundColor Yellow
    exit 1
}

$Message = $Message.Trim()
if ([string]::IsNullOrWhiteSpace($Message)) {
    Write-Host "说明不能为空" -ForegroundColor Red
    exit 1
}
if ($Message.EndsWith('。')) { $Message = $Message.TrimEnd('。') }
if ($Message.Length -gt 60) {
    Write-Host ("说明太长了（" + $Message.Length + " 字），控制在 60 字内") -ForegroundColor Yellow
}

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    if (-not $NoAdd) {
        & git add -A
        if ($LASTEXITCODE -ne 0) { throw "git add 失败" }
    }

    $staged = (& git diff --cached --name-only | Measure-Object).Count
    if ($staged -eq 0) {
        Write-Host "没有要提交的改动" -ForegroundColor Yellow
        exit 0
    }

    $line = "$Type`: $Message"
    & git commit -m $line
    if ($LASTEXITCODE -ne 0) { throw "git commit 失败" }

    Write-Host ""
    Write-Host ("已提交：" + $line) -ForegroundColor Green
    Write-Host ("共 " + $staged + " 个文件。推上去：git push") -ForegroundColor Cyan
}
finally {
    Pop-Location
}

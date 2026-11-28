# Commit helper: turn "type + one line" into a conventional commit message
#
# Usage:
#   .\scripts\commit.ps1 fix "fix missing Start menu icon"
#   .\scripts\commit.ps1 docs "add NapCat troubleshooting steps" -NoAdd
#
# It does three things:
#   1) runs git add -A first (skip with -NoAdd)
#   2) checks the type is one of the six
#   3) builds "<type>: <message>" and commits

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
    Write-Host "Unknown type: $Type" -ForegroundColor Red
    Write-Host ("Allowed types: " + ($allowed -join ' / ')) -ForegroundColor Yellow
    exit 1
}

$Message = $Message.Trim()
if ([string]::IsNullOrWhiteSpace($Message)) {
    Write-Host "Message must not be empty" -ForegroundColor Red
    exit 1
}
if ($Message.EndsWith('。')) { $Message = $Message.TrimEnd('。') }
if ($Message.Length -gt 60) {
    Write-Host ("Message too long (" + $Message.Length + " chars), keep it under 60") -ForegroundColor Yellow
}

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    if (-not $NoAdd) {
        & git add -A
        if ($LASTEXITCODE -ne 0) { throw "git add failed" }
    }

    $staged = (& git diff --cached --name-only | Measure-Object).Count
    if ($staged -eq 0) {
        Write-Host "Nothing to commit" -ForegroundColor Yellow
        exit 0
    }

    $line = "$Type`: $Message"
    & git commit -m $line
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

    Write-Host ""
    Write-Host ("Committed: " + $line) -ForegroundColor Green
    Write-Host ($staged + " file(s). Push with: git push") -ForegroundColor Cyan
}
finally {
    Pop-Location
}

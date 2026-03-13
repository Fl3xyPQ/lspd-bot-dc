$ErrorActionPreference = "Stop"

$repoPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$gitExe = "C:\Program Files\Git\cmd\git.exe"

if (-not (Test-Path $gitExe)) {
    Write-Host "Git není nainstalovaný na očekávané cestě: $gitExe" -ForegroundColor Red
    exit 1
}

Set-Location $repoPath

$status = & $gitExe status --porcelain
if (-not $status) {
    Write-Host "Žádné změny k odeslání." -ForegroundColor Yellow
    exit 0
}

$commitMessage = Read-Host "Zadej commit zprávu (Enter = automatická)"
if ([string]::IsNullOrWhiteSpace($commitMessage)) {
    $commitMessage = "Update bot $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
}

Write-Host "Přidávám změny..." -ForegroundColor Cyan
& $gitExe add .

Write-Host "Vytvářím commit..." -ForegroundColor Cyan
& $gitExe commit -m $commitMessage

Write-Host "Odesílám na GitHub..." -ForegroundColor Cyan
& $gitExe push

Write-Host "Hotovo. Railway si změny stáhne automaticky z GitHubu." -ForegroundColor Green

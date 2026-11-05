#Requires -Version 5.1
<#
.SYNOPSIS
    Create the virtualenv this skill needs, then verify a browser is reachable.

.DESCRIPTION
    Installs playwright + pillow into <repo>\.venv and runs the doctor. It never installs a
    browser it does not need: an existing Chrome or Edge is used as-is. Run this once, or
    again after moving the repo.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

if (-not $Python) {
    $candidates = @("python", "py")
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source; break }
    }
}
if (-not $Python) { throw "Python 3.10+ not found on PATH. Install Python, then rerun." }

if ((Test-Path $VenvPy) -and -not $Force) {
    Write-Host "virtualenv already present: $Venv"
} else {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    Write-Host "creating virtualenv at $Venv"
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}

Write-Host "installing dependencies (playwright, pillow)"
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $Root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $VenvPy (Join-Path $PSScriptRoot "doctor.py")
$doctorExit = $LASTEXITCODE

if ($doctorExit -ne 0) {
    Write-Host ""
    Write-Host "No usable browser found. Two options:" -ForegroundColor Yellow
    Write-Host "  1. install Google Chrome or Microsoft Edge (recommended), or"
    Write-Host "  2. let Playwright fetch its own Chromium:"
    Write-Host "     & `"$VenvPy`" -m playwright install chromium"
}

Write-Host ""
Write-Host "Try it:"
Write-Host "  .\pixelvision.cmd https://example.com -o .\shots"
exit $doctorExit

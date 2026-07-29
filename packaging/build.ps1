# ClusterOne build script — PyInstaller bundle + (optional) Inno Setup installer.
#
# Usage (from project root or anywhere)::
#
#   .\packaging\build.ps1                # bundle + installer (if iscc is on PATH)
#   .\packaging\build.ps1 -SkipInstaller # bundle only
#
# Requires: Python venv at .venv with PyInstaller installed
#           (`pip install -e .[dev]` or `pip install pyinstaller`).

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Python venv not found at $python — create with: python -m venv .venv"
    exit 1
}

Push-Location $root
try {
    Write-Host "[build] PyInstaller bundle…" -ForegroundColor Cyan
    & $python -m PyInstaller --clean --noconfirm "packaging\ClusterOne.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    Write-Host "[build] Bundle ready: dist\ClusterOne\ClusterOne.exe" -ForegroundColor Green

    if (-not $SkipInstaller) {
        $iscc = (Get-Command iscc -ErrorAction SilentlyContinue)
        if ($null -ne $iscc) {
            Write-Host "[build] Inno Setup installer…" -ForegroundColor Cyan
            & iscc "packaging\installer.iss"
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)" }
            Write-Host "[build] Installer ready: packaging\output\ClusterOne_Setup_0.1.0.exe" -ForegroundColor Green
        } else {
            Write-Host "[build] Inno Setup (iscc) not on PATH — skipping installer." -ForegroundColor Yellow
            Write-Host "        Install from https://jrsoftware.org/isdl.php and re-run." -ForegroundColor Yellow
        }
    }

    Write-Host "`n[build] Done." -ForegroundColor Green
}
finally {
    Pop-Location
}

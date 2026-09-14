$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    py -3 -m venv .venv
}

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt pyinstaller
& $python -m PyInstaller --noconfirm --clean --noconsole --onefile --name BrightnessSync .\src\brightness_sync.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $projectRoot\dist\BrightnessSync.exe"



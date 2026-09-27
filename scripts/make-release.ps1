$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$buildScript = Join-Path $PSScriptRoot "build.ps1"
$releaseRoot = Join-Path $projectRoot "release"
$distExe = Join-Path $projectRoot "dist\BrightnessSync.exe"
$releaseExe = Join-Path $releaseRoot "BrightnessSync.exe"

& $buildScript

if (-not (Test-Path $distExe)) {
    throw "Build finished but dist\BrightnessSync.exe was not found."
}

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
Copy-Item -LiteralPath $distExe -Destination $releaseExe -Force

Write-Host ""
Write-Host "Standalone exe created at:"
Write-Host "  $releaseExe"
Write-Host ""
Write-Host "Share that exe directly with end users."

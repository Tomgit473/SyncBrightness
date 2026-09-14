param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$buildScript = Join-Path $PSScriptRoot "build.ps1"
$distExe = Join-Path $projectRoot "dist\BrightnessSync.exe"
$installDir = Join-Path $env:LOCALAPPDATA "BrightnessSync"
$installedExe = Join-Path $installDir "BrightnessSync.exe"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "BrightnessSync"
$configDir = Join-Path $env:APPDATA "BrightnessSync"
$configPath = Join-Path $configDir "config.json"

if (-not $SkipBuild -or -not (Test-Path $distExe)) {
    & $buildScript
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath $distExe -Destination $installedExe -Force

New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $runValueName -Value "`"$installedExe`"" -PropertyType String -Force | Out-Null

New-Item -ItemType Directory -Force -Path $configDir | Out-Null
if (Test-Path $configPath) {
    $config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
}
else {
    $config = [pscustomobject]@{}
}

$defaults = @{
    enabled = $true
    start_with_windows = $true
    monitor_brightness_min = 0
    monitor_brightness_max = 100
    monitor_brightness_gamma = 1.15
    sync_contrast_to_brightness = $false
    monitor_contrast = 75
    smooth_transitions = $true
    smooth_step = 4
}

foreach ($entry in $defaults.GetEnumerator()) {
    if ($null -eq $config.PSObject.Properties[$entry.Key]) {
        $config | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value
    }
}

$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8

Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq "BrightnessSync.exe") -or
    ($_.Name -eq "pythonw.exe" -and $_.CommandLine -match "brightness_sync\.py")
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    }
    catch {
    }
}

Start-Sleep -Seconds 1
Start-Process -FilePath $installedExe -WindowStyle Hidden

Write-Host ""
Write-Host "Installed BrightnessSync to:"
Write-Host "  $installedExe"
Write-Host "Config file:"
Write-Host "  $configPath"
Write-Host ""
Write-Host "The app has been started and registered in Windows startup."


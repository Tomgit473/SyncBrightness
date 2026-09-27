param()

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$pythonExe = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$scriptPath = Join-Path $projectRoot "src\brightness_sync.py"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "BrightnessSync"
$startupCommand = "`"$pythonExe`" `"$scriptPath`""
$configDir = Join-Path $env:APPDATA "BrightnessSync"
$configPath = Join-Path $configDir "config.json"

if (-not (Test-Path $pythonExe)) {
    throw "pythonw.exe was not found. Create the virtual environment and install requirements first."
}

New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $runValueName -Value $startupCommand -PropertyType String -Force | Out-Null

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
    monitor_brightness_min = 10
    monitor_brightness_max = 90
    monitor_brightness_gamma = 1.1
    manage_contrast = $false
    sync_contrast_to_brightness = $false
    monitor_contrast = 70
    smooth_transitions = $true
    smooth_step = 4
    smooth_jump_threshold = 15
    first_run_notice_shown = $false
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
Start-Process -FilePath $pythonExe -ArgumentList "`"$scriptPath`"" -WindowStyle Hidden

Write-Host ""
Write-Host "Configured BrightnessSync to run hidden from source with pythonw:"
Write-Host "  $startupCommand"
Write-Host "Config file:"
Write-Host "  $configPath"
Write-Host ""
Write-Host "The app has been restarted and registered in Windows startup."



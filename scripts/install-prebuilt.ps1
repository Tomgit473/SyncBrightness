param(
    [string]$ExePath
)

$ErrorActionPreference = "Stop"

function New-AppShortcut {
    param(
        [string]$ShortcutPath,
        [string]$TargetPath,
        [string]$WorkingDirectory,
        [string]$Description
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = $TargetPath
    $shortcut.Description = $Description
    $shortcut.Save()
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ExePath) {
    $ExePath = Join-Path $scriptDir "BrightnessSync.exe"
}

$resolvedExe = Resolve-Path -LiteralPath $ExePath -ErrorAction SilentlyContinue
if (-not $resolvedExe) {
    throw "BrightnessSync.exe was not found. Keep this script next to the packaged executable."
}
$resolvedExe = $resolvedExe.Path

$installDir = Join-Path $env:LOCALAPPDATA "BrightnessSync"
$installedExe = Join-Path $installDir "BrightnessSync.exe"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "BrightnessSync"
$configDir = Join-Path $env:APPDATA "BrightnessSync"
$configPath = Join-Path $configDir "config.json"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) "Brightness Sync.lnk"
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startMenuShortcut = Join-Path $startMenuDir "Brightness Sync.lnk"

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath $resolvedExe -Destination $installedExe -Force

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

New-AppShortcut -ShortcutPath $desktopShortcut -TargetPath $installedExe -WorkingDirectory $installDir -Description "Brightness Sync"
New-AppShortcut -ShortcutPath $startMenuShortcut -TargetPath $installedExe -WorkingDirectory $installDir -Description "Brightness Sync"

Start-Sleep -Seconds 1
Start-Process -FilePath $installedExe -WindowStyle Hidden

Write-Host ""
Write-Host "Brightness Sync is installed."
Write-Host "Installed to:"
Write-Host "  $installedExe"
Write-Host "Desktop shortcut:"
Write-Host "  $desktopShortcut"
Write-Host "Start menu shortcut:"
Write-Host "  $startMenuShortcut"
Write-Host ""
Write-Host "The app is now running and will start with Windows."

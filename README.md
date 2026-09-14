# Brightness Sync

Brightness Sync is a small Windows tray utility that mirrors your laptop's built-in display brightness to DDC/CI-compatible external monitors.

## Features

- Applies laptop brightness-key changes immediately through `WmiMonitorBrightnessEvent`.
- Falls back to WMI polling when event delivery is unavailable.
- Detects DDC/CI-compatible external monitors with `monitorcontrol`.
- Prefers a Dell `S2240L` on HDMI when one is detected.
- Maps laptop brightness to the monitor's actual luminance range.
- Supports optional smoothing for non-event-driven transitions.
- Keeps a fixed monitor contrast or optionally syncs contrast to brightness.
- Adds a manual brightness and contrast control window for PC-only monitor control.
- Handles monitor disconnect and reconnect automatically.
- Runs in the system tray with enable/disable controls.
- Can register itself to start automatically with Windows.
- Logs activity to `%APPDATA%\BrightnessSync\logs\brightness-sync.log`.

## Requirements

- Windows 10 or Windows 11
- Python 3.11+
- DDC/CI enabled in the monitor's on-screen display menu
- A laptop with a built-in display whose brightness is exposed through WMI

## Project Layout

```text
Brightness-Sync-main/
|-- requirements.txt
|-- README.md
|-- scripts/
|   |-- build.ps1
|   |-- install.ps1
|   `-- install-source.ps1
`-- src/
    `-- brightness_sync.py
```

## Local Setup

1. Create a virtual environment:

```powershell
py -3.11 -m venv .venv
```

2. Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

4. Run the app locally:

```powershell
pythonw .\src\brightness_sync.py
```

On first launch the app creates:

- `%APPDATA%\BrightnessSync\config.json`
- `%APPDATA%\BrightnessSync\logs\brightness-sync.log`

## Config Defaults

Default values written to `config.json`:

- `enabled`: `true`
- `start_with_windows`: `true`
- `monitor_brightness_min`: `0`
- `monitor_brightness_max`: `100`
- `monitor_brightness_gamma`: `1.15`
- `sync_contrast_to_brightness`: `false`
- `monitor_contrast`: `75`
- `smooth_transitions`: `true`
- `smooth_step`: `4`
- `smooth_jump_threshold`: `15`
- `manual_mode`: `false`
- `manual_brightness`: `65`
- `manual_contrast`: `70`

If the external monitor still feels off, tune these values and restart the tray app.

## Manual Control Window

If your laptop panel is off and you want to control only the external monitor:

1. Open the tray icon menu.
2. Select `Open brightness controls`.
3. Turn on `Enable manual monitor control`.
4. Adjust the brightness and contrast sliders.

When manual mode is off, the app goes back to following the laptop brightness.

## Install Helpers

Build and install the packaged app:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Run directly from source and register it in Windows startup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-source.ps1
```

## Build

Build a single-file executable with the helper script:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

The packaged executable is created at `dist\BrightnessSync.exe`.

## Tray Menu

- `Enable synchronization`
- `Open brightness controls`
- `Manual monitor control`
- `Start with Windows`
- `Open log folder`
- `Quit`

## Notes

- Verify `DDC/CI` is enabled in the monitor OSD.
- Some HDMI adapters, docks, and KVM switches block DDC/CI traffic.
- If WMI brightness is unavailable on a system, the app stays running and logs the failure instead of crashing.

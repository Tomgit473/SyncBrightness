# Brightness Sync

Brightness Sync is a Windows tray app that keeps an external monitor in sync with laptop brightness changes.

## For you

Use the normal local install script:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

## Standalone `.exe` for other users

Build a standalone exe with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\make-release.ps1
```

That creates:

```text
release\BrightnessSync.exe
```

Give that `.exe` directly to the user. They can run it like a normal app.

## Safe defaults

- Brightness is limited to `10` through `90`
- Contrast changes are off by default
- Repeated monitor failures are temporarily disabled automatically
- Brightness-key updates apply instantly

## Requirements

- Windows 10 or Windows 11
- DDC/CI enabled on the external monitor
- A laptop whose brightness is exposed through WMI

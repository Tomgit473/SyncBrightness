from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk
import winreg
from dataclasses import asdict, dataclass
from typing import Optional

import pythoncom
import pystray
import wmi
from monitorcontrol import InputSource, get_monitors, vcp_codes
from monitorcontrol.monitorcontrol import Monitor
from monitorcontrol.vcp.vcp_abc import VCPError
from PIL import Image, ImageDraw

APP_NAME = "BrightnessSync"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = APP_NAME
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
LOG_PATH = LOG_DIR / "brightness-sync.log"
POLL_INTERVAL_SECONDS = 0.25
POLL_INTERVAL_IDLE_SECONDS = 1.0
IDLE_THRESHOLD_SECONDS = 5.0
TRANSITION_WAIT_SECONDS = 0.04
EVENT_DRAIN_TIMEOUT_MS = 10
MONITOR_FAILURE_DISABLE_COUNT = 3
MONITOR_FAILURE_COOLDOWN_SECONDS = 60.0
MONITOR_REFRESH_SECONDS = 30.0
MONITOR_RETRY_SECONDS = 5.0
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = r"Local\BrightnessSyncSingleton"
HOTKEY_TOGGLE = 1
HOTKEY_QUIT = 2


@dataclass
class AppConfig:
    enabled: bool = True
    start_with_windows: bool = True
    monitor_brightness_min: int = 10
    monitor_brightness_max: int = 90
    monitor_brightness_gamma: float = 1.1
    manage_contrast: bool = False
    sync_contrast_to_brightness: bool = False
    monitor_contrast: int = 70
    smooth_transitions: bool = True
    smooth_step: int = 4
    smooth_jump_threshold: int = 15
    manual_mode: bool = False
    manual_brightness: int = 65
    manual_contrast: int = 70
    first_run_notice_shown: bool = False
    preferred_monitor_model: str = ""
    preferred_monitor_vendor: str = ""
    hotkey_start: str = "ctrl+alt+s"
    hotkey_toggle: str = "ctrl+alt+b"
    hotkey_quit: str = "ctrl+alt+q"


DEFAULT_CONFIG = AppConfig()


@dataclass
class DetectedMonitor:
    monitor: Monitor
    description: str
    model: str
    input_source: str
    luminance_max: int
    contrast_max: int
    preferred: bool
    selected: bool = False

    @property
    def identity(self) -> str:
        return f"{self.description}|{self.model}|{self.input_source}"

    @property
    def label(self) -> str:
        model_part = self.model or "Unknown model"
        input_part = self.input_source or "Unknown input"
        return f"{model_part} [{input_part}] ({self.description})"


class SingleInstanceGuard:
    def __init__(self, mutex_name: str) -> None:
        self.mutex_name = mutex_name
        self.handle: int | None = None

    def acquire(self) -> bool:
        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.mutex_name)
        if not self.handle:
            raise ctypes.WinError()

        if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class HotkeyManager:
    """Register global hotkeys using Win32 RegisterHotKey API."""

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312

    _VK_MAP: dict[str, int] = {}

    @classmethod
    def _init_vk_map(cls) -> None:
        if cls._VK_MAP:
            return
        # Letters A-Z
        for c in range(ord("A"), ord("Z") + 1):
            cls._VK_MAP[chr(c).lower()] = c
        # Digits 0-9
        for d in range(10):
            cls._VK_MAP[str(d)] = 0x30 + d
        # F-keys
        for f in range(1, 25):
            cls._VK_MAP[f"f{f}"] = 0x6F + f
        # Common keys
        cls._VK_MAP.update({
            "space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
            "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "printscreen": 0x2C, "pause": 0x13, "numlock": 0x90, "scrolllock": 0x91,
        })

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._callbacks: dict[int, tuple[str, callable]] = {}
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._registered_ids: list[int] = []
        self._ready = threading.Event()
        self._init_vk_map()

    @staticmethod
    def _parse_hotkey(hotkey_str: str) -> tuple[int, int]:
        """Parse a hotkey string like 'ctrl+alt+b' into (modifiers, vk_code)."""
        HotkeyManager._init_vk_map()
        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        modifiers = HotkeyManager.MOD_NOREPEAT
        vk_code = 0

        for part in parts:
            if part in ("ctrl", "control"):
                modifiers |= HotkeyManager.MOD_CONTROL
            elif part in ("alt", "menu"):
                modifiers |= HotkeyManager.MOD_ALT
            elif part in ("shift",):
                modifiers |= HotkeyManager.MOD_SHIFT
            elif part in HotkeyManager._VK_MAP:
                vk_code = HotkeyManager._VK_MAP[part]
            else:
                raise ValueError(f"Unknown key in hotkey string: '{part}'")

        if vk_code == 0:
            raise ValueError(f"No key specified in hotkey string: '{hotkey_str}'")

        return modifiers, vk_code

    def register(self, hotkey_id: int, hotkey_str: str, callback: callable) -> None:
        """Queue a hotkey for registration. Call start() after all registrations."""
        self._callbacks[hotkey_id] = (hotkey_str, callback)

    def start(self) -> None:
        """Start the hotkey listener thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._message_loop, name="hotkey-listener", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        """Unregister all hotkeys and stop the listener."""
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._thread_id = None

    def _message_loop(self) -> None:
        """Win32 thread message pump for hotkey events (runs on daemon thread)."""
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        # Force creation of message queue for this thread
        msg = ctypes.wintypes.MSG()
        ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        # Register hotkeys to this thread (hWnd = None)
        for hotkey_id, (hotkey_str, _) in self._callbacks.items():
            try:
                modifiers, vk_code = self._parse_hotkey(hotkey_str)
                result = ctypes.windll.user32.RegisterHotKey(None, hotkey_id, modifiers, vk_code)
                if result:
                    self._registered_ids.append(hotkey_id)
                    self.logger.info("Registered global hotkey: %s (id=%d)", hotkey_str, hotkey_id)
                else:
                    self.logger.warning(
                        "Failed to register hotkey '%s' — it may be in use by another app.", hotkey_str,
                    )
            except ValueError as exc:
                self.logger.warning("Invalid hotkey config '%s': %s", hotkey_str, exc)

        self._ready.set()

        # Message pump
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                hotkey_id = int(msg.wParam)
                entry = self._callbacks.get(hotkey_id)
                if entry:
                    _, callback = entry
                    try:
                        callback()
                    except Exception:
                        self.logger.exception("Error in hotkey callback for id=%d", hotkey_id)
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        for hid in self._registered_ids:
            ctypes.windll.user32.UnregisterHotKey(None, hid)
        self._registered_ids.clear()


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()

        try:
            with self.path.open("r", encoding="utf-8-sig") as file:
                raw = json.load(file)
            return AppConfig(
                enabled=bool(raw.get("enabled", DEFAULT_CONFIG.enabled)),
                start_with_windows=bool(raw.get("start_with_windows", DEFAULT_CONFIG.start_with_windows)),
                monitor_brightness_min=int(raw.get("monitor_brightness_min", DEFAULT_CONFIG.monitor_brightness_min)),
                monitor_brightness_max=int(raw.get("monitor_brightness_max", DEFAULT_CONFIG.monitor_brightness_max)),
                monitor_brightness_gamma=float(raw.get("monitor_brightness_gamma", DEFAULT_CONFIG.monitor_brightness_gamma)),
                manage_contrast=bool(raw.get("manage_contrast", DEFAULT_CONFIG.manage_contrast)),
                sync_contrast_to_brightness=bool(raw.get("sync_contrast_to_brightness", DEFAULT_CONFIG.sync_contrast_to_brightness)),
                monitor_contrast=int(raw.get("monitor_contrast", DEFAULT_CONFIG.monitor_contrast)),
                smooth_transitions=bool(raw.get("smooth_transitions", DEFAULT_CONFIG.smooth_transitions)),
                smooth_step=int(raw.get("smooth_step", DEFAULT_CONFIG.smooth_step)),
                smooth_jump_threshold=int(raw.get("smooth_jump_threshold", DEFAULT_CONFIG.smooth_jump_threshold)),
                manual_mode=bool(raw.get("manual_mode", DEFAULT_CONFIG.manual_mode)),
                manual_brightness=int(raw.get("manual_brightness", DEFAULT_CONFIG.manual_brightness)),
                manual_contrast=int(raw.get("manual_contrast", DEFAULT_CONFIG.manual_contrast)),
                first_run_notice_shown=bool(raw.get("first_run_notice_shown", DEFAULT_CONFIG.first_run_notice_shown)),
                preferred_monitor_model=str(raw.get("preferred_monitor_model", DEFAULT_CONFIG.preferred_monitor_model)),
                preferred_monitor_vendor=str(raw.get("preferred_monitor_vendor", DEFAULT_CONFIG.preferred_monitor_vendor)),
                hotkey_start=str(raw.get("hotkey_start", DEFAULT_CONFIG.hotkey_start)),
                hotkey_toggle=str(raw.get("hotkey_toggle", DEFAULT_CONFIG.hotkey_toggle)),
                hotkey_quit=str(raw.get("hotkey_quit", DEFAULT_CONFIG.hotkey_quit)),
            )
        except Exception:
            logging.getLogger(APP_NAME).exception("Failed to read config file, using defaults.")
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(asdict(config), file, indent=2)


class StartupManager:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def is_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return value == self._startup_command()
        except FileNotFoundError:
            return False
        except OSError:
            self.logger.exception("Failed to read Windows startup registration.")
            return False

    def set_enabled(self, enabled: bool) -> None:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
                if enabled:
                    command = self._startup_command()
                    winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
                    self.logger.info("Registered Windows startup command: %s", command)
                else:
                    try:
                        winreg.DeleteValue(key, RUN_VALUE_NAME)
                    except FileNotFoundError:
                        pass
                    self.logger.info("Removed Windows startup registration.")
        except OSError:
            self.logger.exception("Failed to update Windows startup registration.")

    def _startup_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}"'

        python_exe = Path(sys.executable).resolve()
        pythonw_exe = python_exe.with_name("pythonw.exe")
        interpreter = pythonw_exe if pythonw_exe.exists() else python_exe
        script_path = Path(__file__).resolve()
        return f'"{interpreter}" "{script_path}"'

    def create_or_update_shortcut(self, hotkey: str = "ctrl+alt+s") -> None:
        """Create or update Windows Start Menu shortcut with a hotkey to start the app."""
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            programs = Path(shell.SpecialFolders("Programs"))
            lnk_path = programs / f"{APP_NAME}.lnk"

            python_exe = Path(sys.executable).resolve()
            pythonw_exe = python_exe.with_name("pythonw.exe")
            interpreter = pythonw_exe if pythonw_exe.exists() else python_exe
            script_path = Path(__file__).resolve()

            shortcut = shell.CreateShortCut(str(lnk_path))
            shortcut.TargetPath = str(interpreter)
            shortcut.Arguments = f'"{script_path}"'
            shortcut.WorkingDirectory = str(script_path.parent.parent)

            if hotkey:
                parts = [p.strip().capitalize() for p in hotkey.split("+")]
                shortcut.Hotkey = "+".join(parts)

            shortcut.Description = f"{APP_NAME} (Universal Brightness Sync)"
            shortcut.Save()
            self.logger.info("Created/updated Start Menu shortcut with hotkey '%s': %s", shortcut.Hotkey, lnk_path)
        except Exception:
            self.logger.exception("Failed to create Start Menu shortcut.")


class LaptopBrightnessReader:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.connection: Optional[wmi.WMI] = None
        self.event_watcher = None
        self.last_missing_log = 0.0
        self._connect()

    def _connect(self) -> None:
        self.connection = wmi.WMI(namespace="wmi")
        self.logger.info("Connected to root\\wmi for laptop brightness polling.")
        try:
            self.event_watcher = self.connection.watch_for(wmi_class="WmiMonitorBrightnessEvent")
            self.logger.info("Enabled WmiMonitorBrightnessEvent watcher for immediate brightness updates.")
        except Exception:
            self.event_watcher = None
            self.logger.warning("Brightness event watcher is unavailable. Falling back to polling only.")

    def read(self) -> Optional[int]:
        try:
            if self.connection is None:
                self._connect()

            assert self.connection is not None
            brightness_entries = [
                item
                for item in self.connection.WmiMonitorBrightness()
                if bool(getattr(item, "Active", False))
            ]
            method_entries = [
                item
                for item in self.connection.WmiMonitorBrightnessMethods()
                if bool(getattr(item, "Active", False))
            ]

            if not brightness_entries:
                self._log_missing_source("No active WmiMonitorBrightness instances were found.")
                return None

            if not method_entries:
                self._log_missing_source("No active WmiMonitorBrightnessMethods instances were found.")
                return None

            method_names = {str(getattr(item, "InstanceName", "")) for item in method_entries}
            for item in brightness_entries:
                if str(getattr(item, "InstanceName", "")) in method_names:
                    return int(item.CurrentBrightness)

            # Fall back to the first active brightness entry if Windows returns unmatched instances.
            return int(brightness_entries[0].CurrentBrightness)
        except Exception:
            self.logger.exception("Failed to read laptop brightness through WMI. Reconnecting.")
            self.connection = None
            self.event_watcher = None
            return None

    def _log_missing_source(self, message: str) -> None:
        now = time.monotonic()
        if now - self.last_missing_log >= 30:
            self.logger.warning(message)
            self.last_missing_log = now

    def supports_events(self) -> bool:
        return self.event_watcher is not None

    def wait_for_change(self, timeout_ms: int) -> Optional[int]:
        if self.event_watcher is None:
            return None

        try:
            event = self.event_watcher(timeout_ms)
            if bool(getattr(event, "Active", False)):
                return int(event.Brightness)
            return None
        except wmi.x_wmi_timed_out:
            pythoncom.PumpWaitingMessages()
            return None
        except Exception:
            self.logger.exception("Brightness event watcher failed. Reconnecting.")
            self.connection = None
            self.event_watcher = None
            return None


class ExternalMonitorManager:
    def __init__(self, logger: logging.Logger, preferred_model: str = "", preferred_vendor: str = "") -> None:
        self.logger = logger
        self.preferred_model = preferred_model
        self.preferred_vendor = preferred_vendor
        self.detected: list[DetectedMonitor] = []
        self.selected: list[DetectedMonitor] = []
        self.last_signature = ""

    def refresh(self) -> bool:
        changed = False
        discovered: list[DetectedMonitor] = []

        try:
            raw_monitors = list(get_monitors())
        except Exception:
            self.logger.exception("Failed to enumerate DDC/CI monitors.")
            raw_monitors = []

        for raw_monitor in raw_monitors:
            description = str(getattr(raw_monitor.vcp, "description", "Unknown monitor")).strip()
            model = ""
            input_source = "Unknown"
            luminance_max = 100
            contrast_max = 100

            try:
                with raw_monitor:
                    try:
                        capabilities = raw_monitor.get_vcp_capabilities()
                        model = str(capabilities.get("model", "")).strip()
                    except Exception:
                        model = ""

                    try:
                        input_code = raw_monitor.get_input_source()
                        input_source = self._format_input_source(input_code)
                    except Exception:
                        input_source = "Unknown"

                    # Query the current and maximum luminance so brightness can be
                    # scaled to the exact range the external monitor reports.
                    _, luminance_max = raw_monitor.vcp.get_vcp_feature(vcp_codes.image_luminance.value)
                    if luminance_max <= 0:
                        luminance_max = 100
                    try:
                        _, contrast_max = raw_monitor.vcp.get_vcp_feature(vcp_codes.image_contrast.value)
                        if contrast_max <= 0:
                            contrast_max = 100
                    except Exception:
                        contrast_max = 100
            except Exception as exc:
                self.logger.warning(
                    "Skipping monitor that did not respond to DDC/CI luminance queries: %s (%s)",
                    description,
                    exc,
                )
                continue

            preferred = self._is_preferred(description, model, input_source)
            discovered.append(
                DetectedMonitor(
                    monitor=raw_monitor,
                    description=description,
                    model=model,
                    input_source=input_source,
                    luminance_max=luminance_max,
                    contrast_max=contrast_max,
                    preferred=preferred,
                )
            )

        selected = self._select_targets(discovered)
        for monitor in discovered:
            monitor.selected = monitor in selected

        signature = "||".join(
            sorted(f"{monitor.identity}|selected={monitor.selected}" for monitor in discovered)
        )
        changed = signature != self.last_signature

        self.detected = discovered
        self.selected = selected

        if changed:
            self.last_signature = signature
            self._log_detection_status()

        return changed

    def _select_targets(self, monitors: list[DetectedMonitor]) -> list[DetectedMonitor]:
        preferred = [monitor for monitor in monitors if monitor.preferred]
        if preferred:
            return preferred
        return monitors

    def _is_preferred(self, description: str, model: str, input_source: str) -> bool:
        # Universal mode: when no preference is configured, treat all monitors equally.
        if not self.preferred_model and not self.preferred_vendor:
            return False
        combined = f"{description} {model}".upper()
        has_model = self.preferred_model and self.preferred_model.upper() in combined
        has_vendor = self.preferred_vendor and self.preferred_vendor.upper() in combined
        if has_model:
            return True
        if has_vendor:
            return True
        return False

    def _format_input_source(self, input_code: int) -> str:
        try:
            return InputSource(input_code).name
        except Exception:
            return str(input_code)

    def _log_detection_status(self) -> None:
        if not self.detected:
            self.logger.warning("No DDC/CI-compatible external monitors are currently available.")
            return

        self.logger.info("Detected %d DDC/CI monitor(s).", len(self.detected))
        for monitor in self.detected:
            self.logger.info(
                "Monitor detected: selected=%s model=%s input=%s max_luminance=%s max_contrast=%s description=%s",
                monitor.selected,
                monitor.model or "Unknown",
                monitor.input_source,
                monitor.luminance_max,
                monitor.contrast_max,
                monitor.description,
            )


class BrightnessSyncApp:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self.logger = self._setup_logging()
        self.config_store = ConfigStore(CONFIG_PATH)
        self.is_first_run = not CONFIG_PATH.exists()
        self.config = self.config_store.load()
        self.config_store.save(self.config)
        self.startup_manager = StartupManager(self.logger)
        self.monitor_manager = ExternalMonitorManager(
            self.logger,
            preferred_model=self.config.preferred_monitor_model,
            preferred_vendor=self.config.preferred_monitor_vendor,
        )
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, name="brightness-sync-worker", daemon=True)
        self.state_lock = threading.Lock()
        self.icon = pystray.Icon(APP_NAME, self._create_tray_image(), APP_NAME, menu=self._build_menu())
        self.reader: Optional[LaptopBrightnessReader] = None
        self.control_window: Optional[ManualControlWindow] = None
        self.last_seen_laptop_brightness: Optional[int] = None
        self.last_applied_brightness: Optional[int] = None
        self.last_applied_signature = ""
        self.last_applied_mode = ""
        self.last_monitor_refresh = 0.0
        self.last_wmi_poll = 0.0
        self.pending_transition = False
        self.applied_monitor_levels: dict[str, dict[str, int]] = {}
        self.monitor_failure_counts: dict[str, int] = {}
        self.monitor_disabled_until: dict[str, float] = {}
        self.last_brightness_change_time = 0.0
        self.monitors_changed_since_last_read = True

    def run(self) -> None:
        self.logger.info("Starting %s.", APP_NAME)
        self.startup_manager.set_enabled(self.config.start_with_windows)
        self.startup_manager.create_or_update_shortcut(self.config.hotkey_start)
        self._maybe_show_first_run_notice()

        # Register global hotkeys.
        self.hotkey_manager = HotkeyManager(self.logger)
        if self.config.hotkey_toggle:
            self.hotkey_manager.register(HOTKEY_TOGGLE, self.config.hotkey_toggle, self.toggle_sync)
        if self.config.hotkey_quit:
            self.hotkey_manager.register(HOTKEY_QUIT, self.config.hotkey_quit, self.quit_app)
        self.hotkey_manager.start()

        self.worker.start()
        self.icon.run()

    def shutdown(self) -> None:
        if hasattr(self, "hotkey_manager"):
            self.hotkey_manager.stop()
        self.stop_event.set()
        if self.worker.is_alive():
            self.worker.join(timeout=5)
        self.logger.info("Stopped %s.", APP_NAME)

    def toggle_sync(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        del icon, item
        with self.state_lock:
            self.config.enabled = not self.config.enabled
            self.config_store.save(self.config)

            if not self.config.enabled:
                self.last_applied_brightness = None
                self.last_applied_signature = ""
                self.last_applied_mode = ""
                self.pending_transition = False
                self.applied_monitor_levels.clear()

        state = "enabled" if self.config.enabled else "disabled"
        self.logger.info("Synchronization %s.", state)
        self._refresh_tray_menu()

    def restore_safe_defaults(
        self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None,
    ) -> None:
        del icon, item
        with self.state_lock:
            start_with_windows = self.config.start_with_windows
            notice_shown = self.config.first_run_notice_shown
            self.config = AppConfig(
                start_with_windows=start_with_windows,
                first_run_notice_shown=notice_shown,
            )
            self.config_store.save(self.config)
            self.last_applied_brightness = None
            self.last_applied_signature = ""
            self.last_applied_mode = ""
            self.pending_transition = False
            self.applied_monitor_levels.clear()
            self.monitor_failure_counts.clear()
            self.monitor_disabled_until.clear()
            self.monitors_changed_since_last_read = True
            self.last_monitor_refresh = 0.0

        self.logger.info("Restored safe defaults from the tray menu.")
        self._refresh_tray_menu()

    def toggle_startup(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        del icon, item
        with self.state_lock:
            self.config.start_with_windows = not self.config.start_with_windows
            self.config_store.save(self.config)
            self.startup_manager.set_enabled(self.config.start_with_windows)

        state = "enabled" if self.config.start_with_windows else "disabled"
        self.logger.info("Windows startup %s from the tray menu.", state)
        self._refresh_tray_menu()

    def open_log_folder(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        del icon, item
        os.startfile(str(LOG_DIR))

    def open_manual_controls(
        self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None,
    ) -> None:
        del icon, item
        if self.control_window is None:
            self.control_window = ManualControlWindow(self)
        self.control_window.show()

    def quit_app(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        del item
        self.stop_event.set()
        if self.control_window is not None:
            self.control_window.close()
        if icon is not None:
            icon.stop()
        elif hasattr(self, "icon") and self.icon is not None:
            self.icon.stop()

    def set_manual_mode(self, enabled: bool) -> None:
        with self.state_lock:
            if self.config.manual_mode == enabled:
                return
            self.config.manual_mode = enabled
            self.config_store.save(self.config)
            self.last_applied_brightness = None
            self.last_applied_signature = ""
            self.last_applied_mode = ""
            self.pending_transition = False
            self.applied_monitor_levels.clear()
            self.last_monitor_refresh = 0.0

        self.logger.info("Manual monitor control %s.", "enabled" if enabled else "disabled")
        self._refresh_tray_menu()

    def set_manual_brightness(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        with self.state_lock:
            if self.config.manual_brightness == value:
                return
            self.config.manual_brightness = value
            self.config_store.save(self.config)
            self.last_applied_brightness = None
            self.last_applied_signature = ""
            self.last_applied_mode = ""
            self.pending_transition = False

        self.logger.info("Manual brightness set to %d%%.", value)

    def set_manual_contrast(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        with self.state_lock:
            if self.config.manual_contrast == value:
                return
            self.config.manual_contrast = value
            self.config_store.save(self.config)
            self.last_applied_brightness = None
            self.last_applied_signature = ""
            self.last_applied_mode = ""
            self.pending_transition = False

        self.logger.info("Manual contrast set to %d%%.", value)

    def get_control_state(self) -> dict[str, object]:
        with self.state_lock:
            selected_count = len(self._available_targets())
            detected_count = len(self.monitor_manager.detected)
            return {
                "enabled": self.config.enabled,
                "manual_mode": self.config.manual_mode,
                "manual_brightness": self.config.manual_brightness,
                "manual_contrast": self.config.manual_contrast,
                "selected_count": selected_count,
                "detected_count": detected_count,
            }

    def _maybe_show_first_run_notice(self) -> None:
        if self.config.first_run_notice_shown:
            return

        self.config.first_run_notice_shown = True
        self.config_store.save(self.config)

        message = (
            "Brightness Sync changes external monitor brightness using standard DDC/CI controls.\n\n"
            "Safe defaults are enabled for first use:\n"
            "- Brightness is limited to a conservative range\n"
            "- Contrast changes are disabled by default\n"
            "- Unsupported monitors are skipped automatically\n\n"
            "Use only on monitors with DDC/CI enabled."
        )
        def show_box() -> None:
            try:
                ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
            except Exception:
                self.logger.warning("Unable to show first-run safety notice.")

        threading.Thread(target=show_box, name="first-run-notice", daemon=True).start()

    def _worker_loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            while not self.stop_event.is_set():
                if self.reader is None:
                    try:
                        self.reader = LaptopBrightnessReader(self.logger)
                    except Exception:
                        self.logger.exception("Failed to initialize the WMI brightness reader.")

                try:
                    brightness_override = None
                    if self.pending_transition:
                        # During smooth transitions, cycle as fast as possible.
                        wait_ms = 40
                    else:
                        wait_ms = 200

                    if self.reader is not None and self.reader.supports_events():
                        brightness_override = self.reader.wait_for_change(wait_ms)
                    else:
                        wait_seconds = wait_ms / 1000.0
                        if self.stop_event.wait(wait_seconds):
                            break

                    self._sync_once(brightness_override=brightness_override)
                except Exception:
                    self.logger.exception("Unexpected error inside the synchronization loop.")
        finally:
            pythoncom.CoUninitialize()

    def _sync_once(self, brightness_override: Optional[int] = None) -> None:
        with self.state_lock:
            manual_mode = self.config.manual_mode
            manual_brightness = self.config.manual_brightness
            manual_contrast = self.config.manual_contrast
            enabled = self.config.enabled

        if not enabled:
            return

        # When monitors are already working, refresh on a slow cadence (30s).
        # When no monitors are available, retry faster (5s).
        # When a brightness event just fired and we have monitors, skip refresh
        # entirely so the event goes straight to the DDC/CI write.
        now = time.monotonic()
        if brightness_override is not None and self.monitor_manager.selected:
            refresh_needed = False
        elif self.monitor_manager.selected:
            refresh_needed = (now - self.last_monitor_refresh) >= MONITOR_REFRESH_SECONDS
        else:
            refresh_needed = (now - self.last_monitor_refresh) >= MONITOR_RETRY_SECONDS

        if refresh_needed:
            monitors_changed = self.monitor_manager.refresh()
            self.last_monitor_refresh = now
            if monitors_changed:
                self.monitors_changed_since_last_read = True
        else:
            monitors_changed = False

        should_poll_wmi = (
            not manual_mode
            and self.reader is not None
            and (brightness_override is not None or (now - self.last_wmi_poll) >= POLL_INTERVAL_SECONDS)
        )
        if manual_mode:
            brightness = manual_brightness
        elif brightness_override is not None:
            brightness = brightness_override
        elif should_poll_wmi and self.reader is not None:
            brightness = self.reader.read()
            self.last_wmi_poll = now
        else:
            brightness = self.last_seen_laptop_brightness
        if brightness is None:
            if not manual_mode:
                return
            brightness = manual_brightness

        # Track when brightness actually changes for adaptive polling.
        if brightness != self.last_seen_laptop_brightness:
            self.last_brightness_change_time = now

        if manual_mode:
            brightness = manual_brightness
        else:
            self.last_seen_laptop_brightness = brightness

        available_targets = self._available_targets()
        if not available_targets:
            return

        selected_signature = "||".join(sorted(monitor.identity for monitor in available_targets))
        mode_signature = f"manual:{manual_mode}|contrast:{manual_contrast if manual_mode else self.config.monitor_contrast}"
        should_apply = (
            monitors_changed
            or brightness != self.last_applied_brightness
            or selected_signature != self.last_applied_signature
            or mode_signature != self.last_applied_mode
            or self.pending_transition
        )

        if not should_apply:
            return

        # Apply event-driven laptop brightness changes immediately so
        # keyboard brightness steps feel instant on the external monitor.
        is_jump = manual_mode or brightness_override is not None
        if self.last_applied_brightness is not None and self.config.smooth_jump_threshold > 0:
            delta = abs(brightness - self.last_applied_brightness)
            if delta >= self.config.smooth_jump_threshold:
                is_jump = True

        applied_successfully, reached_target = self._apply_brightness_to_selected_monitors(
            brightness, manual_mode=manual_mode, manual_contrast=manual_contrast, is_jump=is_jump,
        )
        if applied_successfully:
            self.pending_transition = not reached_target
            if reached_target:
                self.last_applied_brightness = brightness
                self.last_applied_signature = selected_signature
                self.last_applied_mode = mode_signature
        else:
            # Try rediscovery on the next pass when a monitor rejects the update.
            self.pending_transition = False
            self.last_monitor_refresh = 0.0

    def _apply_brightness_to_selected_monitors(
        self,
        brightness: int,
        manual_mode: bool = False,
        manual_contrast: Optional[int] = None,
        is_jump: bool = False,
    ) -> tuple[bool, bool]:
        success = True
        reached_target = True
        skip_read = bool(self.applied_monitor_levels) and not self.monitors_changed_since_last_read
        available_targets = self._available_targets()
        if not available_targets:
            self.monitors_changed_since_last_read = False
            return False, True

        for detected in available_targets:
            try:
                with detected.monitor:
                    target_brightness = self._map_laptop_percent_to_monitor_value(
                        brightness,
                        detected.luminance_max,
                    )
                    brightness_result = self._apply_stepped_vcp_value(
                        detected=detected,
                        level_key="brightness",
                        current_and_max_getter=lambda: detected.monitor.vcp.get_vcp_feature(
                            vcp_codes.image_luminance.value
                        ),
                        setter=detected.monitor.set_luminance,
                        target_value=target_brightness,
                        max_attr="luminance_max",
                        skip_read=skip_read,
                        force_target=is_jump,
                    )

                    contrast_result = self._apply_monitor_contrast(
                        detected,
                        brightness,
                        manual_mode=manual_mode,
                        manual_contrast=manual_contrast,
                        skip_read=skip_read,
                        force_target=is_jump,
                    )
                    reached_target = reached_target and brightness_result and contrast_result

                self._clear_monitor_failure(detected)

                if brightness != self.last_applied_brightness:
                    self.logger.info(
                        "Applied laptop %d%% -> monitor brightness target %d/%d to %s",
                        brightness,
                        target_brightness,
                        detected.luminance_max,
                        detected.label,
                    )
            except (VCPError, ValueError, OSError) as exc:
                success = False
                self._mark_monitor_failure(detected, exc)
            except Exception as exc:
                success = False
                self._mark_monitor_failure(detected, exc)
        self.monitors_changed_since_last_read = False
        return success, reached_target

    def _apply_monitor_contrast(
        self,
        detected: DetectedMonitor,
        percent: int,
        manual_mode: bool = False,
        manual_contrast: Optional[int] = None,
        skip_read: bool = False, force_target: bool = False,
    ) -> bool:
        if manual_mode:
            target_contrast = self._map_monitor_percent_to_monitor_value(
                manual_contrast if manual_contrast is not None else self.config.manual_contrast,
                detected.contrast_max,
            )
        elif not self.config.manage_contrast and not self.config.sync_contrast_to_brightness:
            return True

        try:
            if not manual_mode and self.config.sync_contrast_to_brightness:
                target_contrast = self._map_laptop_percent_to_monitor_value(
                    percent,
                    detected.contrast_max,
                )
            elif not manual_mode:
                target_contrast = self._map_monitor_percent_to_monitor_value(
                    self.config.monitor_contrast,
                    detected.contrast_max,
                )
            reached_target = self._apply_stepped_vcp_value(
                detected=detected,
                level_key="contrast",
                current_and_max_getter=lambda: detected.monitor.vcp.get_vcp_feature(
                    vcp_codes.image_contrast.value
                ),
                setter=detected.monitor.set_contrast,
                target_value=target_contrast,
                max_attr="contrast_max",
                skip_read=skip_read,
                force_target=force_target,
            )
            return reached_target
        except (VCPError, ValueError, OSError) as exc:
            self.logger.warning("Failed to update monitor contrast for %s: %s", detected.label, exc)
            return False
        except Exception as exc:
            self.logger.warning("Unexpected error while updating contrast for %s: %s", detected.label, exc)
            return False

    def _available_targets(self) -> list[DetectedMonitor]:
        available = [
            monitor for monitor in self.monitor_manager.selected if not self._is_monitor_temporarily_disabled(monitor)
        ]
        return available

    def _is_monitor_temporarily_disabled(self, detected: DetectedMonitor) -> bool:
        disabled_until = self.monitor_disabled_until.get(detected.identity, 0.0)
        return disabled_until > time.monotonic()

    def _mark_monitor_failure(self, detected: DetectedMonitor, exc: Exception) -> None:
        count = self.monitor_failure_counts.get(detected.identity, 0) + 1
        self.monitor_failure_counts[detected.identity] = count
        self.logger.warning(
            "Failed to update monitor %s (%d/%d): %s",
            detected.label,
            count,
            MONITOR_FAILURE_DISABLE_COUNT,
            exc,
        )
        if count >= MONITOR_FAILURE_DISABLE_COUNT:
            self.monitor_disabled_until[detected.identity] = time.monotonic() + MONITOR_FAILURE_COOLDOWN_SECONDS
            self.monitor_failure_counts[detected.identity] = 0
            self.applied_monitor_levels.pop(detected.identity, None)
            self.logger.warning(
                "Temporarily disabled monitor updates for %s for %.0f seconds.",
                detected.label,
                MONITOR_FAILURE_COOLDOWN_SECONDS,
            )

    def _clear_monitor_failure(self, detected: DetectedMonitor) -> None:
        self.monitor_failure_counts.pop(detected.identity, None)
        self.monitor_disabled_until.pop(detected.identity, None)

    def _apply_stepped_vcp_value(
        self,
        detected: DetectedMonitor,
        level_key: str,
        current_and_max_getter,
        setter,
        target_value: int,
        max_attr: str,
        skip_read: bool = False,
        force_target: bool = False,
    ) -> bool:
        monitor_state = self.applied_monitor_levels.setdefault(detected.identity, {})

        if skip_read and level_key in monitor_state:
            current_value = monitor_state[level_key]
        else:
            current_value, reported_max = current_and_max_getter()
            if reported_max > 0:
                setattr(detected, max_attr, reported_max)

        known_current = monitor_state.get(level_key, current_value)
        actual_current = (
            current_value
            if abs(current_value - known_current) > max(1, self.config.smooth_step * 2)
            else known_current
        )

        if force_target:
            next_value = target_value
        else:
            next_value = self._next_transition_value(actual_current, target_value)

        if next_value != actual_current:
            setter(next_value)
            monitor_state[level_key] = next_value
        else:
            monitor_state[level_key] = actual_current

        return next_value == target_value

    def _map_laptop_percent_to_monitor_value(self, laptop_percent: int, monitor_max: int) -> int:
        laptop_percent = max(0, min(100, int(laptop_percent)))
        monitor_max = max(1, int(monitor_max))
        min_percent = max(0, min(100, int(self.config.monitor_brightness_min)))
        max_percent = max(min_percent, min(100, int(self.config.monitor_brightness_max)))
        gamma = max(0.2, min(3.0, float(self.config.monitor_brightness_gamma)))

        normalized = math.pow(laptop_percent / 100.0, gamma)
        mapped_percent = min_percent + ((max_percent - min_percent) * normalized)
        return self._map_monitor_percent_to_monitor_value(mapped_percent, monitor_max)

    def _map_monitor_percent_to_monitor_value(self, percent: float, monitor_max: int) -> int:
        clamped_percent = max(0.0, min(100.0, float(percent)))
        monitor_max = max(1, int(monitor_max))
        return int(round((clamped_percent / 100.0) * monitor_max))

    def _next_transition_value(self, current_value: int, target_value: int) -> int:
        if not self.config.smooth_transitions:
            return target_value

        step = max(1, int(self.config.smooth_step))
        if current_value < target_value:
            return min(current_value + step, target_value)
        if current_value > target_value:
            return max(current_value - step, target_value)
        return target_value

    def _build_menu(self) -> pystray.Menu:
        toggle_label = (
            f"Enable synchronization ({self.config.hotkey_toggle.upper()})"
            if self.config.hotkey_toggle
            else "Enable synchronization"
        )
        quit_label = (
            f"Quit ({self.config.hotkey_quit.upper()})"
            if self.config.hotkey_quit
            else "Quit"
        )
        return pystray.Menu(
            pystray.MenuItem(
                toggle_label,
                self.toggle_sync,
                checked=lambda item: self.config.enabled,
            ),
            pystray.MenuItem("Open brightness controls", self.open_manual_controls),
            pystray.MenuItem(
                "Manual monitor control",
                lambda icon, item: self.set_manual_mode(not self.config.manual_mode),
                checked=lambda item: self.config.manual_mode,
            ),
            pystray.MenuItem("Restore safe defaults", self.restore_safe_defaults),
            pystray.MenuItem(
                "Start with Windows",
                self.toggle_startup,
                checked=lambda item: self.config.start_with_windows,
            ),
            pystray.MenuItem("Open log folder", self.open_log_folder),
            pystray.MenuItem(quit_label, self.quit_app),
        )

    def _refresh_tray_menu(self) -> None:
        self.icon.menu = self._build_menu()
        self.icon.update_menu()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger(APP_NAME)
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )

        handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    def _create_tray_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((10, 10, 54, 54), fill=(38, 167, 255, 255))
        draw.ellipse((24, 24, 40, 40), fill=(255, 255, 255, 255))

        rays = [
            ((32, 4), (32, 16)),
            ((32, 48), (32, 60)),
            ((4, 32), (16, 32)),
            ((48, 32), (60, 32)),
            ((11, 11), (19, 19)),
            ((45, 45), (53, 53)),
            ((45, 19), (53, 11)),
            ((11, 53), (19, 45)),
        ]
        for start, end in rays:
            draw.line((start, end), fill=(38, 167, 255, 255), width=4)
        return image


class ManualControlWindow:
    def __init__(self, app: BrightnessSyncApp) -> None:
        self.app = app
        self.thread = threading.Thread(target=self._run, name="manual-control-window", daemon=True)
        self.ready = threading.Event()
        self.root: tk.Tk | None = None
        self.status_var: tk.StringVar | None = None
        self.mode_var: tk.BooleanVar | None = None
        self.brightness_var: tk.IntVar | None = None
        self.contrast_var: tk.IntVar | None = None
        self._updating_ui = False
        self.thread.start()
        self.ready.wait(timeout=5)

    def show(self) -> None:
        if self.root is None:
            return
        self.root.after(0, self._show_window)

    def close(self) -> None:
        if self.root is None:
            return
        self.root.after(0, self.root.destroy)

    def _run(self) -> None:
        root = tk.Tk()
        root.title("Brightness Controls")
        root.geometry("380x250")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._hide_window)

        container = ttk.Frame(root, padding=14)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="External monitor controls",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            container,
            text="Use manual mode when the laptop screen is off or you want PC-only control.",
            wraplength=340,
        ).pack(anchor="w", pady=(6, 12))

        self.mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            container,
            text="Enable manual monitor control",
            variable=self.mode_var,
            command=self._on_mode_changed,
        ).pack(anchor="w", pady=(0, 12))

        self.brightness_var = tk.IntVar(value=0)
        ttk.Label(container, text="Brightness").pack(anchor="w")
        brightness_scale = ttk.Scale(
            container,
            from_=0,
            to=100,
            orient="horizontal",
            command=self._on_brightness_changed,
        )
        brightness_scale.pack(fill="x", pady=(2, 10))

        self.contrast_var = tk.IntVar(value=0)
        ttk.Label(container, text="Contrast").pack(anchor="w")
        contrast_scale = ttk.Scale(
            container,
            from_=0,
            to=100,
            orient="horizontal",
            command=self._on_contrast_changed,
        )
        contrast_scale.pack(fill="x", pady=(2, 10))

        self.status_var = tk.StringVar(value="")
        ttk.Label(container, textvariable=self.status_var, wraplength=340).pack(anchor="w", pady=(6, 0))

        self.root = root
        self._brightness_scale = brightness_scale
        self._contrast_scale = contrast_scale
        self.ready.set()
        self._refresh_from_app()
        root.withdraw()
        root.mainloop()

    def _show_window(self) -> None:
        assert self.root is not None
        self._refresh_from_app()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _hide_window(self) -> None:
        if self.root is not None:
            self.root.withdraw()

    def _refresh_from_app(self) -> None:
        if self.root is None or self.mode_var is None or self.brightness_var is None or self.contrast_var is None:
            return
        state = self.app.get_control_state()
        self._updating_ui = True
        self.mode_var.set(bool(state["manual_mode"]))
        self.brightness_var.set(int(state["manual_brightness"]))
        self.contrast_var.set(int(state["manual_contrast"]))
        self._brightness_scale.set(int(state["manual_brightness"]))
        self._contrast_scale.set(int(state["manual_contrast"]))
        manual_enabled = bool(state["manual_mode"])
        scale_state = "normal" if manual_enabled else "disabled"
        self._brightness_scale.state([scale_state] if scale_state == "disabled" else ["!disabled"])
        self._contrast_scale.state([scale_state] if scale_state == "disabled" else ["!disabled"])
        selected_count = int(state["selected_count"])
        detected_count = int(state["detected_count"])
        if not bool(state["enabled"]):
            status = "Synchronization is off. Enable it from the tray icon to apply monitor changes."
        elif detected_count == 0:
            status = "No DDC/CI monitor detected right now. Check the monitor cable and DDC/CI setting."
        elif selected_count == 0:
            status = "A monitor was detected, but it is temporarily unavailable for updates."
        elif manual_enabled:
            status = f"Manual mode is active. Controlling {selected_count} monitor(s) directly."
        else:
            status = "Manual mode is off. The app is following the laptop brightness."
        if self.status_var is not None:
            self.status_var.set(status)
        self._updating_ui = False
        self.root.after(1000, self._refresh_from_app)

    def _on_mode_changed(self) -> None:
        if self._updating_ui or self.mode_var is None:
            return
        self.app.set_manual_mode(bool(self.mode_var.get()))
        self._refresh_from_app()

    def _on_brightness_changed(self, value: str) -> None:
        if self._updating_ui:
            return
        self.app.set_manual_brightness(int(float(value)))

    def _on_contrast_changed(self, value: str) -> None:
        if self._updating_ui:
            return
        self.app.set_manual_contrast(int(float(value)))


def configure_fallback_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"{APP_NAME}.fallback")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def main() -> int:
    guard = SingleInstanceGuard(MUTEX_NAME)
    if not guard.acquire():
        return 0

    try:
        app = BrightnessSyncApp()
        try:
            app.run()
        finally:
            app.shutdown()
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        logger = configure_fallback_logging()
        logger.error("Fatal error in Brightness Sync:\n%s", traceback.format_exc())
        raise






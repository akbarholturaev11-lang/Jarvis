from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from .base import (
    AUTOSTART_LABEL,
    AVAILABLE,
    BROWSER_CATALOG,
    MESSAGING_CATALOG,
    UNKNOWN,
    PlatformAdapter,
)


_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


_BROWSER_EXES = {
    "chrome": ("chrome.exe", "Google/Chrome/Application/chrome.exe"),
    "atlas": ("ChatGPT.exe", "Atlas.exe"),
    "firefox": ("firefox.exe", "Mozilla Firefox/firefox.exe"),
    "edge": ("msedge.exe", "Microsoft/Edge/Application/msedge.exe"),
    "arc": ("Arc.exe",),
    "brave": ("brave.exe", "BraveSoftware/Brave-Browser/Application/brave.exe"),
    "opera": ("opera.exe", "Opera/opera.exe"),
}

_MESSAGING_EXES = {
    "telegram": ("Telegram.exe",),
    "whatsapp": ("WhatsApp.exe",),
    "wechat": ("WeChat.exe",),
    "discord": ("Discord.exe",),
    "slack": ("slack.exe",),
    "signal": ("Signal.exe",),
    "teams": ("ms-teams.exe", "Teams.exe"),
    "zoom": ("Zoom.exe",),
}


class WindowsAdapter(PlatformAdapter):
    os_key = "windows"

    def detect_os_info(self) -> dict[str, Any]:
        info = super().detect_os_info()
        info["version"] = platform.platform()
        return info

    def detect_shell(self) -> str:
        ps_module = os.environ.get("PSModulePath")
        if ps_module:
            return "powershell"
        return super().detect_shell()

    def detect_desktop_session(self) -> dict[str, Any]:
        session = super().detect_desktop_session()
        session.update(
            {
                "gui_available": True,
                "desktop_environment": "windows",
                "session_type": os.environ.get("SESSIONNAME", "desktop"),
                "display_server": "windows",
            }
        )
        return session

    def detect_app_launch(self) -> dict[str, Any]:
        return {
            "supported": True,
            "method": "start/shell",
            "verified": False,
        }

    def detect_browsers(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in BROWSER_CATALOG.items():
            if app_id == "safari":
                records.append(self._app_record(app_id, spec, False, ""))
                continue
            detected = self._find_windows_app(_BROWSER_EXES.get(app_id, ()))
            records.append(self._app_record(app_id, spec, bool(detected), "path" if detected else ""))
        return records

    def detect_default_browser(self, browsers: list[dict[str, Any]] | None = None) -> str:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            )
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0]).lower()
            winreg.CloseKey(key)
            for browser_id in ("edge", "firefox", "opera", "brave", "chrome"):
                if browser_id in prog_id:
                    return browser_id
        except Exception:
            pass
        detected = [item["id"] for item in (browsers or []) if item.get("detected")]
        return detected[0] if detected else UNKNOWN

    def detect_messaging_apps(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in MESSAGING_CATALOG.items():
            if app_id == "messages":
                records.append(self._app_record(app_id, spec, False, ""))
                continue
            detected = self._find_windows_app(_MESSAGING_EXES.get(app_id, ()))
            records.append(self._app_record(app_id, spec, bool(detected), "path" if detected else ""))
        return records

    def detect_media_control(self) -> dict[str, Any]:
        if self._module_available("pyautogui"):
            return {
                "supported": True,
                "method": "windows_media_key",
                "verified": False,
                "status": AVAILABLE,
                "requires_permission": False,
            }
        return {
            "supported": False,
            "method": UNKNOWN,
            "verified": False,
            "status": UNKNOWN,
            "requires_permission": False,
        }

    def detect_active_window(self) -> dict[str, Any]:
        if self._module_available("pygetwindow"):
            return {
                "supported": True,
                "method": "pygetwindow",
                "status": AVAILABLE,
                "requires_permission": False,
            }
        if self._module_available("pywinauto"):
            return {
                "supported": True,
                "method": "pywinauto",
                "status": AVAILABLE,
                "requires_permission": False,
            }
        return {
            "supported": False,
            "method": UNKNOWN,
            "status": UNKNOWN,
            "requires_permission": False,
        }

    def detect_permissions(self) -> dict[str, str]:
        return {
            "microphone": UNKNOWN,
            "camera": UNKNOWN,
            "screen_recording": "not_applicable",
            "accessibility": "not_applicable",
            "automation": UNKNOWN,
            "notifications": UNKNOWN,
        }

    def launch_app(self, app_name: str) -> tuple[bool, str]:
        try:
            subprocess.Popen(
                f"start {app_name}",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"Windows shell start launched {app_name}."
        except Exception as e:
            return False, str(e)

    def media_pause(self, target_app: str = "") -> tuple[bool | None, str]:
        if not self._module_available("pyautogui"):
            return None, "pyautogui is unavailable for Windows media key control."
        try:
            import pyautogui

            pyautogui.press("playpause")
            return None, "Windows media play/pause key sent; playback was not verified."
        except Exception as e:
            return False, str(e)

    def close_app(self, app_name: str) -> tuple[bool | None, str]:
        name = (app_name or "").strip()
        if not name:
            return False, "No application name provided."
        image = name if name.lower().endswith(".exe") else f"{name}.exe"
        taskkill = self._which("taskkill") or "taskkill"
        # No /F → graceful WM_CLOSE request rather than a forced kill.
        proc = self._run([taskkill, "/IM", image, "/T"], timeout=6.0)
        if proc is None:
            return False, "taskkill could not be executed on Windows."
        combined = ((proc.stdout or "") + (proc.stderr or "")).lower()
        if proc.returncode != 0:
            if "not found" in combined or "not running" in combined:
                return True, f"{name} is not running (already closed)."
            return False, (proc.stderr or proc.stdout or "taskkill failed").strip()
        running = self._image_is_running(image)
        if running is False:
            return True, f"{name} closed and verified."
        if running is True:
            return None, f"Close request sent to {name}, but it is still running."
        return None, f"Close request sent to {name}; running state was not verified."

    def _image_is_running(self, image: str) -> bool | None:
        tasklist = self._which("tasklist") or "tasklist"
        proc = self._run([tasklist, "/FI", f"IMAGENAME eq {image}"], timeout=4.0)
        if proc is None or proc.returncode != 0:
            return None
        return image.lower() in (proc.stdout or "").lower()

    def prevent_sleep(self, reason: str = "") -> tuple[object | None, str]:
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            res = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
            if res == 0:
                return None, "SetThreadExecutionState failed on Windows."
            # Token is a sentinel; release_sleep resets the state on the same thread.
            return "win-exec-state", "Windows system sleep prevented (SetThreadExecutionState)."
        except Exception as e:
            return None, f"Keep-awake failed on Windows: {e}"

    def release_sleep(self, token: object) -> None:
        if token == "win-exec-state":
            try:
                import ctypes

                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
            return
        super().release_sleep(token)

    # ── auto-start (HKCU Run registry entry) ──────────────────────────────────
    def autostart_status(self, label: str = AUTOSTART_LABEL) -> tuple[bool | None, str]:
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_READ
            ) as key:
                try:
                    winreg.QueryValueEx(key, label)
                    return True, "Auto-start Run entry is registered."
                except FileNotFoundError:
                    return False, "Auto-start Run entry is not registered."
        except Exception as e:
            return None, f"Auto-start status unavailable on Windows: {e}"

    def set_autostart(
        self,
        enabled: bool,
        command: list[str],
        label: str = AUTOSTART_LABEL,
    ) -> tuple[bool | None, str]:
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_ALL_ACCESS
            ) as key:
                if enabled:
                    if not command:
                        return False, "No launch command available for auto-start."
                    value = " ".join(f'"{arg}"' for arg in command)
                    winreg.SetValueEx(key, label, 0, winreg.REG_SZ, value)
                    return True, "Auto-start enabled (Run registry entry)."
                try:
                    winreg.DeleteValue(key, label)
                except FileNotFoundError:
                    pass
                return True, "Auto-start disabled (Run registry entry removed)."
        except Exception as e:
            return False, f"Auto-start change failed on Windows: {e}"

    def _find_windows_app(self, candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            found = self._which(candidate, Path(candidate).stem)
            if found:
                return found

        roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
        ]
        for root in roots:
            if not root:
                continue
            base = Path(root)
            for candidate in candidates:
                path = base / candidate.replace("/", os.sep)
                if path.exists():
                    return str(path)
        return ""

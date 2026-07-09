from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from .base import (
    AVAILABLE,
    BROWSER_CATALOG,
    MESSAGING_CATALOG,
    UNKNOWN,
    PlatformAdapter,
)


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

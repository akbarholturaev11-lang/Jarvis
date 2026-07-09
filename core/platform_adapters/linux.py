from __future__ import annotations

import os
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


_BROWSER_COMMANDS = {
    "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "atlas": ("chatgpt-atlas", "atlas"),
    "firefox": ("firefox",),
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "arc": ("arc",),
    "brave": ("brave-browser", "brave"),
    "opera": ("opera",),
}

_MESSAGING_COMMANDS = {
    "telegram": ("telegram-desktop", "telegram"),
    "whatsapp": ("whatsapp-for-linux", "whatsapp"),
    "wechat": ("wechat",),
    "discord": ("discord",),
    "slack": ("slack",),
    "signal": ("signal-desktop", "signal"),
    "teams": ("teams-for-linux", "msteams"),
    "zoom": ("zoom",),
}


class LinuxAdapter(PlatformAdapter):
    os_key = "linux"

    def detect_app_launch(self) -> dict[str, Any]:
        method = "xdg-open" if self._which("xdg-open") else "gtk-launch" if self._which("gtk-launch") else UNKNOWN
        return {
            "supported": method != UNKNOWN,
            "method": method,
            "verified": False,
        }

    def detect_browsers(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in BROWSER_CATALOG.items():
            if app_id == "safari":
                records.append(self._app_record(app_id, spec, False, ""))
                continue
            detected = self._which(*_BROWSER_COMMANDS.get(app_id, ()))
            records.append(self._app_record(app_id, spec, bool(detected), "path" if detected else ""))
        return records

    def detect_default_browser(self, browsers: list[dict[str, Any]] | None = None) -> str:
        proc = self._run(["xdg-settings", "get", "default-web-browser"], timeout=2.0)
        if proc and proc.returncode == 0:
            lowered = proc.stdout.lower()
            for browser_id in ("firefox", "opera", "brave", "chrome", "edge"):
                if browser_id in lowered:
                    return browser_id
        detected = [item["id"] for item in (browsers or []) if item.get("detected")]
        return detected[0] if detected else UNKNOWN

    def detect_messaging_apps(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in MESSAGING_CATALOG.items():
            if app_id == "messages":
                records.append(self._app_record(app_id, spec, False, ""))
                continue
            detected = self._which(*_MESSAGING_COMMANDS.get(app_id, ()))
            records.append(self._app_record(app_id, spec, bool(detected), "path" if detected else ""))
        return records

    def detect_media_control(self) -> dict[str, Any]:
        if self._which("playerctl"):
            return {
                "supported": True,
                "method": "playerctl",
                "verified": False,
                "status": AVAILABLE,
                "requires_permission": False,
            }
        if self._module_available("pyautogui") and self.detect_gui().get("available"):
            return {
                "supported": True,
                "method": "pyautogui_media_key",
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
        if self._which("wmctrl"):
            return {
                "supported": True,
                "method": "wmctrl",
                "status": AVAILABLE,
                "requires_permission": False,
            }
        if self._which("xdotool"):
            return {
                "supported": True,
                "method": "xdotool",
                "status": AVAILABLE,
                "requires_permission": False,
            }
        return {
            "supported": False,
            "method": UNKNOWN,
            "status": UNKNOWN,
            "requires_permission": False,
        }

    def detect_screen_capture(self) -> dict[str, Any]:
        base = super().detect_screen_capture()
        if os.environ.get("WAYLAND_DISPLAY") and base["status"] == AVAILABLE:
            base["status"] = UNKNOWN
            base["requires_permission"] = True
            base["reason"] = "Wayland screen capture may require portal permission."
        return base

    def detect_permissions(self) -> dict[str, str]:
        display_server = self._display_server()
        return {
            "microphone": UNKNOWN,
            "camera": UNKNOWN,
            "screen_recording": UNKNOWN,
            "accessibility": "not_applicable",
            "automation": UNKNOWN,
            "display_server": display_server,
            "wayland_limitations": UNKNOWN if display_server == "wayland" else "not_applicable",
        }

    def launch_app(self, app_name: str) -> tuple[bool, str]:
        binary = self._which(
            app_name,
            app_name.lower(),
            app_name.lower().replace(" ", "-"),
            app_name.lower().replace(" ", "_"),
        )
        try:
            if binary:
                subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Launched {app_name} via executable."
            if self._which("gtk-launch"):
                subprocess.Popen(["gtk-launch", app_name.lower()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Launched {app_name} via gtk-launch."
            if self._which("xdg-open"):
                subprocess.Popen(["xdg-open", app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Launched {app_name} via xdg-open."
        except Exception as e:
            return False, str(e)
        return False, f"No Linux launcher found for {app_name}."

    def media_pause(self, target_app: str = "") -> tuple[bool | None, str]:
        if self._which("playerctl"):
            proc = self._run(["playerctl", "play-pause"], timeout=2.0)
            if proc and proc.returncode == 0:
                return None, "playerctl play-pause sent; playback was not verified."
            detail = (proc.stderr if proc else "") or "playerctl failed"
            return False, detail.strip()
        if not self._module_available("pyautogui"):
            return None, "No playerctl or pyautogui media control is available."
        try:
            import pyautogui

            pyautogui.press("playpause")
            return None, "pyautogui playpause sent; playback was not verified."
        except Exception as e:
            return False, str(e)

from __future__ import annotations

import os
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

    def close_app(self, app_name: str) -> tuple[bool | None, str]:
        name = (app_name or "").strip()
        if not name:
            return False, "No application name provided."
        import time

        # Prefer a graceful window close via wmctrl.
        if self._which("wmctrl"):
            proc = self._run(["wmctrl", "-c", name], timeout=4.0)
            if proc and proc.returncode == 0:
                time.sleep(0.4)
                running = self._process_running(name)
                if running is False:
                    return True, f"{name} closed and verified via wmctrl."
                if running is True:
                    return None, f"Close request sent to {name} via wmctrl, but it is still running."
                return None, f"Close request sent to {name} via wmctrl; running state was not verified."
        # Fallback: graceful SIGTERM via pkill (never SIGKILL by default).
        pkill = self._which("pkill")
        if pkill:
            proc = self._run([pkill, "-TERM", "-f", name], timeout=4.0)
            if proc is not None and proc.returncode in (0, 1):
                time.sleep(0.4)
                running = self._process_running(name)
                if running is False:
                    return True, f"{name} terminated and verified (SIGTERM)."
                if running is True:
                    return None, f"SIGTERM sent to {name}, but it is still running."
                return None, f"SIGTERM sent to {name}; running state was not verified."
            return False, "pkill could not terminate the process on Linux."
        return False, "No wmctrl or pkill available for app close on Linux."

    def _process_running(self, name: str) -> bool | None:
        pgrep = self._which("pgrep")
        if not pgrep:
            return None
        proc = self._run([pgrep, "-f", name], timeout=2.0)
        if proc is None:
            return None
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        return None

    def prevent_sleep(self, reason: str = "") -> tuple[object | None, str]:
        exe = self._which("systemd-inhibit")
        if not exe:
            return None, "systemd-inhibit not found on Linux."
        try:
            proc = subprocess.Popen(
                [
                    exe,
                    "--what=sleep:idle",
                    f"--why={reason or 'JARVIS remote session'}",
                    "--mode=block",
                    "sleep", "infinity",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc, "Linux sleep inhibited (systemd-inhibit)."
        except Exception as e:
            return None, f"Failed to start systemd-inhibit: {e}"

    # ── auto-start (XDG ~/.config/autostart/<label>.desktop) ──────────────────
    def autostart_status(self, label: str = AUTOSTART_LABEL) -> tuple[bool | None, str]:
        entry = self._autostart_entry_path(label)
        if entry.exists():
            return True, f"Auto-start entry registered at {entry.name}."
        return False, "Auto-start .desktop entry is not registered."

    def set_autostart(
        self,
        enabled: bool,
        command: list[str],
        label: str = AUTOSTART_LABEL,
    ) -> tuple[bool | None, str]:
        entry = self._autostart_entry_path(label)
        try:
            if enabled:
                if not command:
                    return False, "No launch command available for auto-start."
                entry.parent.mkdir(parents=True, exist_ok=True)
                exec_line = " ".join(self._shell_quote(arg) for arg in command)
                entry.write_text(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=JARVIS\n"
                    f"Exec={exec_line}\n"
                    "Terminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n",
                    encoding="utf-8",
                )
                if entry.exists():
                    return True, f"Auto-start enabled ({entry.name})."
                return None, "Auto-start entry write could not be verified."
            entry.unlink(missing_ok=True)
            if not entry.exists():
                return True, "Auto-start disabled (.desktop entry removed)."
            return None, "Auto-start entry removal could not be verified."
        except Exception as e:
            return False, f"Auto-start change failed on Linux: {e}"

    def _autostart_entry_path(self, label: str) -> Path:
        return Path.home() / ".config" / "autostart" / f"{label}.desktop"

    @staticmethod
    def _shell_quote(arg: str) -> str:
        import shlex

        return shlex.quote(str(arg))

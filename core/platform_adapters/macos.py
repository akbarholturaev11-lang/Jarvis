from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any

from .base import (
    AUTOSTART_LABEL,
    AVAILABLE,
    BROWSER_CATALOG,
    MESSAGING_CATALOG,
    PERMISSION_REQUIRED,
    UNKNOWN,
    PlatformAdapter,
)


_BROWSER_BUNDLES = {
    "safari": ("com.apple.Safari",),
    "chrome": ("com.google.Chrome",),
    "atlas": ("com.openai.chatgpt", "com.openai.ChatGPT", "com.openai.atlas"),
    "firefox": ("org.mozilla.firefox",),
    "edge": ("com.microsoft.edgemac",),
    "arc": ("company.thebrowser.Browser",),
    "brave": ("com.brave.Browser",),
    "opera": ("com.operasoftware.Opera",),
}

_DEFAULT_BROWSER_BUNDLE_MAP = {
    "com.apple.safari": "safari",
    "com.google.chrome": "chrome",
    "org.mozilla.firefox": "firefox",
    "com.microsoft.edgemac": "edge",
    "company.thebrowser.browser": "arc",
    "com.brave.browser": "brave",
    "com.operasoftware.opera": "opera",
}


class MacOSAdapter(PlatformAdapter):
    os_key = "macos"

    def detect_os_info(self) -> dict[str, Any]:
        info = super().detect_os_info()
        version, _, _ = platform.mac_ver()
        info["version"] = version or platform.platform()
        return info

    def detect_shell(self) -> str:
        return super().detect_shell()

    def detect_desktop_session(self) -> dict[str, Any]:
        session = super().detect_desktop_session()
        session.update(
            {
                "gui_available": True,
                "desktop_environment": "aqua",
                "session_type": "gui",
                "display_server": "quartz",
            }
        )
        return session

    def detect_app_launch(self) -> dict[str, Any]:
        return {
            "supported": bool(self._which("open")),
            "method": "open -a",
            "verified": False,
        }

    def detect_browsers(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in BROWSER_CATALOG.items():
            detected = self._mac_app_exists(spec["launch_name"])
            source = "applications" if detected else ""
            records.append(self._app_record(app_id, spec, detected, source))
        return records

    def detect_default_browser(self, browsers: list[dict[str, Any]] | None = None) -> str:
        proc = self._run(
            [
                "defaults",
                "read",
                "com.apple.LaunchServices/com.apple.launchservices.secure",
                "LSHandlers",
            ],
            timeout=3.0,
        )
        if proc and proc.returncode == 0:
            blocks = proc.stdout.split("{")
            for block in blocks:
                lowered = block.lower()
                if "lshandlerurlscheme = https" not in lowered:
                    continue
                for bundle_id, browser_id in _DEFAULT_BROWSER_BUNDLE_MAP.items():
                    if bundle_id in lowered:
                        return browser_id

        detected = [item["id"] for item in (browsers or []) if item.get("detected")]
        if "safari" in detected:
            return "safari"
        return detected[0] if detected else UNKNOWN

    def detect_messaging_apps(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for app_id, spec in MESSAGING_CATALOG.items():
            detected = self._mac_app_exists(spec["launch_name"])
            source = "applications" if detected else ""
            records.append(self._app_record(app_id, spec, detected, source))
        return records

    def detect_media_control(self) -> dict[str, Any]:
        supported = bool(self._which("osascript"))
        return {
            "supported": supported,
            "method": "system_events_media_key" if supported else UNKNOWN,
            "verified": False,
            "status": AVAILABLE if supported else UNKNOWN,
            "requires_permission": True,
        }

    def detect_active_window(self) -> dict[str, Any]:
        supported = bool(self._which("osascript"))
        return {
            "supported": supported,
            "method": "applescript_system_events" if supported else UNKNOWN,
            "status": PERMISSION_REQUIRED if supported else UNKNOWN,
            "requires_permission": True,
        }

    def detect_screen_capture(self) -> dict[str, Any]:
        base = super().detect_screen_capture()
        if base["status"] == AVAILABLE:
            base["requires_permission"] = True
            base["permission"] = "Screen Recording"
        return base

    def detect_camera(self) -> dict[str, Any]:
        base = super().detect_camera()
        base["requires_permission"] = True
        base["permission"] = "Camera"
        return base

    def detect_ui_automation(self) -> dict[str, Any]:
        if self._module_available("pyautogui") or self._which("osascript"):
            return {
                "status": PERMISSION_REQUIRED,
                "method": "pyautogui/applescript",
                "requires_permission": True,
            }
        return super().detect_ui_automation()

    def detect_permissions(self) -> dict[str, str]:
        return {
            "microphone": UNKNOWN,
            "camera": UNKNOWN,
            "screen_recording": UNKNOWN,
            "accessibility": UNKNOWN,
            "automation": UNKNOWN,
        }

    def launch_app(self, app_name: str) -> tuple[bool, str]:
        proc = self._run(["open", "-a", app_name], timeout=8.0)
        if proc and proc.returncode == 0:
            return True, f"open -a launched {app_name}."
        detail = (proc.stderr if proc else "") or "open command failed"
        return False, detail.strip()

    def get_active_app(self) -> str:
        proc = self._run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            timeout=1.0,
        )
        if proc and proc.returncode == 0:
            return " ".join(proc.stdout.split())
        return ""

    def media_pause(self, target_app: str = "") -> tuple[bool | None, str]:
        proc = self._run(
            ["osascript", "-e", 'tell application "System Events" to key code 16'],
            timeout=2.0,
        )
        if proc and proc.returncode == 0:
            return None, "System Events media play/pause key sent; playback was not verified."
        detail = (proc.stderr if proc else "") or "osascript media key failed"
        return False, detail.strip()

    def close_app(self, app_name: str) -> tuple[bool | None, str]:
        name = (app_name or "").strip()
        if not name:
            return False, "No application name provided."
        if not self._which("osascript"):
            return False, "osascript not available for app close on macOS."
        # Graceful quit — asks the app to quit (it may still prompt to save),
        # never a hard SIGKILL.
        quit_proc = self._run(
            ["osascript", "-e", f'tell application "{name}" to quit'],
            timeout=6.0,
        )
        if not quit_proc or quit_proc.returncode != 0:
            detail = (quit_proc.stderr if quit_proc else "") or "osascript quit failed"
            return False, detail.strip()
        # Give the app a moment to terminate, then verify.
        import time

        time.sleep(0.5)
        running = self._app_is_running(name)
        if running is True:
            return None, f"Quit request sent to {name}, but it is still running (it may be prompting to save)."
        if running is False:
            return True, f"{name} quit and verified closed."
        return None, f"Quit request sent to {name}; running state could not be verified."

    def _app_is_running(self, name: str) -> bool | None:
        proc = self._run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to (name of processes) contains "{name}"',
            ],
            timeout=2.0,
        )
        if not proc or proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip().lower()
        if out == "true":
            return True
        if out == "false":
            return False
        return None

    def prevent_sleep(self, reason: str = "") -> tuple[object | None, str]:
        exe = self._which("caffeinate")
        if not exe:
            return None, "caffeinate not found on macOS."
        try:
            # -i: prevent idle sleep, -m: prevent disk idle sleep. No -s, so a manual
            # lid-close on battery still sleeps (client auto-reconnect covers that).
            proc = subprocess.Popen(
                [exe, "-i", "-m"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc, "macOS idle sleep prevented (caffeinate)."
        except Exception as e:
            return None, f"Failed to start caffeinate: {e}"

    # ── auto-start (LaunchAgent) ──────────────────────────────────────────────
    def autostart_status(self, label: str = AUTOSTART_LABEL) -> tuple[bool | None, str]:
        plist = self._launch_agent_path(label)
        if plist.exists():
            return True, f"LaunchAgent registered at {plist.name}."
        return False, "Auto-start LaunchAgent is not registered."

    def set_autostart(
        self,
        enabled: bool,
        command: list[str],
        label: str = AUTOSTART_LABEL,
    ) -> tuple[bool | None, str]:
        plist = self._launch_agent_path(label)
        try:
            if enabled:
                if not command:
                    return False, "No launch command available for auto-start."
                plist.parent.mkdir(parents=True, exist_ok=True)
                plist.write_text(
                    self._build_launch_agent_plist(label, command), encoding="utf-8"
                )
                # Best-effort load; RunAtLoad still fires at next login even if this
                # fails (e.g. running headless), so a load failure is not fatal.
                self._run(["launchctl", "load", "-w", str(plist)], timeout=4.0)
                if plist.exists():
                    return True, f"Auto-start enabled (LaunchAgent {plist.name})."
                return None, "LaunchAgent write could not be verified."
            self._run(["launchctl", "unload", "-w", str(plist)], timeout=4.0)
            plist.unlink(missing_ok=True)
            if not plist.exists():
                return True, "Auto-start disabled (LaunchAgent removed)."
            return None, "LaunchAgent removal could not be verified."
        except Exception as e:
            return False, f"Auto-start change failed: {e}"

    def _launch_agent_path(self, label: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def _build_launch_agent_plist(self, label: str, command: list[str]) -> str:
        from xml.sax.saxutils import escape

        args_xml = "\n".join(
            f"    <string>{escape(str(arg))}</string>" for arg in command
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>{escape(label)}</string>\n'
            '  <key>ProgramArguments</key><array>\n'
            f'{args_xml}\n'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '</dict></plist>\n'
        )

    def _mac_app_exists(self, app_name: str) -> bool:
        candidates = [
            Path("/Applications") / f"{app_name}.app",
            Path.home() / "Applications" / f"{app_name}.app",
            Path("/System/Applications") / f"{app_name}.app",
        ]
        if any(path.exists() for path in candidates):
            return True

        app_id = self._catalog_id_for_launch_name(app_name)
        bundle_ids = _BROWSER_BUNDLES.get(app_id, ())
        for bundle_id in bundle_ids:
            proc = self._run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"], timeout=1.5)
            if proc and proc.returncode == 0 and proc.stdout.strip():
                return True
        return False

    def _catalog_id_for_launch_name(self, launch_name: str) -> str:
        for catalog in (BROWSER_CATALOG, MESSAGING_CATALOG):
            for app_id, spec in catalog.items():
                if spec.get("launch_name") == launch_name:
                    return app_id
        return ""

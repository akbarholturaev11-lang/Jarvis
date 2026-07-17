from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


UNKNOWN = "unknown"
UNSUPPORTED = "unsupported"
AVAILABLE = "available"
BLOCKED = "blocked"
PERMISSION_REQUIRED = "permission_required"

# Stable identifier used across OSes for the app's auto-start registration
# (macOS LaunchAgent Label, Windows Run value name, Linux .desktop stem).
AUTOSTART_LABEL = "com.jarvis.assistant"

# Permissions JARVIS may need to operate fully. On macOS these are TCC-gated and
# require an explicit user grant; other OSes do not gate them the same way.
PERMISSION_NAMES = (
    "accessibility",
    "automation",
    "screen_recording",
    "microphone",
    "camera",
)


BROWSER_CATALOG: dict[str, dict[str, Any]] = {
    "safari": {
        "name": "Safari",
        "launch_name": "Safari",
        "aliases": ("safari",),
    },
    "chrome": {
        "name": "Google Chrome",
        "launch_name": "Google Chrome",
        "aliases": ("chrome", "google chrome"),
    },
    "atlas": {
        "name": "ChatGPT Atlas",
        "launch_name": "ChatGPT Atlas",
        "aliases": ("atlas", "chatgpt atlas", "gpt atlas"),
    },
    "firefox": {
        "name": "Firefox",
        "launch_name": "Firefox",
        "aliases": ("firefox", "mozilla firefox"),
    },
    "edge": {
        "name": "Microsoft Edge",
        "launch_name": "Microsoft Edge",
        "aliases": ("edge", "microsoft edge", "ms edge"),
    },
    "arc": {
        "name": "Arc",
        "launch_name": "Arc",
        "aliases": ("arc", "arc browser"),
    },
    "brave": {
        "name": "Brave",
        "launch_name": "Brave Browser",
        "aliases": ("brave", "brave browser"),
    },
    "opera": {
        "name": "Opera",
        "launch_name": "Opera",
        "aliases": ("opera",),
    },
}


MESSAGING_CATALOG: dict[str, dict[str, Any]] = {
    "telegram": {
        "name": "Telegram",
        "launch_name": "Telegram",
        "aliases": ("telegram", "tg"),
    },
    "whatsapp": {
        "name": "WhatsApp",
        "launch_name": "WhatsApp",
        "aliases": ("whatsapp", "whats app", "wp"),
    },
    "messages": {
        "name": "Messages",
        "launch_name": "Messages",
        "aliases": ("messages", "imessage", "sms"),
    },
    "wechat": {
        "name": "WeChat",
        "launch_name": "WeChat",
        "aliases": ("wechat", "we chat"),
    },
    "discord": {
        "name": "Discord",
        "launch_name": "Discord",
        "aliases": ("discord",),
    },
    "slack": {
        "name": "Slack",
        "launch_name": "Slack",
        "aliases": ("slack",),
    },
    "signal": {
        "name": "Signal",
        "launch_name": "Signal",
        "aliases": ("signal",),
    },
    "teams": {
        "name": "Microsoft Teams",
        "launch_name": "Microsoft Teams",
        "aliases": ("teams", "microsoft teams"),
    },
    "zoom": {
        "name": "Zoom",
        "launch_name": "Zoom",
        "aliases": ("zoom",),
    },
}


class PlatformAdapter:
    """Best-effort, side-effect-light capability detector for one platform."""

    os_key = "unknown"

    def __init__(self, project_root: str | Path | None = None):
        self.project_root = Path(project_root or Path.cwd()).resolve()

    def detect_os_info(self) -> dict[str, Any]:
        return {
            "os": self.os_key,
            "version": platform.platform(),
            "architecture": platform.machine() or UNKNOWN,
            "python_version": platform.python_version(),
            "venv_active": bool(sys.prefix != getattr(sys, "base_prefix", sys.prefix)),
            "venv_path": sys.prefix if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "",
            "shell": self.detect_shell(),
            "desktop_session": self.detect_desktop_session(),
        }

    def detect_shell(self) -> str:
        candidates = (
            os.environ.get("SHELL"),
            os.environ.get("COMSPEC"),
            os.environ.get("PSModulePath") and "powershell",
        )
        for raw in candidates:
            if raw:
                return Path(str(raw)).name.lower()
        return UNKNOWN

    def detect_desktop_session(self) -> dict[str, Any]:
        gui_available = bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("SESSIONNAME")
            or self.os_key in {"macos", "windows"}
        )
        return {
            "gui_available": gui_available,
            "desktop_environment": os.environ.get("XDG_CURRENT_DESKTOP", "") or UNKNOWN,
            "session_type": os.environ.get("XDG_SESSION_TYPE", "") or UNKNOWN,
            "display_server": self._display_server(),
        }

    def detect_gui(self) -> dict[str, Any]:
        session = self.detect_desktop_session()
        return {
            "available": bool(session.get("gui_available")),
            "desktop_environment": session.get("desktop_environment", UNKNOWN),
            "session_type": session.get("session_type", UNKNOWN),
            "display_server": session.get("display_server", UNKNOWN),
        }

    def detect_app_launch(self) -> dict[str, Any]:
        return {"supported": False, "method": UNKNOWN, "verified": False}

    def detect_installed_apps(self) -> list[dict[str, Any]]:
        apps = self.detect_browsers() + self.detect_messaging_apps()
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for app in apps:
            app_id = str(app.get("id") or app.get("name") or "").lower()
            if app_id and app_id not in seen:
                seen.add(app_id)
                unique.append(app)
        return unique

    def detect_browsers(self) -> list[dict[str, Any]]:
        return []

    def detect_default_browser(self, browsers: list[dict[str, Any]] | None = None) -> str:
        return UNKNOWN

    def detect_messaging_apps(self) -> list[dict[str, Any]]:
        return []

    def detect_media_control(self) -> dict[str, Any]:
        return {
            "supported": False,
            "method": UNKNOWN,
            "verified": False,
            "status": UNSUPPORTED,
        }

    def detect_active_window(self) -> dict[str, Any]:
        return {
            "supported": False,
            "method": UNKNOWN,
            "status": UNSUPPORTED,
            "requires_permission": False,
        }

    def detect_screen_capture(self) -> dict[str, Any]:
        if self._module_available("mss") and self.detect_gui().get("available"):
            return {
                "status": AVAILABLE,
                "requires_permission": False,
                "method": "mss",
            }
        return {
            "status": UNKNOWN,
            "requires_permission": False,
            "method": UNKNOWN,
        }

    def detect_camera(self) -> dict[str, Any]:
        if self._module_available("cv2"):
            return {
                "status": UNKNOWN,
                "requires_permission": True,
                "method": "opencv",
            }
        return {
            "status": UNKNOWN,
            "requires_permission": False,
            "method": UNKNOWN,
        }

    def detect_audio_devices(self) -> dict[str, Any]:
        result = {
            "input": UNKNOWN,
            "output": UNKNOWN,
            "requires_permission": True,
            "method": UNKNOWN,
        }
        if not self._module_available("sounddevice"):
            return result
        try:
            from core.runtime_warnings import install_runtime_warning_filters

            install_runtime_warning_filters()
            import sounddevice as sd

            devices = sd.query_devices()
            has_input = any(int(device.get("max_input_channels", 0)) > 0 for device in devices)
            has_output = any(int(device.get("max_output_channels", 0)) > 0 for device in devices)
            result.update(
                {
                    "input": AVAILABLE if has_input else UNKNOWN,
                    "output": AVAILABLE if has_output else UNKNOWN,
                    "method": "sounddevice",
                }
            )
        except Exception:
            pass
        return result

    def detect_clipboard(self) -> dict[str, Any]:
        if self._module_available("pyperclip"):
            return {"status": AVAILABLE, "method": "pyperclip"}
        return {"status": UNKNOWN, "method": UNKNOWN}

    def detect_ui_automation(self) -> dict[str, Any]:
        if self._module_available("pyautogui"):
            return {
                "status": AVAILABLE if self.detect_gui().get("available") else UNKNOWN,
                "method": "pyautogui",
                "requires_permission": False,
            }
        return {
            "status": UNKNOWN,
            "method": UNKNOWN,
            "requires_permission": False,
        }

    def detect_permissions(self) -> dict[str, str]:
        return {
            "microphone": UNKNOWN,
            "camera": UNKNOWN,
            "screen_recording": UNKNOWN,
            "accessibility": UNKNOWN,
            "automation": UNKNOWN,
        }

    def build_common_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for catalog in (BROWSER_CATALOG, MESSAGING_CATALOG):
            for spec in catalog.values():
                launch_name = str(spec["launch_name"])
                for alias in spec.get("aliases", ()):
                    aliases[str(alias).lower()] = launch_name
                aliases[str(spec["name"]).lower()] = launch_name
        return aliases

    def launch_app(self, app_name: str) -> tuple[bool, str]:
        return False, f"App launch is unsupported on {self.os_key}."

    def get_active_app(self) -> str:
        return ""

    def media_pause(self, target_app: str = "") -> tuple[bool | None, str]:
        return None, f"Media control is unsupported on {self.os_key}."

    # ── graceful app close ────────────────────────────────────────────────────
    # Contract: close_app(app_name) returns (ok, detail).
    #   ok is True  → the app is verified no longer running (graceful quit).
    #   ok is None  → a quit request was sent but the result could not be verified.
    #   ok is False → the close failed or is unsupported on this OS.
    # Adapters must request a graceful quit, never a hard kill by default, and must
    # never fake a success. Per-OS adapters override this honest fallback.
    def close_app(self, app_name: str) -> tuple[bool | None, str]:
        return False, f"App close is unsupported on {self.os_key}."

    # ── keep-awake (prevent OS idle sleep during a remote session) ────────────
    # Contract: prevent_sleep() returns (token, status). A non-None token means
    # success and must be passed back to release_sleep() to undo. None means the
    # capability is unavailable on this OS — an honest unsupported, never a fake
    # success. Per-OS adapters override this; the base is the honest fallback.
    def prevent_sleep(self, reason: str = "") -> tuple[object | None, str]:
        return None, f"Keep-awake is unsupported on {self.os_key}."

    def release_sleep(self, token: object) -> None:
        self._terminate_proc(token)

    # ── auto-start on login (register the app to launch at OS startup) ─────────
    # Contract:
    #   autostart_status(label) returns (state, detail):
    #     state True  → auto-start is currently registered for this app.
    #     state False → auto-start is not registered.
    #     state None  → the capability is unsupported/undetectable on this OS.
    #   set_autostart(enabled, command, label) returns (result, detail):
    #     result True  → the change was applied and verified.
    #     result None  → attempted but the result could not be verified.
    #     result False → failed or unsupported on this OS.
    # `command` is the argv used to relaunch the app (e.g. [python, main.py] for a
    # source run, or [executable] for a frozen build). Per-OS adapters override
    # these; the base is the honest unsupported fallback — never a fake success.
    def autostart_status(self, label: str = AUTOSTART_LABEL) -> tuple[bool | None, str]:
        return None, f"Auto-start is unsupported on {self.os_key}."

    def set_autostart(
        self,
        enabled: bool,
        command: list[str],
        label: str = AUTOSTART_LABEL,
    ) -> tuple[bool | None, str]:
        return None, f"Auto-start is unsupported on {self.os_key}."

    # ── OS permission onboarding (macOS TCC; no-op contract elsewhere) ─────────
    # Contract:
    #   permission_status(name) -> "granted" | "denied" | "unknown" | "not_required"
    #     "not_required" means this OS does not gate the capability behind a grant.
    #   request_permission(name) -> (result, detail):
    #     result True  → a real OS prompt was triggered or the settings pane opened.
    #     result None  → nothing to do (already granted / not applicable).
    #     result False → could not act.
    #   open_permission_pane(name) -> (ok, detail) deep-links to the settings pane.
    # Per-OS adapters override these; the base is the honest not-applicable fallback
    # so Windows/Linux never claim a macOS-style grant they don't have.
    def permission_status(self, name: str) -> str:
        return "not_required"

    def request_permission(self, name: str) -> tuple[bool | None, str]:
        return None, f"Permission management is not applicable on {self.os_key}."

    def open_permission_pane(self, name: str) -> tuple[bool, str]:
        return False, f"Permission settings pane is not available on {self.os_key}."

    def _terminate_proc(self, token: object) -> None:
        """Best-effort terminate for adapters whose token is a subprocess handle."""
        if token is None:
            return
        terminate = getattr(token, "terminate", None)
        if not callable(terminate):
            return
        try:
            terminate()
            wait = getattr(token, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=2)
                except Exception:
                    kill = getattr(token, "kill", None)
                    if callable(kill):
                        kill()
        except Exception:
            pass

    def _display_server(self) -> str:
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return UNKNOWN

    def _module_available(self, module_name: str) -> bool:
        return importlib.util.find_spec(module_name) is not None

    def _which(self, *names: str) -> str:
        for name in names:
            path = shutil.which(name)
            if path:
                return path
        return ""

    def _run(
        self,
        cmd: list[str],
        timeout: float = 2.0,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            return None

    def _app_record(
        self,
        app_id: str,
        spec: dict[str, Any],
        detected: bool,
        source: str = "",
    ) -> dict[str, Any]:
        return {
            "id": app_id,
            "name": str(spec["name"]),
            "launch_name": str(spec["launch_name"]),
            "detected": bool(detected),
            "source": source,
        }

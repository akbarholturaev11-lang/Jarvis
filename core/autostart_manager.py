"""core/autostart_manager.py — cross-platform "launch at login" facade.

Registers or unregisters JARVIS with the OS startup system through the active
platform adapter (macOS LaunchAgent, Windows HKCU Run entry, Linux XDG autostart
.desktop entry). Honest by construction: on an OS that cannot support it the
adapter returns an unsupported / unverified status and nothing is faked.

The launch command is derived here — a frozen build relaunches its own executable;
a source run relaunches the current interpreter plus main.py, preferring
pythonw.exe on Windows so no console window flashes at login — then handed to the
adapter, which owns only the OS-level registration.
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.environment_discovery import select_platform_adapter

BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = BASE_DIR / "main.py"


def build_launch_command() -> list[str]:
    """The argv used to relaunch the app at login."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    interpreter = sys.executable
    if sys.platform.startswith("win"):
        # Prefer pythonw.exe so no console window appears at login.
        pythonw = Path(interpreter).with_name("pythonw.exe")
        if pythonw.exists():
            interpreter = str(pythonw)
    return [interpreter, str(MAIN_SCRIPT)]


def autostart_status() -> tuple[bool | None, str]:
    """(state, detail): True registered, False not registered, None unsupported."""
    try:
        adapter = select_platform_adapter()
        return adapter.autostart_status()
    except Exception as e:  # never let a status probe crash the caller
        return None, f"Auto-start status unavailable: {e}"


def is_autostart_supported() -> bool:
    state, _ = autostart_status()
    return state is not None


def set_autostart(enabled: bool) -> tuple[bool | None, str]:
    """(result, detail): True applied+verified, None unverified, False failed/unsupported."""
    try:
        adapter = select_platform_adapter()
        return adapter.set_autostart(bool(enabled), build_launch_command())
    except Exception as e:
        return False, f"Auto-start change failed: {e}"

from __future__ import annotations

import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.platform_adapters import LinuxAdapter, MacOSAdapter, PlatformAdapter, WindowsAdapter


SCHEMA_VERSION = 1
PROJECT_RESOURCE_FILES = (
    "AI_RULES.md",
    "PROJECT_MEMORY.md",
    "CHANGELOG_AKBAR.md",
    "NEXT_STEPS.md",
    "PROJECT_MAP.md",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def detect_platform_key() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    if system == "Linux":
        return "linux"
    return "unknown"


def select_platform_adapter(project_root: str | Path | None = None) -> PlatformAdapter:
    os_key = detect_platform_key()
    if os_key == "macos":
        return MacOSAdapter(project_root)
    if os_key == "windows":
        return WindowsAdapter(project_root)
    if os_key == "linux":
        return LinuxAdapter(project_root)
    return PlatformAdapter(project_root)


def discover_environment(
    project_root: str | Path | None = None,
    existing_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a safe operational profile from best-effort local detection."""

    root = Path(project_root or Path.cwd()).resolve()
    adapter = select_platform_adapter(root)
    existing = existing_profile or {}
    created_at = str(existing.get("created_at") or now_iso())
    updated_at = now_iso()

    platform_info = _safe_call(adapter.detect_os_info, {})
    browsers = _safe_call(adapter.detect_browsers, [])
    messaging = _safe_call(adapter.detect_messaging_apps, [])
    default_browser = _safe_call(lambda: adapter.detect_default_browser(browsers), "unknown")
    gui = _safe_call(adapter.detect_gui, {"available": False})
    app_launch = _safe_call(adapter.detect_app_launch, {"supported": False, "method": "unknown"})
    media_control = _safe_call(adapter.detect_media_control, {"supported": False, "method": "unknown", "status": "unknown"})
    active_window = _safe_call(adapter.detect_active_window, {"supported": False, "method": "unknown", "status": "unknown"})
    screen_capture = _safe_call(adapter.detect_screen_capture, {"status": "unknown", "requires_permission": False})
    camera = _safe_call(adapter.detect_camera, {"status": "unknown", "requires_permission": True})
    audio_devices = _safe_call(adapter.detect_audio_devices, {"input": "unknown", "output": "unknown"})
    clipboard = _safe_call(adapter.detect_clipboard, {"status": "unknown", "method": "unknown"})
    ui_automation = _safe_call(adapter.detect_ui_automation, {"status": "unknown", "method": "unknown"})
    permissions = _safe_call(adapter.detect_permissions, {})

    browser_control = {
        "supported": bool(app_launch.get("supported") and _detected_ids(browsers)),
        "default_browser": default_browser or "unknown",
        "preferred_browser": _preferred_browser(existing),
        "installed_browsers": _detected_ids(browsers),
    }

    profile = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": updated_at,
        "platform": platform_info,
        "capabilities": {
            "gui_available": bool(gui.get("available")),
            "desktop": gui,
            "app_launch": app_launch,
            "browser_control": browser_control,
            "media_control": media_control,
            "active_window": active_window,
            "screen_capture": screen_capture,
            "camera": camera,
            "audio_devices": audio_devices,
            "clipboard": clipboard,
            "ui_automation": ui_automation,
        },
        "apps": {
            "browsers": browsers,
            "messaging": messaging,
            "installed": _safe_call(adapter.detect_installed_apps, []),
            "common_aliases": adapter.build_common_aliases(),
        },
        "permissions": _normalize_permissions(permissions),
        "project": {
            "project_root": str(root),
            "memory_docs": _project_resources(root),
        },
    }
    return profile


def _safe_call(func, fallback):
    try:
        value = func()
        return fallback if value is None else value
    except Exception:
        return fallback


def _detected_ids(apps: list[dict[str, Any]]) -> list[str]:
    ids = []
    for app in apps:
        if app.get("detected") and app.get("id"):
            ids.append(str(app["id"]))
    return ids


def _preferred_browser(existing_profile: dict[str, Any]) -> str:
    try:
        return str(
            existing_profile.get("capabilities", {})
            .get("browser_control", {})
            .get("preferred_browser", "")
        )
    except Exception:
        return ""


def _project_resources(root: Path) -> list[dict[str, Any]]:
    resources = []
    for filename in PROJECT_RESOURCE_FILES:
        path = root / filename
        resources.append(
            {
                "name": filename,
                "path": str(path),
                "exists": path.exists(),
            }
        )
    return resources


def _normalize_permissions(raw: dict[str, Any]) -> dict[str, str]:
    base = {
        "microphone": "unknown",
        "camera": "unknown",
        "screen_recording": "unknown",
        "accessibility": "unknown",
        "automation": "unknown",
    }
    for key, value in (raw or {}).items():
        base[str(key)] = str(value)
    return base

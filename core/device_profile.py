from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.environment_discovery import SCHEMA_VERSION, discover_environment, now_iso
from core.platform_adapters.base import BROWSER_CATALOG, MESSAGING_CATALOG


ALLOWED_OSES = {"macos", "windows", "linux", "unknown"}
STATUS_VALUES = {"available", "blocked", "unknown", "unsupported", "permission_required", "not_applicable"}

_BROWSER_ALIASES = {
    alias: browser_id
    for browser_id, spec in BROWSER_CATALOG.items()
    for alias in (*spec.get("aliases", ()), spec.get("name", ""), spec.get("launch_name", ""))
    if alias
}
_MESSAGING_ALIASES = {
    alias: app_id
    for app_id, spec in MESSAGING_CATALOG.items()
    for alias in (*spec.get("aliases", ()), spec.get("name", ""), spec.get("launch_name", ""))
    if alias
}

_REFRESH_PATTERNS = (
    "refresh device profile",
    "rescan device",
    "scan my computer",
    "qurilmani qayta tekshir",
    "kompyuterni qayta o'rgan",
    "kompyuterni qayta organ",
    "mac'ni qayta tekshir",
    "macni qayta tekshir",
    "windows'ni qayta tekshir",
    "windowsni qayta tekshir",
)

_QUERY_PATTERNS = (
    "qaysi qurilmada ishlayapsan",
    "asosiy browser qaysi",
    "default browser",
    "telegram bormi",
    "device profile",
    "what device",
    "which device",
)

_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "database_url",
    "gemini_api",
)


def default_profile(project_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(project_root or Path.cwd()).resolve()
    now = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
        "platform": {
            "os": "unknown",
            "version": "unknown",
            "architecture": "unknown",
            "python_version": "unknown",
            "venv_active": False,
            "venv_path": "",
            "shell": "unknown",
            "desktop_session": {
                "gui_available": False,
                "desktop_environment": "unknown",
                "session_type": "unknown",
                "display_server": "unknown",
            },
        },
        "capabilities": {
            "gui_available": False,
            "desktop": {
                "available": False,
                "desktop_environment": "unknown",
                "session_type": "unknown",
                "display_server": "unknown",
            },
            "app_launch": {
                "supported": False,
                "method": "unknown",
                "verified": False,
            },
            "browser_control": {
                "supported": False,
                "default_browser": "unknown",
                "preferred_browser": "",
                "installed_browsers": [],
            },
            "media_control": {
                "supported": False,
                "method": "unknown",
                "verified": False,
                "status": "unknown",
            },
            "active_window": {
                "supported": False,
                "method": "unknown",
                "status": "unknown",
                "requires_permission": False,
            },
            "screen_capture": {
                "status": "unknown",
                "requires_permission": False,
                "method": "unknown",
            },
            "camera": {
                "status": "unknown",
                "requires_permission": True,
                "method": "unknown",
            },
            "audio_devices": {
                "input": "unknown",
                "output": "unknown",
                "requires_permission": True,
                "method": "unknown",
            },
            "clipboard": {
                "status": "unknown",
                "method": "unknown",
            },
            "ui_automation": {
                "status": "unknown",
                "method": "unknown",
                "requires_permission": False,
            },
        },
        "apps": {
            "browsers": [],
            "messaging": [],
            "installed": [],
            "common_aliases": {},
        },
        "permissions": {
            "microphone": "unknown",
            "camera": "unknown",
            "screen_recording": "unknown",
            "accessibility": "unknown",
            "automation": "unknown",
        },
        "project": {
            "project_root": str(root),
            "memory_docs": [],
        },
    }


def get_device_profile_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root or Path.cwd()).resolve()
    return root / "config" / "device_profile.json"


def ensure_device_profile(
    project_root: str | Path | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(profile_path) if profile_path else get_device_profile_path(project_root)
    if path.exists():
        profile = load_device_profile(path)
        merged = merge_profile_defaults(profile, project_root)
        if merged != profile:
            save_device_profile(merged, path)
        return merged
    profile = refresh_device_profile(project_root, path)
    return profile


def refresh_device_profile(
    project_root: str | Path | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(profile_path) if profile_path else get_device_profile_path(project_root)
    existing = load_device_profile(path) if path.exists() else {}
    try:
        profile = discover_environment(project_root, existing)
    except Exception:
        profile = default_profile(project_root)
        profile["updated_at"] = now_iso()
    save_device_profile(profile, path)
    return profile


def load_device_profile(profile_path: str | Path) -> dict[str, Any]:
    path = Path(profile_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Device profile must be a JSON object.")
    return data


def save_device_profile(profile: dict[str, Any], profile_path: str | Path) -> None:
    path = Path(profile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_profile = scrub_profile(profile)
    path.write_text(
        json.dumps(safe_profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def merge_profile_defaults(
    profile: dict[str, Any],
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    merged = default_profile(project_root)
    _deep_update(merged, profile)
    if merged.get("schema_version") != SCHEMA_VERSION:
        merged["schema_version"] = SCHEMA_VERSION
    merged["platform"]["os"] = normalize_os(merged.get("platform", {}).get("os", "unknown"))
    return scrub_profile(merged)


def validate_device_profile(profile: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return False, ["profile must be a dict"]
    if profile.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    platform_block = profile.get("platform", {})
    os_name = normalize_os(platform_block.get("os", "unknown"))
    if os_name not in ALLOWED_OSES:
        errors.append(f"platform.os is invalid: {os_name}")
    for key in ("capabilities", "apps", "permissions", "project"):
        if key not in profile or not isinstance(profile[key], dict):
            errors.append(f"missing object: {key}")
    return not errors, errors


def normalize_os(value: Any) -> str:
    lowered = _clean(value).lower()
    if lowered in {"darwin", "mac", "macos", "osx", "mac os"}:
        return "macos"
    if lowered in {"windows", "win32", "win"}:
        return "windows"
    if lowered in {"linux", "gnu/linux"}:
        return "linux"
    return "unknown"


def normalize_browser_name(value: Any) -> str:
    lowered = _clean(value).lower()
    if not lowered:
        return ""
    if lowered in _BROWSER_ALIASES:
        return _BROWSER_ALIASES[lowered]
    for alias, browser_id in _BROWSER_ALIASES.items():
        if alias and alias in lowered:
            return browser_id
    return lowered


def normalize_messaging_app(value: Any) -> str:
    lowered = _clean(value).lower()
    if not lowered:
        return ""
    if lowered in _MESSAGING_ALIASES:
        return _MESSAGING_ALIASES[lowered]
    for alias, app_id in _MESSAGING_ALIASES.items():
        if alias and alias in lowered:
            return app_id
    return lowered


def resolve_browser_route(
    profile: dict[str, Any],
    requested_browser: str = "",
    session_context: Any = None,
) -> dict[str, Any]:
    installed = set(installed_browser_ids(profile))
    browser_control = profile.get("capabilities", {}).get("browser_control", {})
    requested = normalize_browser_name(requested_browser)
    if requested:
        if requested in installed:
            return {"status": "ok", "browser": requested, "source": "explicit"}
        return {
            "status": "failed",
            "browser": requested,
            "source": "explicit",
            "reason": f"Browser not found in DeviceProfile: {requested}.",
        }

    session_browser = normalize_browser_name(getattr(session_context, "last_browser_used", ""))
    if session_browser and session_browser in installed:
        return {"status": "ok", "browser": session_browser, "source": "session_context"}

    preferred = normalize_browser_name(browser_control.get("preferred_browser", ""))
    if preferred and preferred in installed:
        return {"status": "ok", "browser": preferred, "source": "user_preference"}

    default = normalize_browser_name(browser_control.get("default_browser", ""))
    if default and default != "unknown" and default in installed:
        return {"status": "ok", "browser": default, "source": "system_default"}

    if installed:
        browser = sorted(installed)[0]
        return {"status": "ok", "browser": browser, "source": "installed_available"}

    return {
        "status": "needs_confirmation",
        "browser": "",
        "source": "device_profile",
        "reason": "No installed browser was detected. Ask which browser to use.",
    }


def resolve_app_route(profile: dict[str, Any], app_name: str) -> dict[str, Any]:
    raw = _clean(app_name)
    if not raw:
        return {"status": "failed", "reason": "No application name provided."}

    launch = profile.get("capabilities", {}).get("app_launch", {})
    normalized_lower = raw.lower()
    aliases = profile.get("apps", {}).get("common_aliases", {})
    launch_name = aliases.get(normalized_lower, raw)

    known_ids = _known_app_ids(profile)
    requested_browser = normalize_browser_name(raw)
    requested_message = normalize_messaging_app(raw)
    known_requested = requested_browser in BROWSER_CATALOG or requested_message in MESSAGING_CATALOG

    if known_requested:
        app_id = requested_browser if requested_browser in BROWSER_CATALOG else requested_message
        if app_id not in known_ids:
            return {
                "status": "failed",
                "app_name": launch_name,
                "reason": f"Application not found in DeviceProfile: {raw}.",
            }

    if not launch.get("supported"):
        return {
            "status": "failed",
            "app_name": launch_name,
            "reason": "App launch is unsupported or unknown on this device.",
        }

    return {
        "status": "ok",
        "app_name": launch_name,
        "method": launch.get("method", "unknown"),
        "source": "device_profile",
    }


def resolve_media_route(profile: dict[str, Any]) -> dict[str, Any]:
    media = profile.get("capabilities", {}).get("media_control", {})
    supported = bool(media.get("supported"))
    status = str(media.get("status", "unknown"))
    method = str(media.get("method", "unknown"))
    if supported and status in {"available", "permission_required"} and method != "unknown":
        return {
            "status": "ok",
            "method": method,
            "verified": bool(media.get("verified")),
            "reason": "DeviceProfile media control is available.",
        }
    return {
        "status": "uncertain",
        "method": method,
        "verified": False,
        "reason": f"Media control capability is {status or 'unknown'} on this device.",
    }


def resolve_messaging_route(
    profile: dict[str, Any],
    platform_name: str,
    receiver: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    app_id = normalize_messaging_app(platform_name)
    if not app_id:
        return {
            "status": "needs_confirmation",
            "reason": "No messaging app was provided.",
        }
    installed = set(installed_messaging_ids(profile))
    if app_id not in installed:
        return {
            "status": "failed",
            "app": app_id,
            "reason": f"Messaging app not found in DeviceProfile: {platform_name}.",
        }
    if not receiver:
        return {
            "status": "needs_confirmation",
            "app": app_id,
            "reason": "Recipient/contact is not verified.",
        }
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "app": app_id,
            "reason": "Message sending requires explicit confirmation and verification.",
        }
    return {
        "status": "ok",
        "app": app_id,
        "reason": "Messaging app is installed; send flow may attempt after verification.",
    }


def check_permission_gate(
    profile: dict[str, Any],
    capability_name: str,
) -> dict[str, Any]:
    capabilities = profile.get("capabilities", {})
    capability = capabilities.get(capability_name, {})
    if capability_name == "ui_automation":
        status = str(capability.get("status", "unknown"))
    else:
        status = str(capability.get("status", "unknown"))
    requires_permission = bool(capability.get("requires_permission"))
    if status == "available":
        return {
            "allowed": True,
            "status": status,
            "requires_permission": requires_permission,
            "reason": "Capability is available in DeviceProfile.",
        }
    if status == "permission_required":
        return {
            "allowed": True,
            "status": status,
            "requires_permission": True,
            "reason": "Capability may require platform permission.",
        }
    return {
        "allowed": False,
        "status": status,
        "requires_permission": requires_permission,
        "reason": f"{capability_name} capability is {status}; do not claim success.",
    }


def installed_browser_ids(profile: dict[str, Any]) -> list[str]:
    apps = profile.get("apps", {}).get("browsers", [])
    ids = _detected_ids_from_apps(apps)
    if ids:
        return ids
    fallback = profile.get("capabilities", {}).get("browser_control", {}).get("installed_browsers", [])
    return [normalize_browser_name(item) for item in fallback if normalize_browser_name(item)]


def installed_messaging_ids(profile: dict[str, Any]) -> list[str]:
    return _detected_ids_from_apps(profile.get("apps", {}).get("messaging", []))


def format_device_profile_summary(profile: dict[str, Any]) -> str:
    platform_block = profile.get("platform", {})
    capabilities = profile.get("capabilities", {})
    browser_control = capabilities.get("browser_control", {})
    media = capabilities.get("media_control", {})
    permissions = profile.get("permissions", {})
    browsers = installed_browser_ids(profile)
    messaging = installed_messaging_ids(profile)
    warnings = [
        name
        for name, status in permissions.items()
        if str(status) in {"unknown", "blocked", "permission_required"}
    ]
    unknown_caps = []
    for name in ("screen_capture", "camera", "ui_automation", "active_window"):
        status = str(capabilities.get(name, {}).get("status", "unknown"))
        if status in {"unknown", "blocked", "unsupported"}:
            unknown_caps.append(f"{name}:{status}")

    return (
        "[DEVICE PROFILE SUMMARY]\n"
        f"OS: {platform_block.get('os', 'unknown')} {platform_block.get('version', '')} "
        f"({platform_block.get('architecture', 'unknown')})\n"
        f"Python: {platform_block.get('python_version', 'unknown')} | "
        f"venv: {platform_block.get('venv_path') or 'inactive/unknown'}\n"
        f"Default browser: {browser_control.get('default_browser', 'unknown')}\n"
        f"Installed browsers: {', '.join(browsers) if browsers else 'none detected'}\n"
        f"Messaging apps: {', '.join(messaging) if messaging else 'none detected'}\n"
        f"Media control: {media.get('method', 'unknown')} "
        f"({media.get('status', 'unknown')}, verified={bool(media.get('verified'))})\n"
        f"Permission warnings: {', '.join(warnings) if warnings else 'none'}\n"
        f"Missing/unknown capabilities: {', '.join(unknown_caps) if unknown_caps else 'none'}"
    )


def format_device_profile_for_prompt(profile: dict[str, Any]) -> str:
    return (
        "[DEVICE PROFILE - internal, do not read aloud]\n"
        "Consult this before app/browser/media/message/screen/camera/microphone/UI automation actions.\n"
        f"{format_device_profile_summary(profile)}\n"
        "DeviceProfile = what this device can do. SessionContext = what happened recently. "
        "Tool verification = what actually succeeded. Unknown capability is unknown, not success.\n"
    )


def answer_device_profile_query(profile: dict[str, Any], query: str = "") -> str:
    lowered = _clean(query).lower()
    if "telegram" in lowered:
        installed = set(installed_messaging_ids(profile))
        return "Telegram is detected in DeviceProfile." if "telegram" in installed else "Telegram is not detected in DeviceProfile."
    if "browser" in lowered:
        browser = profile.get("capabilities", {}).get("browser_control", {}).get("default_browser", "unknown")
        return f"Default browser from DeviceProfile: {browser}."
    if "qurilma" in lowered or "device" in lowered or "platform" in lowered:
        platform_block = profile.get("platform", {})
        return (
            f"DeviceProfile says OS={platform_block.get('os', 'unknown')}, "
            f"version={platform_block.get('version', 'unknown')}, "
            f"architecture={platform_block.get('architecture', 'unknown')}."
        )
    return format_device_profile_summary(profile)


def is_device_profile_refresh_request(text: str) -> bool:
    lowered = _normalize_quotes(_clean(text).lower())
    return any(pattern in lowered for pattern in _REFRESH_PATTERNS)


def is_device_profile_query_request(text: str) -> bool:
    lowered = _normalize_quotes(_clean(text).lower())
    return any(pattern in lowered for pattern in _QUERY_PATTERNS)


def scrub_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return _scrub_value(profile)


def find_privacy_violations(profile: dict[str, Any]) -> list[str]:
    violations: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered_key = str(key).lower()
                if any(part in lowered_key for part in _FORBIDDEN_KEY_PARTS):
                    violations.append(path + "." + str(key) if path else str(key))
                walk(child, path + "." + str(key) if path else str(key))
            return
        if isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")
            return
        if isinstance(value, str):
            lowered = value.lower()
            if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                violations.append(path)
            if len(value) > 600 and "project_root" not in path and "path" not in path:
                violations.append(path)

    walk(profile)
    return [item for item in violations if item]


def _scrub_value(value: Any, key: str = "") -> Any:
    lowered_key = key.lower()
    if any(part in lowered_key for part in _FORBIDDEN_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _scrub_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, key) for item in value]
    if isinstance(value, str):
        if any(part in value.lower() for part in _FORBIDDEN_KEY_PARTS):
            return "[redacted]"
        if len(value) > 1000:
            return value[:1000] + "..."
    return value


def _detected_ids_from_apps(apps: list[Any]) -> list[str]:
    ids = []
    for item in apps:
        if isinstance(item, dict):
            if item.get("detected") and item.get("id"):
                ids.append(str(item["id"]).lower())
        else:
            normalized = normalize_browser_name(item) or normalize_messaging_app(item)
            if normalized:
                ids.append(normalized)
    return ids


def _known_app_ids(profile: dict[str, Any]) -> set[str]:
    ids = set(installed_browser_ids(profile))
    ids.update(installed_messaging_ids(profile))
    return ids


def _deep_update(base: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_quotes(value: str) -> str:
    return (
        value.replace("‘", "'")
        .replace("’", "'")
        .replace("`", "'")
        .replace("ʻ", "'")
        .replace("ʼ", "'")
    )

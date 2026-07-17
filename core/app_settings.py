"""core/app_settings.py — safe read/modify/write for config/settings.json.

Non-secret app settings only (ui_language, remote_tunnel, ...). Reads tolerate a
missing or corrupt file; writes preserve unrelated keys so different features can
own different settings without clobbering each other (i18n owns ui_language, the
remote tunnel owns remote_tunnel, etc.). Never store secrets here — cloudflared
credentials live in ~/.cloudflared, outside the repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.app_paths import resolve_app_paths

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = (
    resolve_app_paths().config_dir / "settings.json"
    if getattr(sys, "frozen", False)
    else BASE_DIR / "config" / "settings.json"
)

_DEFAULT_TUNNEL = {
    "enabled": False,
    "provider": "cloudflare",
    "mode": "quick",     # "quick" (no account) or "named" (stable hostname)
    "hostname": "",
}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def update_settings(patch: dict) -> dict:
    """Merge `patch` into settings.json, preserving all other keys."""
    settings = load_settings()
    settings.update(patch)
    save_settings(settings)
    return settings


def get_tunnel_config() -> dict:
    cfg = load_settings().get("remote_tunnel")
    merged = dict(_DEFAULT_TUNNEL)
    if isinstance(cfg, dict):
        for k in _DEFAULT_TUNNEL:
            if k in cfg:
                merged[k] = cfg[k]
    return merged


def set_tunnel_enabled(enabled: bool) -> dict:
    cfg = get_tunnel_config()
    cfg["enabled"] = bool(enabled)
    update_settings({"remote_tunnel": cfg})
    return cfg


def get_keep_awake_enabled() -> bool:
    val = load_settings().get("keep_awake_enabled", True)
    return bool(val)


def set_keep_awake_enabled(enabled: bool) -> bool:
    update_settings({"keep_awake_enabled": bool(enabled)})
    return bool(enabled)


def get_clipboard_actions_enabled() -> bool:
    """Whether the clipboard-intelligence quick-action panel is active."""
    val = load_settings().get("clipboard_actions_enabled", True)
    return bool(val)


def set_clipboard_actions_enabled(enabled: bool) -> bool:
    update_settings({"clipboard_actions_enabled": bool(enabled)})
    return bool(enabled)


def get_permissions_onboarded() -> bool:
    """Whether the user has already been through the permission checklist once,
    so startup does not re-open it on every launch."""
    return bool(load_settings().get("permissions_onboarded", False))


def set_permissions_onboarded(done: bool) -> bool:
    update_settings({"permissions_onboarded": bool(done)})
    return bool(done)


_DEFAULT_ASSISTANT_NAME = "Jarvis"


def get_assistant_config() -> dict:
    """Assistant display name and how the assistant should address the user.

    Defaults to the JARVIS brand name and empty user name (language-aware
    addressing). Both are non-secret settings stored in config/settings.json.
    """
    data = load_settings().get("assistant")
    name = ""
    user = ""
    if isinstance(data, dict):
        name = str(data.get("assistant_name") or "").strip()
        user = str(data.get("user_name") or "").strip()
    return {
        "assistant_name": name or _DEFAULT_ASSISTANT_NAME,
        "user_name": user,
    }


def save_assistant_config(assistant_name: str, user_name: str) -> dict:
    cfg = {
        "assistant_name": (str(assistant_name or "").strip() or _DEFAULT_ASSISTANT_NAME),
        "user_name": str(user_name or "").strip(),
    }
    update_settings({"assistant": cfg})
    return cfg

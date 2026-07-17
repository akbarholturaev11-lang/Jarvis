"""core/permissions_manager.py — cross-platform OS permission onboarding facade.

JARVIS needs several macOS TCC grants to work fully: Accessibility + Automation
to drive apps and send messages, Screen Recording to see the screen, and
Microphone + Camera to hear and see. This routes status detection and
grant-prompting through the active platform adapter so the UI can present a
one-tap "grant everything" checklist at startup.

Honest by construction: statuses are "granted" / "denied" / "unknown" /
"not_required". On Windows/Linux these capabilities are not TCC-gated, so the
adapter reports "not_required" and onboarding is skipped — nothing is faked.
"""

from __future__ import annotations

from core.environment_discovery import select_platform_adapter
from core.platform_adapters.base import PERMISSION_NAMES

# User-facing order for the onboarding checklist.
PERMISSIONS: tuple[str, ...] = PERMISSION_NAMES


def _adapter():
    return select_platform_adapter()


def permission_status(name: str) -> str:
    try:
        return _adapter().permission_status(name)
    except Exception:
        return "unknown"


def all_statuses() -> dict[str, str]:
    """Current grant status for every onboarding permission."""
    try:
        adapter = _adapter()
    except Exception:
        return {name: "unknown" for name in PERMISSIONS}
    result: dict[str, str] = {}
    for name in PERMISSIONS:
        try:
            result[name] = adapter.permission_status(name)
        except Exception:
            result[name] = "unknown"
    return result


def blocking_permissions(statuses: dict[str, str] | None = None) -> list[str]:
    """Permissions confirmed NOT granted (status == 'denied').

    Only a definite "denied" is treated as blocking so startup does not nag when
    a grant simply cannot be read ("unknown", e.g. mic/camera without an
    AVFoundation binding). "not_required" (non-macOS) and "granted" are excluded.
    """
    statuses = all_statuses() if statuses is None else statuses
    return [name for name, status in statuses.items() if status == "denied"]


def any_actionable(statuses: dict[str, str] | None = None) -> bool:
    """True if any permission is denied or undetectable — i.e. the checklist can
    offer the user something to grant. Used to decide whether onboarding is useful
    at all on this OS (False on Windows/Linux where all are 'not_required')."""
    statuses = all_statuses() if statuses is None else statuses
    return any(status in ("denied", "unknown") for status in statuses.values())


def request_permission(name: str) -> tuple[bool | None, str]:
    """Trigger the real OS prompt where possible, else open the settings pane."""
    try:
        return _adapter().request_permission(name)
    except Exception as e:
        return False, f"Could not request {name}: {e}"


def open_permission_pane(name: str) -> tuple[bool, str]:
    try:
        return _adapter().open_permission_pane(name)
    except Exception as e:
        return False, f"Could not open {name} settings: {e}"

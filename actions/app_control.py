"""Cross-platform application close (graceful quit).

Counterpart to ``actions/open_app.py``. Execution is routed through the shared
platform adapters (macOS / Windows / Linux) so SessionContext stays semantic and
platform-neutral. This deliberately asks each app to *quit gracefully* — it never
force-kills by default — and it never claims success unless the adapter verified
the app is no longer running. Missing/unsupported capability returns an honest
status rather than a fake success.
"""

from __future__ import annotations

from typing import Any

from core.environment_discovery import select_platform_adapter

try:  # reuse the same alias normalization used when opening apps
    from actions.open_app import _normalize as _normalize_app_name
except Exception:  # pragma: no cover - defensive; keep close working if import shifts
    def _normalize_app_name(raw: str) -> str:
        return raw


def close_app(
    parameters: dict[str, Any] | None = None,
    response=None,
    player=None,
    device_profile: dict | None = None,
) -> str:
    """Gracefully close an application.

    Returns a status string whose wording is understood by
    ``core.session_context.infer_result_status`` for the ``close_app`` tool.
    """
    app_name = (parameters or {}).get("app_name", "").strip()
    if not app_name:
        return "No application name provided."

    normalized = _normalize_app_name(app_name)
    if player:
        try:
            player.write_log(f"[close_app] {app_name}")
        except Exception:
            pass

    try:
        adapter = select_platform_adapter()
    except Exception as e:  # pragma: no cover - adapter selection failure
        return f"Could not close {app_name}: adapter unavailable ({e})."

    try:
        ok, detail = adapter.close_app(normalized)
    except Exception as e:
        return f"Could not close {app_name}: {e}"

    detail = (detail or "").strip()
    if ok is True:
        # Explicit verified-success wording for truthful status inference.
        if "verified" in detail.lower() or "already closed" in detail.lower() or "not running" in detail.lower():
            return detail
        return f"{app_name} closed and verified. {detail}".strip()
    if ok is None:
        return (
            f"Close request sent to {app_name}, but its running state was not verified. {detail}".strip()
        )
    # ok is False → failed / unsupported. Keep the adapter's honest reason.
    return f"Could not close {app_name}: {detail or 'app close failed or is unsupported.'}"

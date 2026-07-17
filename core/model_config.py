"""Single source for Gemini model names used by helper actions.

Model IDs must not be hardcoded in several files. Resolution order for each
model is: environment variable, then `config/settings.json`, then a safe default.
This lets a deprecated/unavailable model be swapped without touching call sites.
"""

from __future__ import annotations

import os

# Vision-capable model for screenshot element finding (screen_find/screen_click)
# and any image+text request. Must accept image input.
_DEFAULT_VISION_MODEL = "gemini-2.5-flash"
# Fast text model for intent detection / normalization fallbacks.
_DEFAULT_INTENT_MODEL = "gemini-2.5-flash"


def _from_settings(key: str) -> str | None:
    try:
        from core.app_settings import load_settings
        value = str(load_settings().get(key, "") or "").strip()
        return value or None
    except Exception:
        return None


def vision_model() -> str:
    return (
        os.environ.get("JARVIS_VISION_MODEL", "").strip()
        or _from_settings("vision_model")
        or _DEFAULT_VISION_MODEL
    )


def intent_model() -> str:
    return (
        os.environ.get("JARVIS_INTENT_MODEL", "").strip()
        or _from_settings("intent_model")
        or _DEFAULT_INTENT_MODEL
    )


def is_model_unavailable_error(exc: BaseException) -> bool:
    """True when an exception means the model itself is missing/unavailable
    (HTTP 404 / NOT_FOUND / deprecated), as opposed to a normal content or
    request error. Used so a model 404 is not masked as "element not found"."""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    if code in (404, "404", "NOT_FOUND"):
        return True
    status = str(getattr(exc, "status", "") or "").upper()
    if status in ("NOT_FOUND", "PERMISSION_DENIED"):
        return True
    s = str(exc).lower()
    return (
        "not_found" in s
        or " 404" in s
        or "no longer available" in s
        or "is not found for api version" in s
        or "not found for api" in s
    )

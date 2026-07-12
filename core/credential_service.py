"""Secure application-credential boundary.

Only opaque secret values cross this module.  Callers receive fixed status
messages and must never log the returned value.  The legacy JSON file is read
only as a compatibility fallback; this module never writes, migrates, deletes,
or prints it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.secure_store import (
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SecureStore,
    create_secure_store,
)


GEMINI_SERVICE: Final = "com.jarvis.assistant.credentials"
GEMINI_ACCOUNT: Final = "gemini-api-key-v1"
_MAX_LEGACY_BYTES: Final = 128 * 1024
_VALID_STATUSES: Final = frozenset(
    {STATUS_SUCCESS, STATUS_NOT_FOUND, STATUS_NOT_AVAILABLE, STATUS_FAILED}
)


@dataclass(frozen=True, slots=True)
class CredentialResult:
    status: str
    value: str | None = field(default=None, repr=False)
    source: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("invalid credential status")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        value = "<redacted>" if self.value is not None else "None"
        return (
            f"CredentialResult(status={self.status!r}, value={value}, "
            f"source={self.source!r}, message={self.message!r})"
        )


def load_gemini_api_key(
    *,
    store: SecureStore | None = None,
    legacy_path: Path | None = None,
) -> CredentialResult:
    """Load from secure storage, with an explicitly read-only legacy fallback."""

    backend = create_secure_store() if store is None else store
    secured = backend.get(GEMINI_SERVICE, GEMINI_ACCOUNT)
    if secured.status == STATUS_SUCCESS and secured.value:
        return CredentialResult(
            STATUS_SUCCESS,
            secured.value,
            "secure_store",
            "Credential loaded from secure storage.",
        )

    legacy = _load_legacy_key(legacy_path)
    if legacy is not None:
        return CredentialResult(
            STATUS_SUCCESS,
            legacy,
            "legacy_read_only",
            "Credential loaded from the legacy read-only source.",
        )

    if secured.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
        )
    if secured.status == STATUS_FAILED:
        return CredentialResult(
            STATUS_FAILED,
            message="Secure credential storage failed.",
        )
    return CredentialResult(STATUS_NOT_FOUND, message="Credential was not found.")


def store_gemini_api_key(
    value: str,
    *,
    store: SecureStore | None = None,
) -> CredentialResult:
    """Store a Gemini key only in the platform secure store."""

    backend = create_secure_store() if store is None else store
    stored = backend.set(GEMINI_SERVICE, GEMINI_ACCOUNT, value)
    if stored.status == STATUS_SUCCESS:
        return CredentialResult(
            STATUS_SUCCESS,
            source="secure_store",
            message="Credential stored securely.",
        )
    if stored.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
        )
    return CredentialResult(STATUS_FAILED, message="Credential could not be stored.")


def require_gemini_api_key(*, legacy_path: Path | None = None) -> str:
    """Return the configured key or raise a fixed, non-secret error."""

    result = load_gemini_api_key(legacy_path=legacy_path)
    if not result.ok or result.value is None:
        raise RuntimeError("Gemini API credential is not configured securely.")
    return result.value


def _load_legacy_key(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0 or size > _MAX_LEGACY_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("gemini_api_key") if isinstance(data, dict) else None
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or "\x00" in value or len(value.encode("utf-8")) > 64 * 1024:
            return None
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


__all__ = [
    "CredentialResult",
    "GEMINI_ACCOUNT",
    "GEMINI_SERVICE",
    "load_gemini_api_key",
    "require_gemini_api_key",
    "store_gemini_api_key",
]

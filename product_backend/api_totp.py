"""RFC 6238 TOTP primitives and recovery-code helpers for admin MFA.

This module implements the time-based one-time-password algorithm with the
Python standard library only (HMAC-SHA1 dynamic truncation).  It never logs,
returns, or embeds raw secrets in an exception message and it compares codes in
constant time.  Callers layer replay protection on top of :func:`verify_totp`,
which returns the matched time step so a used step can be rejected afterwards.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import unicodedata
from typing import Final


TOTP_PERIOD_SECONDS: Final = 30
TOTP_DIGITS: Final = 6
TOTP_DRIFT_STEPS: Final = 1
TOTP_ALGORITHM: Final = "SHA1"
_TOTP_SECRET_BYTES: Final = 20  # 160-bit shared secret, per RFC 4226 guidance.
_MAX_DRIFT_STEPS: Final = 4
_RECOVERY_ALPHABET: Final = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/1/I/O/L
_RECOVERY_GROUP: Final = 5
_RECOVERY_GROUPS: Final = 2


class TotpConfigurationError(ValueError):
    """A TOTP parameter or secret was structurally invalid."""


def generate_totp_secret(num_bytes: int = _TOTP_SECRET_BYTES) -> bytes:
    if type(num_bytes) is not int or not 16 <= num_bytes <= 64:
        raise TotpConfigurationError("TOTP secret length is invalid")
    return secrets.token_bytes(num_bytes)


def base32_secret(secret: bytes) -> str:
    """Encode a raw secret as unpadded uppercase base32 for authenticator apps."""

    if type(secret) is not bytes or not 16 <= len(secret) <= 64:
        raise TotpConfigurationError("TOTP secret is invalid")
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def decode_base32_secret(value: object) -> bytes:
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise TotpConfigurationError("TOTP secret is invalid")
    padded = value.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    try:
        decoded = base64.b32decode(padded, casefold=False)
    except (ValueError, TypeError) as exc:
        raise TotpConfigurationError("TOTP secret is invalid") from exc
    if not 16 <= len(decoded) <= 64:
        raise TotpConfigurationError("TOTP secret is invalid")
    return decoded


def _hotp(secret: bytes, counter: int, digits: int) -> str:
    if counter < 0:
        raise TotpConfigurationError("TOTP counter is invalid")
    mac = hmac.new(secret, struct.pack(">Q", counter), "sha1").digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFF_FFFF
    return str(truncated % (10**digits)).zfill(digits)


def totp_code(
    secret: bytes,
    timestamp: float,
    *,
    period: int = TOTP_PERIOD_SECONDS,
    digits: int = TOTP_DIGITS,
) -> str:
    """Return the canonical TOTP code for a moment in time (for enrollment tests)."""

    if type(secret) is not bytes or not 16 <= len(secret) <= 64:
        raise TotpConfigurationError("TOTP secret is invalid")
    if type(period) is not int or not 5 <= period <= 300:
        raise TotpConfigurationError("TOTP period is invalid")
    if type(digits) is not int or not 6 <= digits <= 8:
        raise TotpConfigurationError("TOTP digit count is invalid")
    counter = int(timestamp // period)
    return _hotp(secret, counter, digits)


def _normalize_code(value: object, digits: int) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) != digits or not candidate.isascii() or not candidate.isdigit():
        return None
    return candidate


def verify_totp(
    secret: bytes,
    code: object,
    *,
    timestamp: float,
    period: int = TOTP_PERIOD_SECONDS,
    digits: int = TOTP_DIGITS,
    drift_steps: int = TOTP_DRIFT_STEPS,
) -> int | None:
    """Verify ``code`` within ``±drift_steps`` and return the matched time step.

    The returned counter lets the caller reject a replay by refusing any step at
    or below the last accepted step.  Every candidate step in the window is
    evaluated and compared with :func:`hmac.compare_digest`, without an early
    return, so acceptance does not leak the matched offset through timing.
    """

    if type(secret) is not bytes or not 16 <= len(secret) <= 64:
        return None
    if type(period) is not int or not 5 <= period <= 300:
        return None
    if type(digits) is not int or not 6 <= digits <= 8:
        return None
    if type(drift_steps) is not int or not 0 <= drift_steps <= _MAX_DRIFT_STEPS:
        return None
    candidate = _normalize_code(code, digits)
    if candidate is None:
        return None
    current_step = int(timestamp // period)
    matched: int = -1
    for offset in range(-drift_steps, drift_steps + 1):
        step = current_step + offset
        if step < 0:
            continue
        expected = _hotp(secret, step, digits)
        if hmac.compare_digest(expected, candidate):
            matched = step
    return matched if matched >= 0 else None


def provisioning_uri(
    secret: bytes,
    *,
    account_name: str,
    issuer: str,
    digits: int = TOTP_DIGITS,
    period: int = TOTP_PERIOD_SECONDS,
) -> str:
    """Build an ``otpauth://totp`` URI for QR display and manual entry."""

    from urllib.parse import quote

    if not isinstance(account_name, str) or not account_name.strip():
        raise TotpConfigurationError("TOTP account name is invalid")
    if not isinstance(issuer, str) or not issuer.strip():
        raise TotpConfigurationError("TOTP issuer is invalid")
    safe_issuer = quote(issuer.strip(), safe="")
    safe_account = quote(account_name.strip(), safe="")
    label = f"{safe_issuer}:{safe_account}"
    query = (
        f"secret={base32_secret(secret)}"
        f"&issuer={safe_issuer}"
        f"&algorithm={TOTP_ALGORITHM}"
        f"&digits={digits}"
        f"&period={period}"
    )
    return f"otpauth://totp/{label}?{query}"


def normalize_recovery_code(value: object) -> str | None:
    """Fold a recovery code to its canonical, separator-free comparison form."""

    if not isinstance(value, str) or len(value) > 64:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    normalized = normalized.replace("-", "").replace(" ", "")
    expected_length = _RECOVERY_GROUP * _RECOVERY_GROUPS
    if len(normalized) != expected_length:
        return None
    if any(character not in _RECOVERY_ALPHABET for character in normalized):
        return None
    return normalized


def generate_recovery_code() -> str:
    """Generate one display recovery code such as ``ABCDE-FGHJK``."""

    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP))
        for _ in range(_RECOVERY_GROUPS)
    ]
    return "-".join(groups)


__all__ = [
    "TOTP_DIGITS",
    "TOTP_DRIFT_STEPS",
    "TOTP_PERIOD_SECONDS",
    "TotpConfigurationError",
    "base32_secret",
    "decode_base32_secret",
    "generate_recovery_code",
    "generate_totp_secret",
    "normalize_recovery_code",
    "provisioning_uri",
    "totp_code",
    "verify_totp",
]

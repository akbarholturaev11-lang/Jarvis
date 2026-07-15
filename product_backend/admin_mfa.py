"""Encrypted admin TOTP secrets, single-use recovery codes, and MFA audit.

The TOTP shared secret is never stored in plaintext: it is sealed with
AES-256-GCM under a key derived from an operator-supplied master key that lives
only in an environment-referenced secret file (see :mod:`product_backend.runtime`).
A missing or malformed master key is fail-closed — the manager cannot be built.
Recovery codes are stored only as keyed HMAC-SHA256 digests, are single-use, and
are revoked in bulk on regeneration.  No table, audit row, log line, or exception
ever contains a raw secret, recovery code, or provisioning URI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final

from .api_auth import BackendConfigurationError
from .api_totp import (
    TOTP_DIGITS,
    TOTP_DRIFT_STEPS,
    TOTP_PERIOD_SECONDS,
    TotpConfigurationError,
    base32_secret,
    decode_base32_secret,
    generate_recovery_code,
    generate_totp_secret,
    normalize_recovery_code,
    provisioning_uri,
    verify_totp,
)
from .models import (
    PersistenceInvariantError,
    format_utc_timestamp,
    sanitize_human_text,
    validate_opaque_identifier,
)


MIN_MASTER_KEY_BYTES: Final = 32
MAX_MASTER_KEY_BYTES: Final = 128
DEFAULT_RECOVERY_CODE_COUNT: Final = 10
_ENC_KEY_LABEL: Final = b"jarvis-admin-totp-secret-encryption-v1"
_RECOVERY_HMAC_LABEL: Final = b"jarvis-admin-recovery-code-hmac-v1"
_NONCE_BYTES: Final = 12


class MfaConfigurationError(BackendConfigurationError):
    """MFA security material is absent or invalid; the subsystem cannot start."""


class MfaCryptoError(RuntimeError):
    """A sealed secret failed authenticated decryption; treat as unavailable."""


class MfaStateError(RuntimeError):
    """An MFA lifecycle transition was requested from an incompatible state."""


class MfaState(StrEnum):
    NOT_ENROLLED = "not_enrolled"
    ENROLLING = "enrolling"
    ACTIVE = "active"
    DISABLED = "disabled"


class MfaAuditEvent(StrEnum):
    ENROLLMENT_STARTED = "enrollment_started"
    ENROLLMENT_COMPLETED = "enrollment_completed"
    ENROLLMENT_FAILED = "enrollment_failed"
    MFA_DISABLED = "mfa_disabled"
    MFA_RESET = "mfa_reset"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    TOTP_FAILURE = "totp_failure"
    TOTP_REPLAY = "totp_replay"
    RECOVERY_USED = "recovery_used"
    RECOVERY_REGENERATED = "recovery_regenerated"
    SESSION_REVOKED = "session_revoked"
    SESSIONS_REVOKED_ALL = "sessions_revoked_all"
    PASSWORD_CHANGED = "password_changed"


class LoginFactorResult(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    REPLAY = "replay"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True, slots=True)
class MfaEnrollmentStart:
    subject: str
    secret_base32: str = field(repr=False)
    provisioning_uri: str = field(repr=False)
    started_at: str

    def __repr__(self) -> str:  # never expose the shared secret
        return f"MfaEnrollmentStart(subject={self.subject!r})"


@dataclass(frozen=True, slots=True)
class RecoveryCodeBatch:
    subject: str
    codes: tuple[str, ...] = field(repr=False)
    generated_at: str

    def __repr__(self) -> str:  # never expose plaintext recovery codes
        return f"RecoveryCodeBatch(subject={self.subject!r}, count={len(self.codes)})"


@dataclass(frozen=True, slots=True)
class MfaAuditEntry:
    id: str
    subject: str
    event: MfaAuditEvent
    detail: str | None
    occurred_at: str


@dataclass(frozen=True, slots=True)
class MfaStatus:
    subject: str
    state: MfaState
    activated_at: str | None
    recovery_codes_remaining: int


def _base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _from_base64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


class MfaSecretCipher:
    """AES-256-GCM sealing keyed by subkeys derived from an operator master key."""

    def __init__(self, master_key: bytes) -> None:
        if (
            type(master_key) is not bytes
            or not MIN_MASTER_KEY_BYTES <= len(master_key) <= MAX_MASTER_KEY_BYTES
        ):
            raise MfaConfigurationError("MFA master key is invalid")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except Exception as exc:  # pragma: no cover - dependency guard
            raise MfaConfigurationError(
                "authenticated encryption is unavailable"
            ) from exc
        self._enc_key = hmac.new(master_key, _ENC_KEY_LABEL, hashlib.sha256).digest()
        self._recovery_pepper = hmac.new(
            master_key, _RECOVERY_HMAC_LABEL, hashlib.sha256
        ).digest()
        self._aesgcm = AESGCM(self._enc_key)

    def seal_secret(self, subject: str, secret: bytes) -> tuple[str, str]:
        import os

        if type(secret) is not bytes or not 16 <= len(secret) <= 64:
            raise MfaCryptoError("TOTP secret is invalid")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, secret, subject.encode("utf-8"))
        return _base64(nonce), _base64(ciphertext)

    def open_secret(self, subject: str, nonce_b64: str, ciphertext_b64: str) -> bytes:
        try:
            nonce = _from_base64(nonce_b64)
            ciphertext = _from_base64(ciphertext_b64)
        except (ValueError, TypeError) as exc:
            raise MfaCryptoError("sealed secret is malformed") from exc
        if len(nonce) != _NONCE_BYTES:
            raise MfaCryptoError("sealed secret nonce is malformed")
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, subject.encode("utf-8"))
        except Exception as exc:
            raise MfaCryptoError("sealed secret authentication failed") from exc

    def hash_recovery_code(self, normalized_code: str) -> str:
        return hmac.new(
            self._recovery_pepper,
            normalized_code.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_mfa (
    subject TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('enrolling', 'active', 'disabled')),
    secret_nonce TEXT NOT NULL,
    secret_ciphertext TEXT NOT NULL,
    last_used_step INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z'),
    activated_at TEXT CHECK (activated_at IS NULL OR substr(activated_at, -1) = 'Z'),
    disabled_at TEXT CHECK (disabled_at IS NULL OR substr(disabled_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS admin_recovery_codes (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    code_hmac TEXT NOT NULL UNIQUE CHECK (
        length(code_hmac) = 64
        AND code_hmac = lower(code_hmac)
        AND code_hmac NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z'),
    used_at TEXT CHECK (used_at IS NULL OR substr(used_at, -1) = 'Z'),
    revoked_at TEXT CHECK (revoked_at IS NULL OR substr(revoked_at, -1) = 'Z')
);

CREATE INDEX IF NOT EXISTS admin_recovery_codes_subject
ON admin_recovery_codes(subject);

CREATE TABLE IF NOT EXISTS admin_mfa_audit (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL CHECK (substr(occurred_at, -1) = 'Z')
);

CREATE INDEX IF NOT EXISTS admin_mfa_audit_time
ON admin_mfa_audit(occurred_at);
"""


@dataclass(frozen=True, slots=True)
class AdminMfaSettings:
    issuer: str = "JARVIS Admin"
    mandatory: bool = True
    allow_password_only: bool = False
    recovery_code_count: int = DEFAULT_RECOVERY_CODE_COUNT
    drift_steps: int = TOTP_DRIFT_STEPS

    def __post_init__(self) -> None:
        if not isinstance(self.issuer, str) or not 1 <= len(self.issuer) <= 64:
            raise MfaConfigurationError("MFA issuer is invalid")
        if type(self.mandatory) is not bool or type(self.allow_password_only) is not bool:
            raise MfaConfigurationError("MFA enforcement flags are invalid")
        if self.mandatory and self.allow_password_only:
            raise MfaConfigurationError(
                "mandatory MFA cannot also allow a password-only bypass"
            )
        if type(self.recovery_code_count) is not int or not 6 <= self.recovery_code_count <= 24:
            raise MfaConfigurationError("recovery code count is invalid")
        if type(self.drift_steps) is not int or not 0 <= self.drift_steps <= 2:
            raise MfaConfigurationError("TOTP drift window is invalid")


class SQLiteAdminMfaManager:
    """Persistent, thread-safe MFA lifecycle with atomic replay protection."""

    def __init__(
        self,
        cipher: MfaSecretCipher,
        database: str | Path = ":memory:",
        *,
        settings: AdminMfaSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cipher, MfaSecretCipher):
            raise MfaConfigurationError("MFA cipher is required")
        self._cipher = cipher
        self._settings = settings or AdminMfaSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(database),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(_SCHEMA)

    @property
    def settings(self) -> AdminMfaSettings:
        return self._settings

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteAdminMfaManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise PersistenceInvariantError("MFA clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _audit_locked(
        self,
        subject: str,
        event: MfaAuditEvent,
        *,
        detail: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        stamp = occurred_at or format_utc_timestamp(self._now())
        clean = None
        if detail is not None:
            try:
                clean = sanitize_human_text(detail, field="detail", max_length=240)
            except Exception:
                clean = None
        self._connection.execute(
            "INSERT INTO admin_mfa_audit(id, subject, event, detail, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"mfaev_{uuid.uuid4().hex}", subject, event.value, clean, stamp),
        )

    # -- lifecycle ---------------------------------------------------------

    def state(self, subject: object) -> MfaState:
        try:
            subject = validate_opaque_identifier(subject, field="admin_subject")
        except Exception:
            return MfaState.NOT_ENROLLED
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM admin_mfa WHERE subject = ?", (subject,)
            ).fetchone()
        if row is None:
            return MfaState.NOT_ENROLLED
        return MfaState(row["state"])

    def is_active(self, subject: object) -> bool:
        return self.state(subject) is MfaState.ACTIVE

    def status(self, subject: str) -> MfaStatus:
        subject = validate_opaque_identifier(subject, field="admin_subject")
        with self._lock:
            row = self._connection.execute(
                "SELECT state, activated_at FROM admin_mfa WHERE subject = ?",
                (subject,),
            ).fetchone()
            remaining = self._connection.execute(
                "SELECT COUNT(*) AS n FROM admin_recovery_codes "
                "WHERE subject = ? AND used_at IS NULL AND revoked_at IS NULL",
                (subject,),
            ).fetchone()["n"]
        if row is None:
            return MfaStatus(subject, MfaState.NOT_ENROLLED, None, 0)
        return MfaStatus(
            subject,
            MfaState(row["state"]),
            row["activated_at"],
            int(remaining),
        )

    def begin_enrollment(self, subject: str) -> MfaEnrollmentStart:
        """Create or replace a pending secret; an active enrollment is not touched."""

        subject = validate_opaque_identifier(subject, field="admin_subject")
        secret = generate_totp_secret()
        nonce_b64, ciphertext_b64 = self._cipher.seal_secret(subject, secret)
        now = self._now()
        started_at = format_utc_timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                "SELECT state FROM admin_mfa WHERE subject = ?", (subject,)
            ).fetchone()
            if row is not None and row["state"] == MfaState.ACTIVE.value:
                raise MfaStateError("MFA is already active for this operator")
            self._connection.execute(
                "INSERT INTO admin_mfa("
                "subject, state, secret_nonce, secret_ciphertext, last_used_step, "
                "created_at, activated_at, disabled_at) "
                "VALUES (?, 'enrolling', ?, ?, -1, ?, NULL, NULL) "
                "ON CONFLICT(subject) DO UPDATE SET "
                "state = 'enrolling', secret_nonce = excluded.secret_nonce, "
                "secret_ciphertext = excluded.secret_ciphertext, last_used_step = -1, "
                "created_at = excluded.created_at, activated_at = NULL, "
                "disabled_at = NULL",
                (subject, nonce_b64, ciphertext_b64, started_at),
            )
            self._connection.execute(
                "UPDATE admin_recovery_codes SET revoked_at = ? "
                "WHERE subject = ? AND used_at IS NULL AND revoked_at IS NULL",
                (started_at, subject),
            )
            self._audit_locked(
                subject,
                MfaAuditEvent.ENROLLMENT_STARTED,
                occurred_at=started_at,
            )
        return MfaEnrollmentStart(
            subject,
            base32_secret(secret),
            provisioning_uri(
                secret,
                account_name=subject,
                issuer=self._settings.issuer,
                digits=TOTP_DIGITS,
                period=TOTP_PERIOD_SECONDS,
            ),
            started_at,
        )

    def pending_provisioning_uri(self, subject: str) -> str | None:
        """Return the otpauth URI for the current pending secret (QR rendering)."""

        subject = validate_opaque_identifier(subject, field="admin_subject")
        with self._lock:
            row = self._connection.execute(
                "SELECT state, secret_nonce, secret_ciphertext "
                "FROM admin_mfa WHERE subject = ?",
                (subject,),
            ).fetchone()
        if row is None or row["state"] != MfaState.ENROLLING.value:
            return None
        secret = self._cipher.open_secret(
            subject, row["secret_nonce"], row["secret_ciphertext"]
        )
        return provisioning_uri(
            secret,
            account_name=subject,
            issuer=self._settings.issuer,
            digits=TOTP_DIGITS,
            period=TOTP_PERIOD_SECONDS,
        )

    def activate_enrollment(
        self, subject: str, code: object
    ) -> RecoveryCodeBatch | None:
        """Confirm the first TOTP code, mark MFA active, and mint recovery codes."""

        subject = validate_opaque_identifier(subject, field="admin_subject")
        now = self._now()
        stamp = format_utc_timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                "SELECT state, secret_nonce, secret_ciphertext, last_used_step "
                "FROM admin_mfa WHERE subject = ?",
                (subject,),
            ).fetchone()
            if row is None or row["state"] != MfaState.ENROLLING.value:
                raise MfaStateError("no pending MFA enrollment for this operator")
            secret = self._cipher.open_secret(
                subject, row["secret_nonce"], row["secret_ciphertext"]
            )
            matched = verify_totp(
                secret,
                code,
                timestamp=now.timestamp(),
                drift_steps=self._settings.drift_steps,
            )
            if matched is None or matched <= row["last_used_step"]:
                self._audit_locked(
                    subject,
                    MfaAuditEvent.ENROLLMENT_FAILED,
                    occurred_at=stamp,
                )
                return None
            self._connection.execute(
                "UPDATE admin_mfa SET state = 'active', activated_at = ?, "
                "last_used_step = ?, disabled_at = NULL WHERE subject = ?",
                (stamp, matched, subject),
            )
            codes = self._replace_recovery_codes_locked(subject, stamp)
            self._audit_locked(
                subject,
                MfaAuditEvent.ENROLLMENT_COMPLETED,
                occurred_at=stamp,
            )
        return RecoveryCodeBatch(subject, codes, stamp)

    def verify_login_totp(self, subject: str, code: object) -> LoginFactorResult:
        """Atomically verify an active login TOTP and burn its time step."""

        subject = validate_opaque_identifier(subject, field="admin_subject")
        now = self._now()
        stamp = format_utc_timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                "SELECT state, secret_nonce, secret_ciphertext, last_used_step "
                "FROM admin_mfa WHERE subject = ?",
                (subject,),
            ).fetchone()
            if row is None or row["state"] != MfaState.ACTIVE.value:
                return LoginFactorResult.NOT_ACTIVE
            secret = self._cipher.open_secret(
                subject, row["secret_nonce"], row["secret_ciphertext"]
            )
            matched = verify_totp(
                secret,
                code,
                timestamp=now.timestamp(),
                drift_steps=self._settings.drift_steps,
            )
            if matched is None:
                self._audit_locked(
                    subject, MfaAuditEvent.TOTP_FAILURE, occurred_at=stamp
                )
                return LoginFactorResult.INVALID
            if matched <= row["last_used_step"]:
                self._audit_locked(
                    subject, MfaAuditEvent.TOTP_REPLAY, occurred_at=stamp
                )
                return LoginFactorResult.REPLAY
            self._connection.execute(
                "UPDATE admin_mfa SET last_used_step = ? WHERE subject = ?",
                (matched, subject),
            )
        return LoginFactorResult.ACCEPTED

    def verify_recovery_code(self, subject: str, code: object) -> LoginFactorResult:
        """Consume one single-use recovery code in a serialized transaction."""

        subject = validate_opaque_identifier(subject, field="admin_subject")
        normalized = normalize_recovery_code(code)
        stamp = format_utc_timestamp(self._now())
        with self._transaction():
            active = self._connection.execute(
                "SELECT state FROM admin_mfa WHERE subject = ?", (subject,)
            ).fetchone()
            if active is None or active["state"] != MfaState.ACTIVE.value:
                return LoginFactorResult.NOT_ACTIVE
            if normalized is None:
                self._audit_locked(
                    subject, MfaAuditEvent.TOTP_FAILURE, occurred_at=stamp
                )
                return LoginFactorResult.INVALID
            digest = self._cipher.hash_recovery_code(normalized)
            updated = self._connection.execute(
                "UPDATE admin_recovery_codes SET used_at = ? "
                "WHERE subject = ? AND code_hmac = ? "
                "AND used_at IS NULL AND revoked_at IS NULL",
                (stamp, subject, digest),
            )
            if updated.rowcount != 1:
                self._audit_locked(
                    subject, MfaAuditEvent.TOTP_FAILURE, occurred_at=stamp
                )
                return LoginFactorResult.INVALID
            self._audit_locked(
                subject, MfaAuditEvent.RECOVERY_USED, occurred_at=stamp
            )
        return LoginFactorResult.ACCEPTED

    def regenerate_recovery_codes(self, subject: str) -> RecoveryCodeBatch:
        subject = validate_opaque_identifier(subject, field="admin_subject")
        stamp = format_utc_timestamp(self._now())
        with self._transaction():
            row = self._connection.execute(
                "SELECT state FROM admin_mfa WHERE subject = ?", (subject,)
            ).fetchone()
            if row is None or row["state"] != MfaState.ACTIVE.value:
                raise MfaStateError("recovery codes require active MFA")
            codes = self._replace_recovery_codes_locked(subject, stamp)
            self._audit_locked(
                subject, MfaAuditEvent.RECOVERY_REGENERATED, occurred_at=stamp
            )
        return RecoveryCodeBatch(subject, codes, stamp)

    def disable(self, subject: str, *, reset: bool = False) -> None:
        subject = validate_opaque_identifier(subject, field="admin_subject")
        stamp = format_utc_timestamp(self._now())
        event = MfaAuditEvent.MFA_RESET if reset else MfaAuditEvent.MFA_DISABLED
        with self._transaction():
            row = self._connection.execute(
                "SELECT state FROM admin_mfa WHERE subject = ?", (subject,)
            ).fetchone()
            if row is None:
                raise MfaStateError("no MFA record for this operator")
            if reset:
                self._connection.execute(
                    "DELETE FROM admin_recovery_codes WHERE subject = ?", (subject,)
                )
                self._connection.execute(
                    "DELETE FROM admin_mfa WHERE subject = ?", (subject,)
                )
            else:
                self._connection.execute(
                    "UPDATE admin_mfa SET state = 'disabled', disabled_at = ? "
                    "WHERE subject = ?",
                    (stamp, subject),
                )
                self._connection.execute(
                    "UPDATE admin_recovery_codes SET revoked_at = ? "
                    "WHERE subject = ? AND used_at IS NULL AND revoked_at IS NULL",
                    (stamp, subject),
                )
            self._audit_locked(subject, event, occurred_at=stamp)

    def record_event(
        self,
        subject: str,
        event: MfaAuditEvent,
        *,
        detail: str | None = None,
    ) -> None:
        subject = validate_opaque_identifier(subject, field="admin_subject")
        with self._transaction():
            self._audit_locked(subject, event, detail=detail)

    def list_audit(
        self, *, subject: str | None = None, limit: int = 50
    ) -> tuple[MfaAuditEntry, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            if subject is None:
                rows = self._connection.execute(
                    "SELECT id, subject, event, detail, occurred_at "
                    "FROM admin_mfa_audit ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                subject = validate_opaque_identifier(subject, field="admin_subject")
                rows = self._connection.execute(
                    "SELECT id, subject, event, detail, occurred_at "
                    "FROM admin_mfa_audit WHERE subject = ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (subject, limit),
                ).fetchall()
        return tuple(
            MfaAuditEntry(
                row["id"],
                row["subject"],
                MfaAuditEvent(row["event"]),
                row["detail"],
                row["occurred_at"],
            )
            for row in rows
        )

    def _replace_recovery_codes_locked(
        self, subject: str, stamp: str
    ) -> tuple[str, ...]:
        self._connection.execute(
            "UPDATE admin_recovery_codes SET revoked_at = ? "
            "WHERE subject = ? AND used_at IS NULL AND revoked_at IS NULL",
            (stamp, subject),
        )
        batch_id = f"rcb_{uuid.uuid4().hex}"
        codes: list[str] = []
        seen: set[str] = set()
        while len(codes) < self._settings.recovery_code_count:
            code = generate_recovery_code()
            normalized = normalize_recovery_code(code)
            if normalized is None or normalized in seen:
                continue
            digest = self._cipher.hash_recovery_code(normalized)
            try:
                self._connection.execute(
                    "INSERT INTO admin_recovery_codes("
                    "id, subject, batch_id, code_hmac, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"rc_{uuid.uuid4().hex}", subject, batch_id, digest, stamp),
                )
            except sqlite3.IntegrityError:
                continue
            seen.add(normalized)
            codes.append(code)
        return tuple(codes)


__all__ = [
    "DEFAULT_RECOVERY_CODE_COUNT",
    "AdminMfaSettings",
    "LoginFactorResult",
    "MfaAuditEntry",
    "MfaAuditEvent",
    "MfaConfigurationError",
    "MfaCryptoError",
    "MfaEnrollmentStart",
    "MfaSecretCipher",
    "MfaState",
    "MfaStateError",
    "MfaStatus",
    "RecoveryCodeBatch",
    "SQLiteAdminMfaManager",
]

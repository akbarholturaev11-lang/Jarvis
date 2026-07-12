"""Short-lived, single-use device proof challenges.

The Ed25519 primitive in :mod:`core.device_identity` proves possession of the
per-install private key.  This service supplies the missing server guarantees:
an unpredictable nonce, license/action/resource binding, a bounded TTL, active
device lookup, and atomic one-time consumption.  It stores only a nonce digest,
never the raw nonce, public key, signature, or private material.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final

from core.device_identity import (
    STATUS_NOT_AVAILABLE as DEVICE_STATUS_NOT_AVAILABLE,
    verify_device_challenge,
)

from .models import (
    InstallAuthorization,
    InstallMode,
    PersistenceInvariantError,
    VerifiedDevicePrincipal,
    format_utc_timestamp,
    normalize_utc_timestamp,
    validate_device_key_fingerprint,
    validate_opaque_identifier,
)
from .repository import CommerceRepository


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID: Final = "invalid"
STATUS_EXPIRED: Final = "expired"
STATUS_ALREADY_USED: Final = "already_used"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

DEFAULT_CHALLENGE_TTL_SECONDS: Final = 120
MIN_CHALLENGE_TTL_SECONDS: Final = 30
MAX_CHALLENGE_TTL_SECONDS: Final = 300
_NONCE_BYTES: Final = 32
_ED25519_PUBLIC_KEY_BASE64URL_LENGTH: Final = 43
_ED25519_SIGNATURE_BASE64URL_LENGTH: Final = 86


class DeviceChallengeAction(StrEnum):
    AUTHORIZE_INSTALL = "authorize_install"
    DOWNLOAD_ARTIFACT = "download_artifact"
    SUBMIT_PAYMENT = "submit_payment"
    FETCH_ENTITLEMENT = "fetch_entitlement"
    REPLACE_DEVICE = "replace_device"


@dataclass(frozen=True, slots=True)
class IssuedDeviceChallenge:
    id: str
    license_id: str = field(repr=False)
    action: DeviceChallengeAction
    resource_id: str = field(repr=False)
    challenge_nonce: str = field(repr=False)
    issued_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class VerifiedDeviceChallenge:
    challenge_id: str
    license_id: str = field(repr=False)
    action: DeviceChallengeAction
    resource_id: str = field(repr=False)
    device_principal: VerifiedDevicePrincipal = field(repr=False)
    verified_at: str


@dataclass(frozen=True, slots=True)
class DeviceChallengeResult:
    status: str
    message: str = field(repr=False)
    issued: IssuedDeviceChallenge | None = field(default=None, repr=False)
    verified: VerifiedDeviceChallenge | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        valid = {
            STATUS_SUCCESS,
            STATUS_NOT_FOUND,
            STATUS_INVALID,
            STATUS_EXPIRED,
            STATUS_ALREADY_USED,
            STATUS_NOT_AVAILABLE,
            STATUS_FAILED,
        }
        if self.status not in valid:
            raise ValueError("unsupported device challenge status")
        payload_count = int(self.issued is not None) + int(self.verified is not None)
        if (self.status == STATUS_SUCCESS) != (payload_count == 1):
            raise ValueError("only successful challenge operations carry a result")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        payload = "issued" if self.issued else "verified" if self.verified else "none"
        return f"DeviceChallengeResult(status={self.status!r}, payload={payload!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class DeviceInstallAuthorizationResult:
    """Result of the combined proof-consumption/install-authorization boundary."""

    status: str
    authorization: InstallAuthorization | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return (
            self.status == STATUS_SUCCESS
            and self.authorization is not None
            and self.authorization.allowed
        )

    def __repr__(self) -> str:
        return (
            f"DeviceInstallAuthorizationResult(status={self.status!r}, "
            f"authorized={self.ok!r})"
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_challenges (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL,
    device_key_fingerprint TEXT NOT NULL CHECK (
        length(device_key_fingerprint) = 71
        AND substr(device_key_fingerprint, 1, 7) = 'sha256:'
        AND device_key_fingerprint = lower(device_key_fingerprint)
        AND substr(device_key_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    action TEXT NOT NULL CHECK (action IN (
        'authorize_install', 'download_artifact', 'submit_payment',
        'fetch_entitlement', 'replace_device'
    )),
    resource_id TEXT NOT NULL,
    nonce_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(nonce_sha256) = 64
        AND nonce_sha256 = lower(nonce_sha256)
        AND nonce_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    issued_at TEXT NOT NULL CHECK (substr(issued_at, -1) = 'Z'),
    expires_at TEXT NOT NULL CHECK (substr(expires_at, -1) = 'Z'),
    consumed_at TEXT CHECK (consumed_at IS NULL OR substr(consumed_at, -1) = 'Z'),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN (
        'attempted', 'success', 'invalid', 'expired'
    )),
    CHECK (
        (consumed_at IS NULL AND outcome IS NULL)
        OR (consumed_at IS NOT NULL AND outcome IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS device_challenges_expiry
ON device_challenges(expires_at);
"""


class SQLiteDeviceChallengeService:
    """Persistent challenge service with atomic consumption across processes."""

    def __init__(
        self,
        commerce_repository: CommerceRepository,
        database: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[int], bytes] | None = None,
        ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
    ) -> None:
        if not isinstance(commerce_repository, CommerceRepository):
            raise TypeError("commerce_repository must implement CommerceRepository")
        if (
            type(ttl_seconds) is not int
            or not MIN_CHALLENGE_TTL_SECONDS
            <= ttl_seconds
            <= MAX_CHALLENGE_TTL_SECONDS
        ):
            raise ValueError("device challenge TTL is outside the allowed range")
        self._commerce = commerce_repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce_factory = nonce_factory or secrets.token_bytes
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(database),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteDeviceChallengeService:
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
            raise PersistenceInvariantError("challenge clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def issue(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> DeviceChallengeResult:
        """Issue a challenge only for the license's current bound device."""

        try:
            license_id = validate_opaque_identifier(license_id, field="license_id")
            fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
            resource_id = validate_opaque_identifier(resource_id, field="resource_id")
            if type(action) is not DeviceChallengeAction:
                raise ValueError("action must be a DeviceChallengeAction")
        except (TypeError, ValueError):
            return DeviceChallengeResult(STATUS_INVALID, "Device challenge request is invalid.")

        binding = self._commerce.get_active_device(license_id)
        if binding is None or binding.device_key_fingerprint != fingerprint:
            return DeviceChallengeResult(STATUS_NOT_FOUND, "Active device was not found.")

        now = self._now()
        issued_at = format_utc_timestamp(now)
        expires_at = format_utc_timestamp(now + timedelta(seconds=self._ttl_seconds))
        for _attempt in range(3):
            nonce = self._nonce_factory(_NONCE_BYTES)
            if type(nonce) is not bytes or len(nonce) != _NONCE_BYTES:
                return DeviceChallengeResult(
                    STATUS_FAILED, "Device challenge could not be created."
                )
            nonce_text = _base64url(nonce)
            nonce_digest = hashlib.sha256(nonce).hexdigest()
            challenge_id = f"chl_{uuid.uuid4().hex}"
            try:
                with self._transaction():
                    self._connection.execute(
                        "INSERT INTO device_challenges("
                        "id, license_id, device_key_fingerprint, action, resource_id, "
                        "nonce_sha256, issued_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            challenge_id,
                            license_id,
                            fingerprint,
                            action.value,
                            resource_id,
                            nonce_digest,
                            issued_at,
                            expires_at,
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            issued = IssuedDeviceChallenge(
                challenge_id,
                license_id,
                action,
                resource_id,
                nonce_text,
                issued_at,
                expires_at,
            )
            return DeviceChallengeResult(
                STATUS_SUCCESS,
                "Device challenge issued.",
                issued=issued,
            )
        return DeviceChallengeResult(STATUS_FAILED, "Device challenge could not be created.")

    def verify_and_consume(
        self,
        *,
        challenge_id: str,
        challenge_nonce: str,
        public_key_base64: str,
        signature_base64: str,
    ) -> DeviceChallengeResult:
        """Atomically consume a challenge, then verify proof and current binding."""

        try:
            challenge_id = validate_opaque_identifier(
                challenge_id, field="challenge_id"
            )
            nonce = _strict_nonce_bytes(challenge_nonce)
        except (TypeError, ValueError):
            return DeviceChallengeResult(STATUS_INVALID, "Device challenge is invalid.")

        now = self._now()
        consumed_at = format_utc_timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM device_challenges WHERE id = ?", (challenge_id,)
            ).fetchone()
            if row is None:
                return DeviceChallengeResult(
                    STATUS_NOT_FOUND, "Device challenge was not found."
                )
            if row["consumed_at"] is not None:
                return DeviceChallengeResult(
                    STATUS_ALREADY_USED, "Device challenge was already used."
                )
            expires_at = _parse_utc(row["expires_at"])
            if now >= expires_at:
                self._connection.execute(
                    "UPDATE device_challenges SET consumed_at = ?, outcome = 'expired' "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (consumed_at, challenge_id),
                )
                return DeviceChallengeResult(STATUS_EXPIRED, "Device challenge expired.")
            if not secrets.compare_digest(
                hashlib.sha256(nonce).hexdigest(), row["nonce_sha256"]
            ):
                self._connection.execute(
                    "UPDATE device_challenges SET consumed_at = ?, outcome = 'invalid' "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (consumed_at, challenge_id),
                )
                return DeviceChallengeResult(STATUS_INVALID, "Device challenge is invalid.")
            self._connection.execute(
                "UPDATE device_challenges SET consumed_at = ?, outcome = 'attempted' "
                "WHERE id = ? AND consumed_at IS NULL",
                (consumed_at, challenge_id),
            )
            row_data = dict(row)

        if (
            type(public_key_base64) is not str
            or len(public_key_base64) != _ED25519_PUBLIC_KEY_BASE64URL_LENGTH
            or type(signature_base64) is not str
            or len(signature_base64) != _ED25519_SIGNATURE_BASE64URL_LENGTH
        ):
            self._set_outcome(challenge_id, "invalid")
            return DeviceChallengeResult(STATUS_INVALID, "Device proof is invalid.")

        proof = verify_device_challenge(
            public_key_base64=public_key_base64,
            device_key_fingerprint=row_data["device_key_fingerprint"],
            challenge_nonce=challenge_nonce,
            signature_base64=signature_base64,
        )
        if proof.status == DEVICE_STATUS_NOT_AVAILABLE:
            self._set_outcome(challenge_id, "invalid")
            return DeviceChallengeResult(
                STATUS_NOT_AVAILABLE, "Device proof verification is not available."
            )
        if not proof.ok:
            self._set_outcome(challenge_id, "invalid")
            return DeviceChallengeResult(STATUS_INVALID, "Device proof is invalid.")

        binding = self._commerce.get_active_device(row_data["license_id"])
        if (
            binding is None
            or binding.device_key_fingerprint
            != row_data["device_key_fingerprint"]
        ):
            self._set_outcome(challenge_id, "invalid")
            return DeviceChallengeResult(STATUS_INVALID, "Device binding changed.")

        principal = VerifiedDevicePrincipal(
            device_key_fingerprint=binding.device_key_fingerprint,
            platform=binding.platform,
            architecture=binding.architecture,
            proof_verified=True,
        )
        verified_at = format_utc_timestamp(self._now())
        self._set_outcome(challenge_id, "success")
        verified = VerifiedDeviceChallenge(
            challenge_id,
            row_data["license_id"],
            DeviceChallengeAction(row_data["action"]),
            row_data["resource_id"],
            principal,
            verified_at,
        )
        return DeviceChallengeResult(
            STATUS_SUCCESS,
            "Device challenge verified.",
            verified=verified,
        )

    def verify_and_authorize_install(
        self,
        *,
        challenge_id: str,
        challenge_nonce: str,
        public_key_base64: str,
        signature_base64: str,
        artifact_id: str,
        install_mode: InstallMode,
        source_version: str | None = None,
        source_build: int | None = None,
    ) -> DeviceInstallAuthorizationResult:
        """Consume device proof and authorize its bound artifact in one call.

        This is the public service boundary for install authorization.  Network
        callers never submit or construct a ``VerifiedDevicePrincipal``.
        """

        proof = self.verify_and_consume(
            challenge_id=challenge_id,
            challenge_nonce=challenge_nonce,
            public_key_base64=public_key_base64,
            signature_base64=signature_base64,
        )
        if not proof.ok or proof.verified is None:
            return DeviceInstallAuthorizationResult(
                proof.status,
                message="Device proof was not accepted.",
            )
        verified = proof.verified
        try:
            normalized_artifact_id = validate_opaque_identifier(
                artifact_id, field="artifact_id"
            )
        except (TypeError, ValueError):
            return DeviceInstallAuthorizationResult(
                STATUS_INVALID, message="Install request is invalid."
            )
        if (
            verified.action is not DeviceChallengeAction.AUTHORIZE_INSTALL
            or not secrets.compare_digest(
                verified.resource_id, normalized_artifact_id
            )
        ):
            return DeviceInstallAuthorizationResult(
                STATUS_INVALID,
                message="Device challenge is not bound to this install.",
            )
        try:
            authorization = self._commerce.authorize_install(
                verified.license_id,
                device_principal=verified.device_principal,
                artifact_id=normalized_artifact_id,
                install_mode=install_mode,
                source_version=source_version,
                source_build=source_build,
            )
        except (TypeError, ValueError):
            return DeviceInstallAuthorizationResult(
                STATUS_INVALID, message="Install request is invalid."
            )
        return DeviceInstallAuthorizationResult(
            STATUS_SUCCESS,
            authorization=authorization,
            message="Install authorization evaluated.",
        )

    def _set_outcome(self, challenge_id: str, outcome: str) -> None:
        with self._transaction():
            self._connection.execute(
                "UPDATE device_challenges SET outcome = ? "
                "WHERE id = ? AND consumed_at IS NOT NULL",
                (outcome, challenge_id),
            )


def _base64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _strict_nonce_bytes(value: object) -> bytes:
    import base64
    import binascii

    if type(value) is not str or len(value) != 43:
        raise ValueError("nonce must be canonical base64url")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("nonce must be canonical base64url") from exc
    if len(decoded) != _NONCE_BYTES or _base64url(decoded) != value:
        raise ValueError("nonce must be canonical base64url")
    return decoded


def _parse_utc(value: str) -> datetime:
    normalized = normalize_utc_timestamp(value, field="challenge_timestamp")
    return datetime.fromisoformat(normalized[:-1] + "+00:00")


__all__ = [
    "DEFAULT_CHALLENGE_TTL_SECONDS",
    "MAX_CHALLENGE_TTL_SECONDS",
    "MIN_CHALLENGE_TTL_SECONDS",
    "STATUS_ALREADY_USED",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "DeviceChallengeAction",
    "DeviceChallengeResult",
    "DeviceInstallAuthorizationResult",
    "IssuedDeviceChallenge",
    "SQLiteDeviceChallengeService",
    "VerifiedDeviceChallenge",
]

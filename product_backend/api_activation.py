"""Persistent, one-time client activation credentials and proof challenges.

Raw activation credentials and challenge nonces are returned only to the
issuing boundary.  SQLite retains keyed/deterministic digests and bounded
metadata, never reusable raw credentials, nonces, signatures, or public keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from core.device_identity import STATUS_SUCCESS, verify_device_challenge
from core.product_version import PRODUCT_ID

from .api_auth import BackendConfigurationError
from .api_ports import EntitlementCertificateSigner
from .models import (
    ConflictError,
    PersistenceInvariantError,
    format_utc_timestamp,
    normalize_semver,
    normalize_target_architecture,
    normalize_target_platform,
    validate_device_key_fingerprint,
    validate_opaque_identifier,
)
from .repository import CommerceRepository


DEFAULT_ACTIVATION_CHALLENGE_TTL_SECONDS: Final = 120
DEFAULT_ACTIVATION_CREDENTIAL_TTL_SECONDS: Final = 7 * 24 * 60 * 60
_MIN_CREDENTIAL_SECRET_BYTES: Final = 32
_ACTIVATION_KEY_PREFIX: Final = "jv1_"


class ActivationRejectedError(RuntimeError):
    """Activation authority, context, or proof was rejected."""


class ActivationNotAvailableError(RuntimeError):
    """A required activation dependency cannot safely complete the request."""


@dataclass(frozen=True, slots=True)
class IssuedActivationCredential:
    credential_id: str
    license_id: str
    version: str
    license_key: str = field(repr=False)
    issued_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class IssuedActivationChallenge:
    challenge_id: str
    challenge_nonce: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ActivationCompletion:
    license_id: str
    entitlement_certificate: str = field(repr=False)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activation_credentials (
    id TEXT PRIMARY KEY,
    credential_digest TEXT NOT NULL UNIQUE CHECK(length(credential_digest) = 64),
    license_id TEXT NOT NULL,
    version TEXT NOT NULL,
    issued_at TEXT NOT NULL CHECK(substr(issued_at, -1) = 'Z'),
    expires_at TEXT NOT NULL CHECK(substr(expires_at, -1) = 'Z'),
    consumed_at TEXT CHECK(consumed_at IS NULL OR substr(consumed_at, -1) = 'Z')
);

CREATE INDEX IF NOT EXISTS activation_credentials_expiry
ON activation_credentials(expires_at);

CREATE TABLE IF NOT EXISTS activation_challenges (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL REFERENCES activation_credentials(id),
    license_id TEXT NOT NULL,
    version TEXT NOT NULL,
    device_key_fingerprint TEXT NOT NULL,
    platform TEXT NOT NULL,
    architecture TEXT NOT NULL,
    nonce_sha256 TEXT NOT NULL UNIQUE CHECK(length(nonce_sha256) = 64),
    issued_at TEXT NOT NULL CHECK(substr(issued_at, -1) = 'Z'),
    expires_at TEXT NOT NULL CHECK(substr(expires_at, -1) = 'Z'),
    consumed_at TEXT CHECK(consumed_at IS NULL OR substr(consumed_at, -1) = 'Z'),
    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('attempted', 'expired')),
    CHECK (
        (consumed_at IS NULL AND outcome IS NULL)
        OR (consumed_at IS NOT NULL AND outcome IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS activation_one_live_challenge
ON activation_challenges(credential_id) WHERE consumed_at IS NULL;
"""


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url(value: object, *, expected_bytes: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ActivationRejectedError("Activation material is invalid.")
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ActivationRejectedError("Activation material is invalid.") from exc
    if len(decoded) != expected_bytes or _base64url(decoded) != value:
        raise ActivationRejectedError("Activation material is invalid.")
    return decoded


class SQLiteClientActivationService:
    """One-time activation authority backed by a dedicated SQLite database."""

    def __init__(
        self,
        commerce: CommerceRepository,
        certificate_signer: EntitlementCertificateSigner,
        credential_pepper: bytes,
        database: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
        challenge_ttl_seconds: int = DEFAULT_ACTIVATION_CHALLENGE_TTL_SECONDS,
        credential_ttl_seconds: int = DEFAULT_ACTIVATION_CREDENTIAL_TTL_SECONDS,
        max_live_credentials: int = 4096,
    ) -> None:
        if not isinstance(commerce, CommerceRepository):
            raise BackendConfigurationError("commerce repository is invalid")
        if not isinstance(certificate_signer, EntitlementCertificateSigner):
            raise BackendConfigurationError("activation certificate signer is missing")
        if type(credential_pepper) is not bytes or len(credential_pepper) < 32:
            raise BackendConfigurationError("activation credential pepper is invalid")
        if not 30 <= challenge_ttl_seconds <= 300:
            raise BackendConfigurationError("activation challenge TTL is invalid")
        if not 300 <= credential_ttl_seconds <= 31 * 24 * 60 * 60:
            raise BackendConfigurationError("activation credential TTL is invalid")
        if not 1 <= max_live_credentials <= 100_000:
            raise BackendConfigurationError("activation credential bound is invalid")
        self._commerce = commerce
        self._signer = certificate_signer
        self._pepper = credential_pepper
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._credential_ttl_seconds = credential_ttl_seconds
        self._max_live_credentials = max_live_credentials
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(database), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteClientActivationService:
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
            raise PersistenceInvariantError("activation clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _credential_digest(self, license_key: str) -> str:
        return hmac.new(
            self._pepper,
            b"jarvis-activation-credential\x00" + license_key.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def issue_activation_credential(
        self,
        *,
        license_id: str,
        version: str,
    ) -> IssuedActivationCredential:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        version = normalize_semver(version)
        if self._commerce.get_entitlement(license_id, version) is None:
            raise ActivationRejectedError("Exact-version entitlement is required.")
        now = self._now()
        issued_at = format_utc_timestamp(now)
        expires_at = format_utc_timestamp(
            now + timedelta(seconds=self._credential_ttl_seconds)
        )
        with self._transaction():
            live = self._connection.execute(
                "SELECT COUNT(*) FROM activation_credentials "
                "WHERE consumed_at IS NULL AND expires_at > ?",
                (issued_at,),
            ).fetchone()[0]
            if live >= self._max_live_credentials:
                raise ActivationNotAvailableError(
                    "Activation credential capacity is reached."
                )
            for _attempt in range(3):
                raw_key = _ACTIVATION_KEY_PREFIX + _base64url(
                    secrets.token_bytes(_MIN_CREDENTIAL_SECRET_BYTES)
                )
                credential_id = f"act_{uuid.uuid4().hex}"
                try:
                    self._connection.execute(
                        "INSERT INTO activation_credentials("
                        "id, credential_digest, license_id, version, issued_at, expires_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            credential_id,
                            self._credential_digest(raw_key),
                            license_id,
                            version,
                            issued_at,
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                return IssuedActivationCredential(
                    credential_id,
                    license_id,
                    version,
                    raw_key,
                    issued_at,
                    expires_at,
                )
        raise ActivationNotAvailableError("Activation credential could not be issued.")

    def create_activation_challenge(
        self,
        *,
        product_id: str,
        license_key: str,
        device_key_fingerprint: str,
        device_public_key: str,
        version: str,
        platform: str,
        architecture: str,
    ) -> IssuedActivationChallenge:
        if product_id != PRODUCT_ID:
            raise ActivationRejectedError("Activation product is invalid.")
        if (
            not isinstance(license_key, str)
            or not 8 <= len(license_key) <= 256
            or license_key != license_key.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in license_key)
        ):
            raise ActivationRejectedError("Activation credential is invalid.")
        fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
        public_key = _decode_base64url(device_public_key, expected_bytes=32)
        if not hmac.compare_digest(
            fingerprint, "sha256:" + hashlib.sha256(public_key).hexdigest()
        ):
            raise ActivationRejectedError("Device identity is invalid.")
        version = normalize_semver(version)
        platform = normalize_target_platform(platform)
        architecture = normalize_target_architecture(architecture)
        now = self._now()
        issued_at = format_utc_timestamp(now)
        expires_at = format_utc_timestamp(
            now + timedelta(seconds=self._challenge_ttl_seconds)
        )
        credential_digest = self._credential_digest(license_key)
        for _attempt in range(3):
            nonce = secrets.token_bytes(32)
            challenge_id = f"ach_{uuid.uuid4().hex}"
            with self._transaction():
                row = self._connection.execute(
                    "SELECT * FROM activation_credentials "
                    "WHERE credential_digest = ?",
                    (credential_digest,),
                ).fetchone()
                if (
                    row is None
                    or row["consumed_at"] is not None
                    or row["expires_at"] <= issued_at
                    or row["version"] != version
                ):
                    raise ActivationRejectedError(
                        "Activation credential is unavailable."
                    )
                if self._commerce.get_entitlement(row["license_id"], version) is None:
                    raise ActivationRejectedError(
                        "Exact-version entitlement is required."
                    )
                self._connection.execute(
                    "UPDATE activation_challenges SET consumed_at = ?, outcome = 'expired' "
                    "WHERE credential_id = ? AND consumed_at IS NULL AND expires_at <= ?",
                    (issued_at, row["id"], issued_at),
                )
                try:
                    self._connection.execute(
                        "INSERT INTO activation_challenges("
                        "id, credential_id, license_id, version, "
                        "device_key_fingerprint, platform, architecture, "
                        "nonce_sha256, issued_at, expires_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            challenge_id,
                            row["id"],
                            row["license_id"],
                            version,
                            fingerprint,
                            platform,
                            architecture,
                            hashlib.sha256(nonce).hexdigest(),
                            issued_at,
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if "activation_one_live_challenge" in str(exc):
                        raise ConflictError(
                            "activation credential already has a live challenge"
                        ) from exc
                    continue
                return IssuedActivationChallenge(challenge_id, _base64url(nonce))
        raise ActivationNotAvailableError("Activation challenge could not be issued.")

    def complete_activation(
        self,
        *,
        product_id: str,
        challenge_id: str,
        challenge_nonce: str,
        device_key_fingerprint: str,
        device_public_key: str,
        challenge_signature: str,
        version: str,
        platform: str,
        architecture: str,
    ) -> ActivationCompletion:
        if product_id != PRODUCT_ID:
            raise ActivationRejectedError("Activation product is invalid.")
        challenge_id = validate_opaque_identifier(
            challenge_id, field="challenge_id"
        )
        nonce = _decode_base64url(challenge_nonce, expected_bytes=32)
        fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
        public_key = _decode_base64url(device_public_key, expected_bytes=32)
        _decode_base64url(challenge_signature, expected_bytes=64)
        if not hmac.compare_digest(
            fingerprint, "sha256:" + hashlib.sha256(public_key).hexdigest()
        ):
            raise ActivationRejectedError("Device identity is invalid.")
        version = normalize_semver(version)
        platform = normalize_target_platform(platform)
        architecture = normalize_target_architecture(architecture)
        now = self._now()
        consumed_at = format_utc_timestamp(now)
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM activation_challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                raise ActivationRejectedError("Activation challenge is unavailable.")
            outcome = "expired" if row["expires_at"] <= consumed_at else "attempted"
            self._connection.execute(
                "UPDATE activation_challenges SET consumed_at = ?, outcome = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (consumed_at, outcome, challenge_id),
            )
            if outcome == "expired":
                raise ActivationRejectedError("Activation challenge expired.")
            if (
                not hmac.compare_digest(
                    row["nonce_sha256"], hashlib.sha256(nonce).hexdigest()
                )
                or row["device_key_fingerprint"] != fingerprint
                or row["version"] != version
                or row["platform"] != platform
                or row["architecture"] != architecture
            ):
                raise ActivationRejectedError("Activation context is invalid.")
            credential_id = row["credential_id"]
            license_id = row["license_id"]

        proof = verify_device_challenge(
            public_key_base64=device_public_key,
            device_key_fingerprint=fingerprint,
            challenge_nonce=challenge_nonce,
            signature_base64=challenge_signature,
        )
        if proof.status != STATUS_SUCCESS:
            raise ActivationRejectedError("Device proof is invalid.")
        if self._commerce.get_entitlement(license_id, version) is None:
            raise ActivationRejectedError("Exact-version entitlement is required.")

        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE activation_credentials SET consumed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL AND expires_at > ?",
                (consumed_at, credential_id, consumed_at),
            )
            if cursor.rowcount != 1:
                raise ActivationRejectedError("Activation credential is unavailable.")

        self._commerce.activate_device(
            license_id,
            fingerprint,
            platform=platform,
            architecture=architecture,
        )
        certificate = self._sign_certificate(
            license_id=license_id,
            device_key_fingerprint=fingerprint,
            version=version,
            issued_at=consumed_at,
        )
        return ActivationCompletion(license_id, certificate)

    def issue_entitlement_certificate(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        version: str,
    ) -> str:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
        version = normalize_semver(version)
        if self._commerce.get_entitlement(license_id, version) is None:
            raise ActivationRejectedError("Exact-version entitlement is required.")
        active = self._commerce.get_active_device(license_id)
        if active is None or active.device_key_fingerprint != fingerprint:
            raise ActivationRejectedError("Active device is required.")
        return self._sign_certificate(
            license_id=license_id,
            device_key_fingerprint=fingerprint,
            version=version,
            issued_at=format_utc_timestamp(self._now()),
        )

    def _sign_certificate(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        version: str,
        issued_at: str,
    ) -> str:
        try:
            certificate = self._signer.sign_entitlement_certificate(
                license_id=license_id,
                device_key_fingerprint=device_key_fingerprint,
                version=version,
                issued_at=issued_at,
            )
        except Exception as exc:
            raise ActivationNotAvailableError(
                "Entitlement certificate signing is unavailable."
            ) from exc
        if (
            not isinstance(certificate, str)
            or not 32 <= len(certificate) <= 32 * 1024
            or any(character in certificate for character in "\x00\r\n")
        ):
            raise ActivationNotAvailableError(
                "Entitlement certificate signing returned invalid data."
            )
        return certificate


__all__ = [
    "ActivationCompletion",
    "ActivationNotAvailableError",
    "ActivationRejectedError",
    "IssuedActivationChallenge",
    "IssuedActivationCredential",
    "SQLiteClientActivationService",
]

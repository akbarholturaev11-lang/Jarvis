"""Durable, encrypted client payment-request envelopes.

A payment submission is not atomic across the network: the server may accept and
persist a payment while the client never sees the response, or the app may be
restarted between picking a screenshot and confirming the submission.  To submit
idempotently after a lost response or a restart, the client must durably remember
two things: the exact idempotency key it used, and the exact sanitized screenshot
bytes it sent (the server matches an idempotent retry on both the submission id
and the evidence digest).

This module keeps that state without ever writing a secret, a screenshot, or
sensitive customer context to a plaintext file:

* The envelope metadata (idempotency key, release/device/customer context, proof
  digest, state and timestamps, and the per-envelope data key) is stored as one
  small JSON secret inside the platform :class:`SecureStore` -- the OS keychain,
  Credential Manager, or Secret Service.
* The screenshot bytes are AES-256-GCM encrypted with that per-envelope data key
  and written to a private, ``0o600`` file.  The plaintext never touches the
  disk, and deleting the keychain secret cryptographically shreds the blob.

If the platform secure store is unavailable the store returns an honest
``not_available`` and stores nothing, so no purchase silently loses durability.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from core.secure_store import (
    STATUS_NOT_AVAILABLE as STORE_NOT_AVAILABLE,
    STATUS_NOT_FOUND as STORE_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
)


try:  # pragma: no cover - exercised through a patched boundary
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
except ImportError:  # pragma: no cover
    _AESGCM = None


ENVELOPE_SCHEMA: Final = "jarvis.payment-request.v1"
MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES: Final = 10 * 1024 * 1024
_DEK_BYTES: Final = 32
_NONCE_BYTES: Final = 12
_MAX_METADATA_BYTES: Final = 48 * 1024

DEFAULT_ENVELOPE_SERVICE: Final = "com.jarvis.assistant.product"
DEFAULT_ENVELOPE_ACCOUNT: Final = "pending-payment-request-v1"

_SUBMISSION_ID_RE: Final = re.compile(r"purchase_[0-9a-f]{32}")
_OPAQUE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_SEMVER_RE: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_UTC_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)
_CONTENT_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp"})


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_CORRUPT: Final = "corrupt"
STATUS_INVALID: Final = "invalid"
STATUS_FAILED: Final = "failed"


class PaymentRequestKind(StrEnum):
    INITIAL = "initial"
    UPDATE = "update"


class PaymentRequestState(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    FAILED = "failed"


def _utc(value: object) -> str:
    if type(value) is not str or _UTC_RE.fullmatch(value) is None:
        raise ValueError("payment request timestamp is invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("payment request timestamp must be UTC")
    return value


@dataclass(frozen=True, slots=True)
class PaymentRequestEnvelope:
    """Durable, non-secret description of one in-flight payment submission.

    The screenshot bytes are never held here; ``proof_sha256`` is the safe
    reference that binds this envelope to the exact evidence that was, or will
    be, submitted under ``idempotency_key``.
    """

    idempotency_key: str
    kind: PaymentRequestKind
    release_id: str = field(repr=False)
    device_fingerprint: str = field(repr=False)
    proof_sha256: str = field(repr=False)
    content_type: str
    paid_at: str = field(repr=False)
    version: str
    state: PaymentRequestState
    created_at: str = field(repr=False)
    updated_at: str = field(repr=False)
    purchase_id: str | None = field(default=None, repr=False)
    license_id: str | None = field(default=None, repr=False)
    supersedes_payment_id: str | None = field(default=None, repr=False)
    payment_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if _SUBMISSION_ID_RE.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency key is invalid")
        if not isinstance(self.kind, PaymentRequestKind):
            raise ValueError("payment request kind is invalid")
        if not isinstance(self.state, PaymentRequestState):
            raise ValueError("payment request state is invalid")
        if _OPAQUE_ID_RE.fullmatch(self.release_id) is None:
            raise ValueError("release id is invalid")
        if (
            type(self.device_fingerprint) is not str
            or not 1 <= len(self.device_fingerprint) <= 256
        ):
            raise ValueError("device fingerprint is invalid")
        if _SHA256_RE.fullmatch(self.proof_sha256) is None:
            raise ValueError("proof digest is invalid")
        if self.content_type not in _CONTENT_TYPES:
            raise ValueError("content type is invalid")
        if _SEMVER_RE.fullmatch(self.version) is None:
            raise ValueError("version is invalid")
        _utc(self.paid_at)
        _utc(self.created_at)
        _utc(self.updated_at)
        for opaque in (self.purchase_id, self.license_id, self.payment_id):
            if opaque is not None and _OPAQUE_ID_RE.fullmatch(opaque) is None:
                raise ValueError("payment request identifier is invalid")
        if self.purchase_id is None and self.license_id is None:
            raise ValueError("payment request needs purchase or license context")
        if self.supersedes_payment_id is not None and (
            _OPAQUE_ID_RE.fullmatch(self.supersedes_payment_id) is None
        ):
            raise ValueError("superseded payment identifier is invalid")

    def with_changes(self, **changes: object) -> "PaymentRequestEnvelope":
        current = {
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "release_id": self.release_id,
            "device_fingerprint": self.device_fingerprint,
            "proof_sha256": self.proof_sha256,
            "content_type": self.content_type,
            "paid_at": self.paid_at,
            "version": self.version,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "purchase_id": self.purchase_id,
            "license_id": self.license_id,
            "supersedes_payment_id": self.supersedes_payment_id,
            "payment_id": self.payment_id,
        }
        current.update(changes)
        return PaymentRequestEnvelope(**current)  # type: ignore[arg-type]

    def _fields(self) -> dict[str, object]:
        return {
            "idempotency_key": self.idempotency_key,
            "kind": self.kind.value,
            "release_id": self.release_id,
            "device_fingerprint": self.device_fingerprint,
            "proof_sha256": self.proof_sha256,
            "content_type": self.content_type,
            "paid_at": self.paid_at,
            "version": self.version,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "purchase_id": self.purchase_id,
            "license_id": self.license_id,
            "supersedes_payment_id": self.supersedes_payment_id,
            "payment_id": self.payment_id,
        }

    def __repr__(self) -> str:
        return (
            f"PaymentRequestEnvelope(kind={self.kind.value!r}, "
            f"version={self.version!r}, state={self.state.value!r}, "
            f"context=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class PaymentRequestLoadResult:
    status: str
    envelope: PaymentRequestEnvelope | None = field(default=None, repr=False)
    screenshot: bytes | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def pending(self) -> bool:
        return (
            self.status == STATUS_SUCCESS
            and self.envelope is not None
            and self.envelope.state is PaymentRequestState.PENDING
            and self.screenshot is not None
        )

    def __repr__(self) -> str:
        return (
            f"PaymentRequestLoadResult(status={self.status!r}, "
            f"envelope={'present' if self.envelope else 'none'!r})"
        )


@dataclass(frozen=True, slots=True)
class PaymentRequestStoreResult:
    status: str
    message: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: object, *, length: int | None = None) -> bytes:
    if type(value) is not str:
        raise ValueError("base64 field is invalid")
    raw = base64.b64decode(value, validate=True)
    if length is not None and len(raw) != length:
        raise ValueError("base64 field length is invalid")
    return raw


class DurablePaymentRequestStore:
    """Persist one pending payment request across restarts and lost responses.

    A single active purchase has at most one pending payment request, so the
    store holds a single slot keyed by ``(service, account)`` in the secure
    store plus one encrypted blob file.
    """

    __slots__ = ("_store", "_dir", "_service", "_account", "_blob_path")

    def __init__(
        self,
        secure_store: SecureStore,
        storage_dir: str | os.PathLike[str],
        *,
        service: str = DEFAULT_ENVELOPE_SERVICE,
        account: str = DEFAULT_ENVELOPE_ACCOUNT,
    ) -> None:
        if not isinstance(secure_store, SecureStore):
            raise TypeError("secure_store must be a SecureStore")
        self._store = secure_store
        self._dir = Path(storage_dir)
        self._service = service
        self._account = account
        self._blob_path = self._dir / f"{account}.enc"

    def __repr__(self) -> str:
        return "DurablePaymentRequestStore(state=<private>)"

    # -- write ---------------------------------------------------------------

    def save(
        self,
        envelope: PaymentRequestEnvelope,
        screenshot: bytes,
    ) -> PaymentRequestStoreResult:
        """Encrypt and durably persist one payment request and its evidence."""

        if not isinstance(envelope, PaymentRequestEnvelope):
            return PaymentRequestStoreResult(STATUS_INVALID, "Envelope is invalid.")
        if (
            type(screenshot) is not bytes
            or not 1 <= len(screenshot) <= MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES
            or hashlib.sha256(screenshot).hexdigest() != envelope.proof_sha256
        ):
            return PaymentRequestStoreResult(
                STATUS_INVALID, "Payment evidence does not match the envelope."
            )
        if _AESGCM is None:
            return PaymentRequestStoreResult(
                STATUS_NOT_AVAILABLE, "Secure payment storage is not available."
            )
        try:
            dek = os.urandom(_DEK_BYTES)
            nonce = os.urandom(_NONCE_BYTES)
            aad = self._associated_data(envelope)
            ciphertext = _AESGCM(dek).encrypt(nonce, screenshot, aad)
        except Exception:
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment evidence could not be secured."
            )
        metadata = {
            "schema": ENVELOPE_SCHEMA,
            **envelope._fields(),
            "cipher": {
                "alg": "AES-256-GCM",
                "dek": _b64(dek),
                "nonce": _b64(nonce),
                "cipher_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "cipher_bytes": len(ciphertext),
                "plain_bytes": len(screenshot),
            },
        }
        try:
            secret = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be encoded."
            )
        if len(secret.encode("utf-8")) > _MAX_METADATA_BYTES:
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request metadata is too large."
            )
        # Write the blob first: a blob without a keychain secret is inert
        # ciphertext, but a keychain secret without a blob would be unusable.
        blob_written = self._write_blob(ciphertext)
        if not blob_written.ok:
            return blob_written
        stored = self._store.set(self._service, self._account, secret)
        if stored.status != STORE_SUCCESS:
            self._discard_blob()
            if stored.status == STORE_NOT_AVAILABLE:
                return PaymentRequestStoreResult(
                    STATUS_NOT_AVAILABLE,
                    "Secure payment storage is not available.",
                )
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be stored securely."
            )
        return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment request stored.")

    def update_state(
        self,
        *,
        state: PaymentRequestState,
        payment_id: str | None = None,
        license_id: str | None = None,
        updated_at: str,
    ) -> PaymentRequestStoreResult:
        """Record a submitted/rejected/failed transition without re-encrypting."""

        loaded = self._load_metadata()
        if loaded[0] != STATUS_SUCCESS or loaded[1] is None:
            return PaymentRequestStoreResult(loaded[0], "Payment request is unavailable.")
        metadata = loaded[1]
        try:
            envelope = _envelope_from_metadata(metadata)
            changes: dict[str, object] = {"state": state, "updated_at": _utc(updated_at)}
            if payment_id is not None:
                changes["payment_id"] = payment_id
            if license_id is not None:
                changes["license_id"] = license_id
            updated = envelope.with_changes(**changes)
        except (TypeError, ValueError):
            return PaymentRequestStoreResult(STATUS_INVALID, "Payment request is invalid.")
        metadata.update(updated._fields())
        try:
            secret = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return PaymentRequestStoreResult(STATUS_FAILED, "Payment request is invalid.")
        stored = self._store.set(self._service, self._account, secret)
        if stored.status != STORE_SUCCESS:
            if stored.status == STORE_NOT_AVAILABLE:
                return PaymentRequestStoreResult(
                    STATUS_NOT_AVAILABLE, "Secure payment storage is not available."
                )
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be updated."
            )
        return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment request updated.")

    def clear(self) -> PaymentRequestStoreResult:
        """Cryptographically shred the request: drop the key, then the blob."""

        deleted = self._store.delete(self._service, self._account)
        self._discard_blob()
        if deleted.status in {STORE_SUCCESS, STORE_NOT_FOUND}:
            return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment request cleared.")
        if deleted.status == STORE_NOT_AVAILABLE:
            return PaymentRequestStoreResult(
                STATUS_NOT_AVAILABLE, "Secure payment storage is not available."
            )
        return PaymentRequestStoreResult(
            STATUS_FAILED, "Payment request could not be cleared."
        )

    # -- read ----------------------------------------------------------------

    def load(self) -> PaymentRequestLoadResult:
        """Load and decrypt the pending request, verifying every binding."""

        status, metadata, message = self._load_metadata()
        if status != STATUS_SUCCESS or metadata is None:
            return PaymentRequestLoadResult(status, message=message)
        try:
            envelope = _envelope_from_metadata(metadata)
            cipher = metadata.get("cipher")
            if type(cipher) is not dict:
                raise ValueError("cipher metadata is missing")
            dek = _unb64(cipher.get("dek"), length=_DEK_BYTES)
            nonce = _unb64(cipher.get("nonce"), length=_NONCE_BYTES)
            cipher_sha = cipher.get("cipher_sha256")
            plain_bytes = cipher.get("plain_bytes")
            if (
                type(cipher_sha) is not str
                or _SHA256_RE.fullmatch(cipher_sha) is None
                or type(plain_bytes) is not int
                or not 1 <= plain_bytes <= MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES
            ):
                raise ValueError("cipher metadata is invalid")
        except (TypeError, ValueError):
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, message="Payment request metadata is unreadable."
            )
        if _AESGCM is None:
            return PaymentRequestLoadResult(
                STATUS_NOT_AVAILABLE,
                message="Secure payment storage is not available.",
            )
        ciphertext = self._read_blob()
        if ciphertext is None:
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, envelope, message="Payment evidence is unavailable."
            )
        if hashlib.sha256(ciphertext).hexdigest() != cipher_sha:
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, envelope, message="Payment evidence was altered."
            )
        try:
            plaintext = _AESGCM(dek).decrypt(
                nonce, ciphertext, self._associated_data(envelope)
            )
        except Exception:
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, envelope, message="Payment evidence could not be decrypted."
            )
        if (
            len(plaintext) != plain_bytes
            or hashlib.sha256(plaintext).hexdigest() != envelope.proof_sha256
        ):
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, envelope, message="Payment evidence does not match."
            )
        return PaymentRequestLoadResult(
            STATUS_SUCCESS, envelope, plaintext, "Payment request loaded."
        )

    def peek(self) -> PaymentRequestLoadResult:
        """Return envelope metadata only, without decrypting the evidence."""

        status, metadata, message = self._load_metadata()
        if status != STATUS_SUCCESS or metadata is None:
            return PaymentRequestLoadResult(status, message=message)
        try:
            envelope = _envelope_from_metadata(metadata)
        except (TypeError, ValueError):
            return PaymentRequestLoadResult(
                STATUS_CORRUPT, message="Payment request metadata is unreadable."
            )
        return PaymentRequestLoadResult(STATUS_SUCCESS, envelope, message="Payment request found.")

    # -- internals -----------------------------------------------------------

    def _associated_data(self, envelope: PaymentRequestEnvelope) -> bytes:
        binding = "\x1f".join(
            (
                ENVELOPE_SCHEMA,
                envelope.idempotency_key,
                envelope.release_id,
                envelope.proof_sha256,
            )
        )
        return binding.encode("utf-8")

    def _load_metadata(self) -> tuple[str, dict[str, object] | None, str]:
        result = self._store.get(self._service, self._account)
        if result.status == STORE_SUCCESS and result.value:
            try:
                if len(result.value.encode("utf-8")) > _MAX_METADATA_BYTES:
                    raise ValueError("metadata too large")
                parsed = json.loads(result.value)
                if type(parsed) is not dict or parsed.get("schema") != ENVELOPE_SCHEMA:
                    raise ValueError("metadata schema mismatch")
            except (TypeError, ValueError):
                return STATUS_CORRUPT, None, "Payment request metadata is unreadable."
            return STATUS_SUCCESS, parsed, "Payment request found."
        if result.status == STORE_NOT_FOUND:
            return STATUS_NOT_FOUND, None, "No pending payment request."
        if result.status == STORE_NOT_AVAILABLE:
            return STATUS_NOT_AVAILABLE, None, "Secure payment storage is not available."
        return STATUS_FAILED, None, "Payment request could not be read."

    def _write_blob(self, ciphertext: bytes) -> PaymentRequestStoreResult:
        temp_path = self._blob_path.with_suffix(".enc.tmp")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self._dir, stat.S_IRWXU)
            except OSError:
                pass
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(temp_path, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(ciphertext)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                try:
                    os.chmod(temp_path, 0o600)
                except OSError:
                    pass
            os.replace(temp_path, self._blob_path)
        except OSError:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment evidence could not be written."
            )
        return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment evidence written.")

    def _read_blob(self) -> bytes | None:
        try:
            with open(self._blob_path, "rb") as handle:
                data = handle.read(MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES + 1024 * 1024)
        except OSError:
            return None
        if not data or len(data) > MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES + 1024 * 1024:
            return None
        return data

    def _discard_blob(self) -> None:
        for candidate in (self._blob_path, self._blob_path.with_suffix(".enc.tmp")):
            try:
                os.unlink(candidate)
            except OSError:
                pass


def _envelope_from_metadata(metadata: dict[str, object]) -> PaymentRequestEnvelope:
    return PaymentRequestEnvelope(
        idempotency_key=_require_str(metadata, "idempotency_key"),
        kind=PaymentRequestKind(_require_str(metadata, "kind")),
        release_id=_require_str(metadata, "release_id"),
        device_fingerprint=_require_str(metadata, "device_fingerprint"),
        proof_sha256=_require_str(metadata, "proof_sha256"),
        content_type=_require_str(metadata, "content_type"),
        paid_at=_require_str(metadata, "paid_at"),
        version=_require_str(metadata, "version"),
        state=PaymentRequestState(_require_str(metadata, "state")),
        created_at=_require_str(metadata, "created_at"),
        updated_at=_require_str(metadata, "updated_at"),
        purchase_id=_optional_str(metadata, "purchase_id"),
        license_id=_optional_str(metadata, "license_id"),
        supersedes_payment_id=_optional_str(metadata, "supersedes_payment_id"),
        payment_id=_optional_str(metadata, "payment_id"),
    )


def _require_str(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if type(value) is not str:
        raise ValueError(f"{key} is invalid")
    return value


def _optional_str(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{key} is invalid")
    return value


__all__ = [
    "DEFAULT_ENVELOPE_ACCOUNT",
    "DEFAULT_ENVELOPE_SERVICE",
    "ENVELOPE_SCHEMA",
    "MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES",
    "STATUS_CORRUPT",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "DurablePaymentRequestStore",
    "PaymentRequestEnvelope",
    "PaymentRequestKind",
    "PaymentRequestLoadResult",
    "PaymentRequestState",
    "PaymentRequestStoreResult",
]

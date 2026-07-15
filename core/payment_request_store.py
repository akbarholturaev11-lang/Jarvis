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
import secrets
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
_GCM_TAG_BYTES: Final = 16
_MAX_CIPHERTEXT_BYTES: Final = (
    MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES + _GCM_TAG_BYTES
)

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
_GENERATED_BLOB_NAME_RE: Final = re.compile(
    r"[0-9a-f]{64}\.[0-9a-f]{32}\.enc"
)


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

    __slots__ = (
        "_store",
        "_dir",
        "_service",
        "_account",
        "_blob_prefix",
        "_legacy_blob_name",
    )

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
        self._blob_prefix = hashlib.sha256(
            (service + "\x00" + account).encode("utf-8", errors="strict")
        ).hexdigest()
        legacy_name = f"{account}.enc"
        self._legacy_blob_name = (
            legacy_name
            if account not in {".", ".."}
            and "/" not in account
            and "\\" not in account
            else None
        )

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
        previous_status, previous_metadata, _ = self._load_metadata()
        previous_blob_name: str | None = None
        if previous_status == STATUS_SUCCESS and previous_metadata is not None:
            try:
                previous_blob_name = self._blob_name_from_metadata(previous_metadata)
            except (TypeError, ValueError):
                return PaymentRequestStoreResult(
                    STATUS_CORRUPT, "Existing payment request is unreadable."
                )
        elif previous_status == STATUS_NOT_AVAILABLE:
            return PaymentRequestStoreResult(
                STATUS_NOT_AVAILABLE, "Secure payment storage is not available."
            )
        elif previous_status not in {STATUS_NOT_FOUND, STATUS_SUCCESS}:
            return PaymentRequestStoreResult(
                previous_status, "Existing payment request is unavailable."
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
        blob_name = self._new_blob_name()
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
                "blob_name": blob_name,
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
        # Every replacement uses a fresh immutable blob.  The secure-store
        # metadata is the commit pointer, so a crash or failed metadata update
        # cannot overwrite the blob referenced by the previous envelope.
        blob_written = self._write_blob(blob_name, ciphertext)
        if not blob_written.ok:
            return blob_written
        try:
            stored = self._store.set(self._service, self._account, secret)
        except Exception:
            stored = None
        if stored is None or stored.status != STORE_SUCCESS:
            reference = self._reference_after_ambiguous_set(secret, blob_name)
            if reference == "new":
                if previous_blob_name is not None:
                    self._discard_blob(previous_blob_name)
                return PaymentRequestStoreResult(
                    STATUS_SUCCESS, "Payment request stored."
                )
            if reference == "other":
                self._discard_blob(blob_name)
            if stored is not None and stored.status == STORE_NOT_AVAILABLE:
                return PaymentRequestStoreResult(
                    STATUS_NOT_AVAILABLE,
                    "Secure payment storage is not available.",
                )
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be stored securely."
            )
        if previous_blob_name is not None:
            self._discard_blob(previous_blob_name)
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
        try:
            stored = self._store.set(self._service, self._account, secret)
        except Exception:
            stored = None
        if stored is None or stored.status != STORE_SUCCESS:
            if self._secret_is_current(secret):
                return PaymentRequestStoreResult(
                    STATUS_SUCCESS, "Payment request updated."
                )
            if stored is not None and stored.status == STORE_NOT_AVAILABLE:
                return PaymentRequestStoreResult(
                    STATUS_NOT_AVAILABLE, "Secure payment storage is not available."
                )
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be updated."
            )
        return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment request updated.")

    def clear(self) -> PaymentRequestStoreResult:
        """Cryptographically shred the request: drop the key, then the blob."""

        metadata_status, metadata, _ = self._load_metadata()
        blob_name: str | None = None
        if metadata_status == STATUS_SUCCESS and metadata is not None:
            try:
                blob_name = self._blob_name_from_metadata(metadata)
            except (TypeError, ValueError):
                blob_name = None
        try:
            deleted = self._store.delete(self._service, self._account)
        except Exception:
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment request could not be cleared."
            )
        if deleted.status in {STORE_SUCCESS, STORE_NOT_FOUND}:
            if blob_name is not None:
                self._discard_blob(blob_name)
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
            cipher_bytes = cipher.get("cipher_bytes")
            plain_bytes = cipher.get("plain_bytes")
            blob_name = self._blob_name_from_metadata(metadata)
            if (
                type(cipher_sha) is not str
                or _SHA256_RE.fullmatch(cipher_sha) is None
                or type(cipher_bytes) is not int
                or not _GCM_TAG_BYTES < cipher_bytes <= _MAX_CIPHERTEXT_BYTES
                or type(plain_bytes) is not int
                or not 1 <= plain_bytes <= MAX_PAYMENT_REQUEST_SCREENSHOT_BYTES
                or cipher_bytes != plain_bytes + _GCM_TAG_BYTES
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
        ciphertext = self._read_blob(blob_name, expected_size=cipher_bytes)
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
        try:
            result = self._store.get(self._service, self._account)
        except Exception:
            return STATUS_FAILED, None, "Payment request could not be read."
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

    def _new_blob_name(self) -> str:
        return f"{self._blob_prefix}.{secrets.token_hex(16)}.enc"

    def _valid_blob_name(self, value: object) -> bool:
        if type(value) is not str or _GENERATED_BLOB_NAME_RE.fullmatch(value) is None:
            return False
        return value.startswith(self._blob_prefix + ".")

    def _blob_name_from_metadata(self, metadata: dict[str, object]) -> str:
        cipher = metadata.get("cipher")
        if type(cipher) is not dict:
            raise ValueError("cipher metadata is missing")
        value = cipher.get("blob_name")
        if value is None:
            if self._legacy_blob_name is None:
                raise ValueError("legacy blob name is unavailable")
            return self._legacy_blob_name
        if not self._valid_blob_name(value):
            raise ValueError("blob name is invalid")
        return value

    def _reference_after_ambiguous_set(self, secret: str, blob_name: str) -> str:
        """Return ``new``, ``other`` or ``unknown`` after a failed store call.

        A native credential API can theoretically commit and then surface an
        operational failure.  We only discard the fresh blob after a read proves
        that the secure-store pointer references something else.  With an
        unavailable read both generations are retained, so either committed
        metadata value remains recoverable after restart.
        """

        try:
            current = self._store.get(self._service, self._account)
        except Exception:
            return "unknown"
        if current.status == STORE_NOT_FOUND:
            return "other"
        if current.status != STORE_SUCCESS or not current.value:
            return "unknown"
        if secrets.compare_digest(current.value, secret):
            return "new"
        try:
            if len(current.value.encode("utf-8")) > _MAX_METADATA_BYTES:
                return "unknown"
            parsed = json.loads(current.value)
            if type(parsed) is not dict or parsed.get("schema") != ENVELOPE_SCHEMA:
                return "unknown"
            current_blob = self._blob_name_from_metadata(parsed)
        except (TypeError, ValueError):
            return "unknown"
        return "new" if current_blob == blob_name else "other"

    def _secret_is_current(self, secret: str) -> bool:
        try:
            current = self._store.get(self._service, self._account)
        except Exception:
            return False
        return bool(
            current.status == STORE_SUCCESS
            and current.value
            and secrets.compare_digest(current.value, secret)
        )

    def _open_private_directory(self) -> int | None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        before = os.lstat(self._dir)
        if not stat.S_ISDIR(before.st_mode):
            raise OSError("payment storage path is not a directory")
        if os.name == "nt":
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._dir, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or not self._same_file(before, opened)
            ):
                raise OSError("payment storage directory is unsafe")
            os.fchmod(descriptor, 0o700)
            opened = os.fstat(descriptor)
            after = os.lstat(self._dir)
            if (
                stat.S_IMODE(opened.st_mode) != 0o700
                or not self._same_file(opened, after)
            ):
                raise OSError("payment storage directory is unsafe")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    @staticmethod
    def _private_file(
        opened: os.stat_result,
        *,
        expected_size: int | None = None,
    ) -> bool:
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (expected_size is not None and opened.st_size != expected_size)
        ):
            return False
        if os.name != "nt":
            if opened.st_uid != os.getuid():
                return False
            if stat.S_IMODE(opened.st_mode) != 0o600:
                return False
        return True

    def _named_stat(self, blob_name: str, directory_fd: int | None) -> os.stat_result:
        if directory_fd is not None and os.stat in os.supports_dir_fd:
            return os.stat(blob_name, dir_fd=directory_fd, follow_symlinks=False)
        return os.lstat(self._dir / blob_name)

    def _open_blob(
        self,
        blob_name: str,
        flags: int,
        directory_fd: int | None,
        mode: int | None = None,
    ) -> int:
        if directory_fd is not None and os.open in os.supports_dir_fd:
            if mode is None:
                return os.open(blob_name, flags, dir_fd=directory_fd)
            return os.open(blob_name, flags, mode, dir_fd=directory_fd)
        path = self._dir / blob_name
        if mode is None:
            return os.open(path, flags)
        return os.open(path, flags, mode)

    def _unlink_blob(self, blob_name: str, directory_fd: int | None) -> None:
        if directory_fd is not None and os.unlink in os.supports_dir_fd:
            os.unlink(blob_name, dir_fd=directory_fd)
        else:
            os.unlink(self._dir / blob_name)

    def _write_blob(
        self,
        blob_name: str,
        ciphertext: bytes,
    ) -> PaymentRequestStoreResult:
        if not self._valid_blob_name(blob_name):
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment evidence could not be written."
            )
        directory_fd: int | None = None
        descriptor: int | None = None
        created: os.stat_result | None = None
        keep_file = False
        try:
            directory_fd = self._open_private_directory()
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = self._open_blob(blob_name, flags, directory_fd, 0o600)
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            created = os.fstat(descriptor)
            if not self._private_file(created, expected_size=0):
                raise OSError("payment evidence file is unsafe")
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                if handle.write(ciphertext) != len(ciphertext):
                    raise OSError("payment evidence write was incomplete")
                handle.flush()
                os.fsync(handle.fileno())
                written = os.fstat(handle.fileno())
                if (
                    not self._private_file(written, expected_size=len(ciphertext))
                    or not self._same_file(created, written)
                ):
                    raise OSError("payment evidence file changed while writing")
            named = self._named_stat(blob_name, directory_fd)
            if (
                not self._private_file(named, expected_size=len(ciphertext))
                or not self._same_file(created, named)
            ):
                raise OSError("payment evidence file changed after writing")
            if directory_fd is not None:
                os.fsync(directory_fd)
            keep_file = True
        except (OSError, TypeError, ValueError):
            return PaymentRequestStoreResult(
                STATUS_FAILED, "Payment evidence could not be written."
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not keep_file and created is not None:
                try:
                    named = self._named_stat(blob_name, directory_fd)
                    if self._same_file(created, named):
                        self._unlink_blob(blob_name, directory_fd)
                except OSError:
                    pass
            if directory_fd is not None:
                os.close(directory_fd)
        return PaymentRequestStoreResult(STATUS_SUCCESS, "Payment evidence written.")

    def _read_blob(self, blob_name: str, *, expected_size: int) -> bytes | None:
        if not (
            self._valid_blob_name(blob_name) or blob_name == self._legacy_blob_name
        ):
            return None
        directory_fd: int | None = None
        descriptor: int | None = None
        try:
            directory_fd = self._open_private_directory()
            named_before = self._named_stat(blob_name, directory_fd)
            if not self._private_file(named_before, expected_size=expected_size):
                return None
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = self._open_blob(blob_name, flags, directory_fd)
            opened = os.fstat(descriptor)
            if (
                not self._private_file(opened, expected_size=expected_size)
                or not self._same_file(named_before, opened)
            ):
                return None
            chunks: list[bytes] = []
            remaining = expected_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                return None
            opened_after = os.fstat(descriptor)
            named_after = self._named_stat(blob_name, directory_fd)
            if (
                not self._private_file(opened_after, expected_size=expected_size)
                or not self._private_file(named_after, expected_size=expected_size)
                or not self._same_file(opened, opened_after)
                or not self._same_file(opened, named_after)
            ):
                return None
            return b"".join(chunks)
        except (OSError, TypeError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd is not None:
                os.close(directory_fd)

    def _discard_blob(self, blob_name: str) -> None:
        if not (
            self._valid_blob_name(blob_name) or blob_name == self._legacy_blob_name
        ):
            return
        directory_fd: int | None = None
        try:
            directory_fd = self._open_private_directory()
            opened = self._named_stat(blob_name, directory_fd)
            if not self._private_file(opened):
                return
            self._unlink_blob(blob_name, directory_fd)
            if directory_fd is not None:
                os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


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

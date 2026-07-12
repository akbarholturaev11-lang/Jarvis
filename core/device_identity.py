"""Per-install Ed25519 device identity backed only by :mod:`secure_store`.

The identity is generated, loaded, and signed in memory.  Its raw private key is
persisted only through the platform-neutral ``SecureStore`` contract.  No
hardware serial number or other machine identifier is read.

Messages are sanitized internal diagnostics.  A future UI must localize status
values through the shared English/Russian message layer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Final

from core.interprocess_lock import (
    InterProcessFileLock,
    InterProcessLockNotAvailable,
    InterProcessLockTimeout,
)
from core.product_version import BUNDLE_ID
from core.secure_store import (
    STATUS_FAILED as STORE_STATUS_FAILED,
    STATUS_NOT_AVAILABLE as STORE_STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND as STORE_STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
)

try:
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives import serialization as _serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _Ed25519PrivateKey,
        Ed25519PublicKey as _Ed25519PublicKey,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover - availability is tested via the flag
    _InvalidSignature = ValueError  # type: ignore[assignment,misc]
    _serialization = None  # type: ignore[assignment]
    _Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    _Ed25519PublicKey = None  # type: ignore[assignment,misc]
    _ED25519_AVAILABLE = False


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_INVALID: Final = "invalid"
STATUS_FAILED: Final = "failed"

DEVICE_IDENTITY_SERVICE: Final = f"{BUNDLE_ID}.device-identity"
DEVICE_IDENTITY_ACCOUNT: Final = "ed25519-private-key-v1"

CHALLENGE_NONCE_MIN_BYTES: Final = 16
CHALLENGE_NONCE_MAX_BYTES: Final = 256

_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_NOT_FOUND,
        STATUS_NOT_AVAILABLE,
        STATUS_INVALID,
        STATUS_FAILED,
    }
)
_RAW_PRIVATE_KEY_BYTES: Final = 32
_RAW_PUBLIC_KEY_BYTES: Final = 32
_SIGNATURE_BYTES: Final = 64
_BASE64URL_RE: Final = re.compile(r"[A-Za-z0-9_-]+")
_FINGERPRINT_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_CHALLENGE_DOMAIN: Final = b"jarvis.device.challenge.v1\x00"
_CREATE_LOCK: Final = threading.Lock()

_MESSAGE_LOADED: Final = "Device identity loaded."
_MESSAGE_CREATED: Final = "Device identity created and verified."
_MESSAGE_NOT_FOUND: Final = "Device identity was not found."
_MESSAGE_NOT_AVAILABLE: Final = "Device identity storage is not available."
_MESSAGE_INVALID: Final = "Stored device identity is invalid."
_MESSAGE_FAILED: Final = "Device identity operation failed."
_MESSAGE_CHALLENGE_VERIFIED: Final = "Device challenge verified."
_MESSAGE_CHALLENGE_INVALID: Final = "Device challenge is invalid."
_MESSAGE_CHALLENGE_NOT_AVAILABLE: Final = (
    "Device challenge verification is not available."
)


class _IdentityInvalid(ValueError):
    """Internal marker for malformed key or challenge input."""


class DeviceIdentity:
    """Verified public identity with a bounded proof-of-possession method.

    The private-key object is name-mangled and has no public raw-key accessor.
    """

    __slots__ = (
        "__private_key",
        "__public_key_bytes",
        "__public_key_base64",
        "__fingerprint",
    )

    def __init__(self, private_key: Any) -> None:
        if (
            not _ED25519_AVAILABLE
            or _Ed25519PrivateKey is None
            or _serialization is None
            or not isinstance(private_key, _Ed25519PrivateKey)
        ):
            raise _IdentityInvalid("invalid Ed25519 private key")
        public_key_bytes = private_key.public_key().public_bytes(
            encoding=_serialization.Encoding.Raw,
            format=_serialization.PublicFormat.Raw,
        )
        if len(public_key_bytes) != _RAW_PUBLIC_KEY_BYTES:
            raise _IdentityInvalid("invalid Ed25519 public key")

        self.__private_key = private_key
        self.__public_key_bytes = public_key_bytes
        self.__public_key_base64 = _encode_base64url(public_key_bytes)
        self.__fingerprint = _fingerprint(public_key_bytes)

    @property
    def public_key_bytes(self) -> bytes:
        return self.__public_key_bytes

    @property
    def public_key_base64(self) -> str:
        return self.__public_key_base64

    @property
    def fingerprint(self) -> str:
        return self.__fingerprint

    def sign_challenge(self, challenge_nonce: str) -> str:
        """Sign one canonical base64url nonce with a domain-separated message."""

        nonce = _decode_base64url(
            challenge_nonce,
            minimum_bytes=CHALLENGE_NONCE_MIN_BYTES,
            maximum_bytes=CHALLENGE_NONCE_MAX_BYTES,
        )
        signature = self.__private_key.sign(_challenge_message(nonce))
        if len(signature) != _SIGNATURE_BYTES:
            raise RuntimeError("Ed25519 returned an invalid signature")
        return _encode_base64url(signature)

    def __repr__(self) -> str:
        return "DeviceIdentity(fingerprint=<available>, public_key=<available>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class DeviceIdentityResult:
    """Sanitized result for loading or creating a device identity."""

    status: str
    message: str = field(repr=False)
    identity: DeviceIdentity | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported device identity status")
        if (self.status == STATUS_SUCCESS) != (self.identity is not None):
            raise ValueError("only success may carry a device identity")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.identity is not None

    def __repr__(self) -> str:
        identity = "available" if self.identity is not None else "none"
        return f"DeviceIdentityResult(status={self.status!r}, identity={identity!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class DeviceChallengeVerificationResult:
    """Sanitized server-side challenge verification result."""

    status: str
    message: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.status not in {STATUS_SUCCESS, STATUS_INVALID, STATUS_NOT_AVAILABLE}:
            raise ValueError("unsupported challenge verification status")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        return f"DeviceChallengeVerificationResult(status={self.status!r})"

    __str__ = __repr__


def _identity_result(
    status: str,
    message: str,
    identity: DeviceIdentity | None = None,
) -> DeviceIdentityResult:
    return DeviceIdentityResult(status, message, identity)


def _challenge_result(status: str, message: str) -> DeviceChallengeVerificationResult:
    return DeviceChallengeVerificationResult(status, message)


def _encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url(
    value: object,
    *,
    minimum_bytes: int,
    maximum_bytes: int,
) -> bytes:
    # Bound text before the regex and decode allocate work proportional to
    # attacker-controlled input.  Unpadded base64url uses at most ceil(4n/3).
    maximum_characters = (maximum_bytes * 8 + 5) // 6
    if (
        type(value) is not str
        or len(value) > maximum_characters
        or _BASE64URL_RE.fullmatch(value) is None
    ):
        raise _IdentityInvalid("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise _IdentityInvalid("invalid base64url value") from exc
    if _encode_base64url(decoded) != value:
        raise _IdentityInvalid("non-canonical base64url value")
    if not minimum_bytes <= len(decoded) <= maximum_bytes:
        raise _IdentityInvalid("base64url value has invalid size")
    return decoded


def _fingerprint(public_key_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_key_bytes).hexdigest()


def _validate_fingerprint(value: object) -> str:
    if type(value) is not str or _FINGERPRINT_RE.fullmatch(value) is None:
        raise _IdentityInvalid("invalid device fingerprint")
    return value


def _challenge_message(nonce: bytes) -> bytes:
    return _CHALLENGE_DOMAIN + nonce


def _identity_from_stored_value(value: object) -> DeviceIdentity:
    if (
        not _ED25519_AVAILABLE
        or _Ed25519PrivateKey is None
        or _serialization is None
    ):
        raise RuntimeError("Ed25519 is unavailable")
    raw_private_key = _decode_base64url(
        value,
        minimum_bytes=_RAW_PRIVATE_KEY_BYTES,
        maximum_bytes=_RAW_PRIVATE_KEY_BYTES,
    )
    try:
        private_key = _Ed25519PrivateKey.from_private_bytes(raw_private_key)
    except (TypeError, ValueError) as exc:
        raise _IdentityInvalid("invalid stored private key") from exc
    return DeviceIdentity(private_key)


def _new_identity_and_stored_value() -> tuple[DeviceIdentity, str]:
    if (
        not _ED25519_AVAILABLE
        or _Ed25519PrivateKey is None
        or _serialization is None
    ):
        raise RuntimeError("Ed25519 is unavailable")
    private_key = _Ed25519PrivateKey.generate()
    raw_private_key = private_key.private_bytes(
        encoding=_serialization.Encoding.Raw,
        format=_serialization.PrivateFormat.Raw,
        encryption_algorithm=_serialization.NoEncryption(),
    )
    if len(raw_private_key) != _RAW_PRIVATE_KEY_BYTES:
        raise RuntimeError("Ed25519 returned an invalid private key")
    return DeviceIdentity(private_key), _encode_base64url(raw_private_key)


class DeviceIdentityManager:
    """Load or create one per-install identity through a supplied SecureStore."""

    __slots__ = ("_store", "_create_lock")

    def __init__(
        self,
        store: SecureStore,
        *,
        creation_lock_path: str | None = None,
    ) -> None:
        self._store = store
        self._create_lock = (
            None
            if creation_lock_path is None
            else InterProcessFileLock(creation_lock_path)
        )

    def __repr__(self) -> str:
        return "DeviceIdentityManager(store=<secure>)"

    def load(self) -> DeviceIdentityResult:
        if not _ED25519_AVAILABLE:
            return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        try:
            stored = self._store.get(
                DEVICE_IDENTITY_SERVICE,
                DEVICE_IDENTITY_ACCOUNT,
            )
        except Exception:
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if not isinstance(stored, SecureStoreResult):
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if stored.status == STORE_STATUS_NOT_FOUND:
            return _identity_result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
        if stored.status == STORE_STATUS_NOT_AVAILABLE:
            return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        if stored.status == STORE_STATUS_FAILED:
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if stored.status != STORE_STATUS_SUCCESS:
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        try:
            identity = _identity_from_stored_value(stored.value)
        except (_IdentityInvalid, RuntimeError, TypeError, ValueError):
            return _identity_result(STATUS_INVALID, _MESSAGE_INVALID)
        return _identity_result(STATUS_SUCCESS, _MESSAGE_LOADED, identity)

    def get_or_create(self) -> DeviceIdentityResult:
        if not _ED25519_AVAILABLE:
            return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        if self._create_lock is None:
            return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        with _CREATE_LOCK:
            try:
                with self._create_lock.acquire():
                    return self._get_or_create_locked()
            except InterProcessLockNotAvailable:
                return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
            except InterProcessLockTimeout:
                return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
            except Exception:
                return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)

    def _get_or_create_locked(self) -> DeviceIdentityResult:
        current = self.load()
        if current.status != STATUS_NOT_FOUND:
            return current

        try:
            candidate, stored_value = _new_identity_and_stored_value()
            stored = self._store.set(
                DEVICE_IDENTITY_SERVICE,
                DEVICE_IDENTITY_ACCOUNT,
                stored_value,
            )
        except Exception:
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if not isinstance(stored, SecureStoreResult):
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if stored.status == STORE_STATUS_NOT_AVAILABLE:
            return _identity_result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        if stored.status != STORE_STATUS_SUCCESS:
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)

        verified = self.load()
        if verified.status != STATUS_SUCCESS or verified.identity is None:
            if verified.status == STATUS_INVALID:
                return verified
            if verified.status == STATUS_NOT_AVAILABLE:
                return _identity_result(
                    STATUS_NOT_AVAILABLE,
                    _MESSAGE_NOT_AVAILABLE,
                )
            return _identity_result(STATUS_FAILED, _MESSAGE_FAILED)
        if not hmac.compare_digest(
            verified.identity.fingerprint,
            candidate.fingerprint,
        ) or not hmac.compare_digest(
            verified.identity.public_key_bytes,
            candidate.public_key_bytes,
        ):
            return _identity_result(STATUS_INVALID, _MESSAGE_INVALID)
        return _identity_result(STATUS_SUCCESS, _MESSAGE_CREATED, verified.identity)


def verify_device_challenge(
    *,
    public_key_base64: str,
    device_key_fingerprint: str,
    challenge_nonce: str,
    signature_base64: str,
) -> DeviceChallengeVerificationResult:
    """Verify a domain-separated proof using only supplied public information.

    This proves key possession only.  The server API must also issue an
    unpredictable, short-lived nonce, bind it to the intended license/request,
    and atomically consume it once so a valid signature cannot be replayed.
    """

    if (
        not _ED25519_AVAILABLE
        or _Ed25519PublicKey is None
        or _serialization is None
    ):
        return _challenge_result(
            STATUS_NOT_AVAILABLE,
            _MESSAGE_CHALLENGE_NOT_AVAILABLE,
        )
    try:
        public_key_bytes = _decode_base64url(
            public_key_base64,
            minimum_bytes=_RAW_PUBLIC_KEY_BYTES,
            maximum_bytes=_RAW_PUBLIC_KEY_BYTES,
        )
        fingerprint = _validate_fingerprint(device_key_fingerprint)
        if not hmac.compare_digest(_fingerprint(public_key_bytes), fingerprint):
            raise _IdentityInvalid("public key fingerprint mismatch")
        nonce = _decode_base64url(
            challenge_nonce,
            minimum_bytes=CHALLENGE_NONCE_MIN_BYTES,
            maximum_bytes=CHALLENGE_NONCE_MAX_BYTES,
        )
        signature = _decode_base64url(
            signature_base64,
            minimum_bytes=_SIGNATURE_BYTES,
            maximum_bytes=_SIGNATURE_BYTES,
        )
        public_key = _Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, _challenge_message(nonce))
    except (
        _IdentityInvalid,
        _InvalidSignature,
        TypeError,
        ValueError,
        binascii.Error,
    ):
        return _challenge_result(STATUS_INVALID, _MESSAGE_CHALLENGE_INVALID)
    return _challenge_result(STATUS_SUCCESS, _MESSAGE_CHALLENGE_VERIFIED)


__all__ = [
    "CHALLENGE_NONCE_MAX_BYTES",
    "CHALLENGE_NONCE_MIN_BYTES",
    "DEVICE_IDENTITY_ACCOUNT",
    "DEVICE_IDENTITY_SERVICE",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "DeviceChallengeVerificationResult",
    "DeviceIdentity",
    "DeviceIdentityManager",
    "DeviceIdentityResult",
    "verify_device_challenge",
]

"""Private offline cache for signed exact-version entitlement certificates."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.entitlement_certificate import (
    STATUS_INVALID as VERIFY_STATUS_INVALID,
    STATUS_NOT_AVAILABLE as VERIFY_STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND as VERIFY_STATUS_NOT_FOUND,
    STATUS_SUCCESS as VERIFY_STATUS_SUCCESS,
    EntitlementCertificate,
    verify_entitlement_certificate,
)
from core.interprocess_lock import (
    InterProcessFileLock,
    InterProcessLockNotAvailable,
    InterProcessLockTimeout,
)
from core.product_version import SemanticVersion


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID: Final = "invalid"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

MAX_CACHED_CERTIFICATE_BYTES: Final = 32 * 1024

_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_NOT_FOUND,
        STATUS_INVALID,
        STATUS_NOT_AVAILABLE,
        STATUS_FAILED,
    }
)
_MESSAGE_SUCCESS: Final = "Offline entitlement certificate verified."
_MESSAGE_NOT_FOUND: Final = "Offline entitlement certificate was not found."
_MESSAGE_INVALID: Final = "Offline entitlement certificate is invalid."
_MESSAGE_NOT_AVAILABLE: Final = "Offline entitlement cache is not available."
_MESSAGE_FAILED: Final = "Offline entitlement cache operation failed."


@dataclass(frozen=True, slots=True)
class EntitlementCacheResult:
    status: str
    message: str = field(repr=False)
    certificate: EntitlementCertificate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported entitlement cache status")
        if (self.status == STATUS_SUCCESS) != (self.certificate is not None):
            raise ValueError("only success may carry entitlement claims")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.certificate is not None

    def __repr__(self) -> str:
        claims = "verified" if self.certificate is not None else "none"
        return f"EntitlementCacheResult(status={self.status!r}, claims={claims!r})"


def _result(
    status: str,
    message: str,
    certificate: EntitlementCertificate | None = None,
) -> EntitlementCacheResult:
    return EntitlementCacheResult(status, message, certificate)


def _certificate_bytes(value: object) -> bytes:
    if type(value) is str:
        if len(value) > MAX_CACHED_CERTIFICATE_BYTES:
            raise ValueError("certificate is too large")
        raw = value.encode("utf-8", errors="strict")
    elif type(value) is bytes:
        raw = value
    else:
        raise ValueError("certificate must be text or bytes")
    if not raw or len(raw) > MAX_CACHED_CERTIFICATE_BYTES:
        raise ValueError("certificate size is invalid")
    return raw


def _version_text(value: object) -> str:
    if isinstance(value, SemanticVersion):
        return str(value)
    if type(value) is str:
        return str(SemanticVersion.parse(value))
    raise ValueError("version is invalid")


class SignedEntitlementCache:
    """Verify before storing and verify again on every offline cache read."""

    __slots__ = ("_directory", "_lock", "_trusted_public_keys")

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        trusted_public_keys: Mapping[str, object],
        lock_system: str | None = None,
    ) -> None:
        cache_directory = Path(directory).expanduser()
        if not cache_directory.is_absolute():
            raise ValueError("entitlement cache directory must be absolute")
        if cache_directory.is_symlink():
            raise ValueError("entitlement cache directory cannot be a symlink")
        cache_directory = cache_directory.resolve(strict=False)
        if not isinstance(trusted_public_keys, Mapping):
            raise TypeError("trusted_public_keys must be a mapping")
        self._directory = cache_directory
        self._trusted_public_keys = dict(trusted_public_keys)
        self._lock = InterProcessFileLock(
            cache_directory / ".entitlements.lock",
            system=lock_system,
        )

    def __repr__(self) -> str:
        return (
            "SignedEntitlementCache(directory=<private>, "
            "trusted_public_keys=<pinned>)"
        )

    def certificate_path(
        self,
        *,
        license_id: str,
        device_fingerprint: str,
        version: str | SemanticVersion,
    ) -> Path:
        version_text = _version_text(version)
        if type(license_id) is not str or type(device_fingerprint) is not str:
            raise ValueError("entitlement identity is invalid")
        identity = (
            license_id.encode("utf-8", errors="strict")
            + b"\0"
            + device_fingerprint.encode("ascii", errors="strict")
            + b"\0"
            + version_text.encode("ascii")
        )
        return self._directory / (hashlib.sha256(identity).hexdigest() + ".entitlement")

    def _ensure_directory(self) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._directory.is_symlink():
            raise OSError("cache directory is a symlink")
        opened = self._directory.stat()
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("cache path is not a directory")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise OSError("cache directory owner mismatch")
        if os.name != "nt":
            self._directory.chmod(0o700)

    def _verify(
        self,
        raw: bytes,
        *,
        license_id: str,
        device_fingerprint: str,
        version: str | SemanticVersion,
    ) -> EntitlementCacheResult:
        verification = verify_entitlement_certificate(
            raw,
            trusted_public_keys=self._trusted_public_keys,
            expected_license_id=license_id,
            expected_device_fingerprint=device_fingerprint,
            expected_version=version,
        )
        if verification.status == VERIFY_STATUS_SUCCESS:
            return _result(STATUS_SUCCESS, _MESSAGE_SUCCESS, verification.certificate)
        if verification.status == VERIFY_STATUS_NOT_AVAILABLE:
            return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        if verification.status == VERIFY_STATUS_NOT_FOUND:
            return _result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
        if verification.status == VERIFY_STATUS_INVALID:
            return _result(STATUS_INVALID, _MESSAGE_INVALID)
        return _result(STATUS_FAILED, _MESSAGE_FAILED)

    def store_verified(
        self,
        certificate: str | bytes,
        *,
        license_id: str,
        device_fingerprint: str,
        version: str | SemanticVersion,
    ) -> EntitlementCacheResult:
        try:
            raw = _certificate_bytes(certificate)
            verified = self._verify(
                raw,
                license_id=license_id,
                device_fingerprint=device_fingerprint,
                version=version,
            )
            if not verified.ok:
                return verified
            target = self.certificate_path(
                license_id=license_id,
                device_fingerprint=device_fingerprint,
                version=version,
            )
            with self._lock.acquire():
                self._ensure_directory()
                if target.is_symlink():
                    return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
                temporary: Path | None = None
                descriptor: int | None = None
                try:
                    for _attempt in range(3):
                        temporary = self._directory / (
                            ".entitlement-" + secrets.token_hex(16) + ".tmp"
                        )
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        if hasattr(os, "O_NOFOLLOW"):
                            flags |= os.O_NOFOLLOW
                        try:
                            descriptor = os.open(temporary, flags, 0o600)
                            break
                        except FileExistsError:
                            temporary = None
                    if descriptor is None or temporary is None:
                        raise OSError("temporary cache file unavailable")
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        descriptor = None
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, target)
                    temporary = None
                    if os.name != "nt":
                        target.chmod(0o600)
                        directory_descriptor = os.open(self._directory, os.O_RDONLY)
                        try:
                            os.fsync(directory_descriptor)
                        finally:
                            os.close(directory_descriptor)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return verified
        except (InterProcessLockNotAvailable,):
            return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        except (InterProcessLockTimeout, OSError, TypeError, ValueError, UnicodeError):
            return _result(STATUS_FAILED, _MESSAGE_FAILED)

    def load_verified(
        self,
        *,
        license_id: str,
        device_fingerprint: str,
        version: str | SemanticVersion,
    ) -> EntitlementCacheResult:
        try:
            target = self.certificate_path(
                license_id=license_id,
                device_fingerprint=device_fingerprint,
                version=version,
            )
            with self._lock.acquire():
                self._ensure_directory()
                if not target.exists():
                    return _result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
                if target.is_symlink():
                    return _result(STATUS_INVALID, _MESSAGE_INVALID)
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags)
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or opened.st_size <= 0
                        or opened.st_size > MAX_CACHED_CERTIFICATE_BYTES
                        or (
                            hasattr(os, "getuid")
                            and opened.st_uid != os.getuid()
                        )
                    ):
                        return _result(STATUS_INVALID, _MESSAGE_INVALID)
                    chunks: list[bytes] = []
                    remaining = opened.st_size
                    while remaining:
                        chunk = os.read(descriptor, min(remaining, 8192))
                        if not chunk:
                            return _result(STATUS_INVALID, _MESSAGE_INVALID)
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    if os.read(descriptor, 1):
                        return _result(STATUS_INVALID, _MESSAGE_INVALID)
                    raw = b"".join(chunks)
                finally:
                    os.close(descriptor)
            return self._verify(
                raw,
                license_id=license_id,
                device_fingerprint=device_fingerprint,
                version=version,
            )
        except FileNotFoundError:
            return _result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
        except InterProcessLockNotAvailable:
            return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        except (InterProcessLockTimeout, OSError, TypeError, ValueError, UnicodeError):
            return _result(STATUS_FAILED, _MESSAGE_FAILED)


__all__ = [
    "MAX_CACHED_CERTIFICATE_BYTES",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "EntitlementCacheResult",
    "SignedEntitlementCache",
]

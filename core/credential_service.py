"""Secure Gemini credential lifecycle and one-time legacy migration.

The platform secure store is authoritative.  A historical JSON credential is
never used directly by the runtime: it is migrated under a bounded process lock,
read back from secure storage, and only then removed from the JSON atomically.
All public errors are fixed text and never include credential values, paths, or
backend exception details.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.interprocess_lock import InterProcessFileLock, InterProcessLockError
from core.secure_store import (
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
    create_secure_store,
)


GEMINI_SERVICE: Final = "com.jarvis.assistant.credentials"
GEMINI_ACCOUNT: Final = "gemini-api-key-v1"
_MAX_LEGACY_BYTES: Final = 128 * 1024
_MAX_SECRET_BYTES: Final = 64 * 1024
_VALID_STATUSES: Final = frozenset(
    {STATUS_SUCCESS, STATUS_NOT_FOUND, STATUS_NOT_AVAILABLE, STATUS_FAILED}
)
MIGRATION_NOT_NEEDED: Final = "not_needed"
MIGRATION_COMPLETED: Final = "completed"
MIGRATION_FAILED: Final = "failed"
_VALID_MIGRATION_STATUSES: Final = frozenset(
    {MIGRATION_NOT_NEEDED, MIGRATION_COMPLETED, MIGRATION_FAILED}
)


@dataclass(frozen=True, slots=True)
class CredentialResult:
    status: str
    value: str | None = field(default=None, repr=False)
    source: str | None = None
    message: str = ""
    migration_status: str = MIGRATION_NOT_NEEDED

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("invalid credential status")
        if self.migration_status not in _VALID_MIGRATION_STATUSES:
            raise ValueError("invalid credential migration status")
        if self.value and self.value in self.message:
            object.__setattr__(
                self, "message", self.message.replace(self.value, "<redacted>")
            )

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        value = "<redacted>" if self.value is not None else "None"
        return (
            f"CredentialResult(status={self.status!r}, value={value}, "
            f"source={self.source!r}, message={self.message!r}, "
            f"migration_status={self.migration_status!r})"
        )


@dataclass(frozen=True, slots=True)
class _LegacyCredential:
    value: str = field(repr=False)
    document: dict[str, object] = field(repr=False)
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _LegacyReadResult:
    status: str
    credential: _LegacyCredential | None = field(default=None, repr=False)


def load_gemini_api_key(
    *,
    store: SecureStore | None = None,
    legacy_path: Path | None = None,
) -> CredentialResult:
    """Load only verified secure storage, migrating legacy JSON when present."""

    backend = create_secure_store() if store is None else store
    secured = _safe_get(backend)
    legacy = _read_legacy(legacy_path)
    if legacy.status == "invalid":
        if secured.status == STATUS_SUCCESS and secured.value:
            return CredentialResult(
                STATUS_SUCCESS,
                secured.value,
                "secure_store",
                "Credential loaded; legacy cleanup requires attention.",
                MIGRATION_FAILED,
            )
        return _migration_failure()
    if legacy.status == "absent":
        return _credential_from_secure_result(secured)

    assert legacy_path is not None
    try:
        lock = _migration_lock(legacy_path)
        with lock.acquire():
            # Another process may have completed migration while this process
            # waited. Re-read both authorities only after acquiring the lock.
            current_secure = _safe_get(backend)
            current_legacy = _read_legacy(legacy_path)
            if current_legacy.status == "invalid":
                if current_secure.status == STATUS_SUCCESS and current_secure.value:
                    return CredentialResult(
                        STATUS_SUCCESS,
                        current_secure.value,
                        "secure_store",
                        "Credential loaded; legacy cleanup requires attention.",
                        MIGRATION_FAILED,
                    )
                return _migration_failure()
            if current_legacy.status == "absent":
                return _credential_from_secure_result(current_secure)
            assert current_legacy.credential is not None
            return _migrate_locked(
                backend,
                current_secure,
                current_legacy.credential,
                legacy_path,
            )
    except InterProcessLockError:
        return _migration_failure()


def store_gemini_api_key(
    value: str,
    *,
    store: SecureStore | None = None,
) -> CredentialResult:
    """Store and read back a Gemini key before reporting persistence success."""

    backend = create_secure_store() if store is None else store
    stored = _safe_set(backend, value)
    if stored.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
        )
    if stored.status != STATUS_SUCCESS:
        return CredentialResult(
            STATUS_FAILED, message="Credential could not be stored."
        )
    verified = _safe_get(backend)
    if (
        verified.status != STATUS_SUCCESS
        or verified.value is None
        or not hmac.compare_digest(verified.value, value)
    ):
        return CredentialResult(
            STATUS_FAILED, message="Credential persistence could not be verified."
        )
    return CredentialResult(
        STATUS_SUCCESS,
        source="secure_store",
        message="Credential stored and verified securely.",
    )


def delete_gemini_api_key(
    *,
    store: SecureStore | None = None,
) -> CredentialResult:
    """Delete the Gemini key from secure storage with fixed safe status text."""

    backend = create_secure_store() if store is None else store
    deleted = _safe_delete(backend)
    if deleted.status == STATUS_SUCCESS:
        return CredentialResult(
            STATUS_SUCCESS,
            source="secure_store",
            message="Credential deleted securely.",
        )
    if deleted.status == STATUS_NOT_FOUND:
        return CredentialResult(
            STATUS_NOT_FOUND, message="Credential was not found."
        )
    if deleted.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
        )
    return CredentialResult(STATUS_FAILED, message="Credential could not be deleted.")


def require_gemini_api_key(*, legacy_path: Path | None = None) -> str:
    """Return the configured key or raise a fixed, non-secret error."""

    result = load_gemini_api_key(legacy_path=legacy_path)
    if not result.ok or result.value is None:
        raise RuntimeError("Gemini API credential is not configured securely.")
    return result.value


def _safe_get(backend: SecureStore) -> SecureStoreResult:
    try:
        return backend.get(GEMINI_SERVICE, GEMINI_ACCOUNT)
    except Exception:
        return SecureStoreResult(
            STATUS_FAILED, message="Secure storage operation failed."
        )


def _safe_set(backend: SecureStore, value: str) -> SecureStoreResult:
    try:
        return backend.set(GEMINI_SERVICE, GEMINI_ACCOUNT, value)
    except Exception:
        return SecureStoreResult(
            STATUS_FAILED, message="Secure storage operation failed."
        )


def _safe_delete(backend: SecureStore) -> SecureStoreResult:
    try:
        return backend.delete(GEMINI_SERVICE, GEMINI_ACCOUNT)
    except Exception:
        return SecureStoreResult(
            STATUS_FAILED, message="Secure storage operation failed."
        )


def _credential_from_secure_result(secured: SecureStoreResult) -> CredentialResult:
    if secured.status == STATUS_SUCCESS and secured.value:
        return CredentialResult(
            STATUS_SUCCESS,
            secured.value,
            "secure_store",
            "Credential loaded from secure storage.",
        )
    if secured.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
        )
    if secured.status == STATUS_FAILED or secured.status == STATUS_SUCCESS:
        return CredentialResult(
            STATUS_FAILED, message="Secure credential storage failed."
        )
    return CredentialResult(STATUS_NOT_FOUND, message="Credential was not found.")


def _migrate_locked(
    backend: SecureStore,
    secured: SecureStoreResult,
    legacy: _LegacyCredential,
    path: Path,
) -> CredentialResult:
    if secured.status == STATUS_SUCCESS and secured.value:
        verified_value = secured.value
    elif secured.status == STATUS_NOT_FOUND:
        stored = _safe_set(backend, legacy.value)
        if stored.status == STATUS_NOT_AVAILABLE:
            return CredentialResult(
                STATUS_NOT_AVAILABLE,
                message="Secure credential storage is not available.",
                migration_status=MIGRATION_FAILED,
            )
        if stored.status != STATUS_SUCCESS:
            return _migration_failure()
        verified = _safe_get(backend)
        if (
            verified.status != STATUS_SUCCESS
            or verified.value is None
            or not hmac.compare_digest(verified.value, legacy.value)
        ):
            return _migration_failure()
        verified_value = verified.value
    elif secured.status == STATUS_NOT_AVAILABLE:
        return CredentialResult(
            STATUS_NOT_AVAILABLE,
            message="Secure credential storage is not available.",
            migration_status=MIGRATION_FAILED,
        )
    else:
        return _migration_failure()

    if not _remove_legacy_key(path, legacy):
        return _migration_failure()
    return CredentialResult(
        STATUS_SUCCESS,
        verified_value,
        "secure_store_migration",
        "Credential migrated to secure storage.",
        MIGRATION_COMPLETED,
    )


def _migration_failure() -> CredentialResult:
    return CredentialResult(
        STATUS_FAILED,
        message="Legacy credential migration failed.",
        migration_status=MIGRATION_FAILED,
    )


def _migration_lock(path: Path) -> InterProcessFileLock:
    absolute = os.path.abspath(os.fspath(path))
    digest = hashlib.sha256(absolute.encode("utf-8", errors="surrogatepass")).hexdigest()
    identity = str(os.getuid()) if hasattr(os, "getuid") else "user"
    lock_path = Path(tempfile.gettempdir()) / f"jarvis-credential-{identity}-{digest}.lock"
    return InterProcessFileLock(lock_path, timeout_seconds=5)


def _read_legacy(path: Path | None) -> _LegacyReadResult:
    if path is None:
        return _LegacyReadResult("absent")
    descriptor: int | None = None
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return _LegacyReadResult("absent")
    except OSError:
        return _LegacyReadResult("invalid")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > _MAX_LEGACY_BYTES
    ):
        return _LegacyReadResult("invalid")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        return _LegacyReadResult("invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
        ):
            return _LegacyReadResult("invalid")
        raw = os.read(descriptor, _MAX_LEGACY_BYTES + 1)
        if len(raw) != opened.st_size:
            return _LegacyReadResult("invalid")
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            return _LegacyReadResult("invalid")
        if "gemini_api_key" not in document:
            return _LegacyReadResult("absent")
        value = document["gemini_api_key"]
        if not isinstance(value, str):
            return _LegacyReadResult("invalid")
        value = value.strip()
        if (
            not value
            or any(character in value for character in "\x00\r\n")
            or len(value.encode("utf-8")) > _MAX_SECRET_BYTES
        ):
            return _LegacyReadResult("invalid")
        return _LegacyReadResult(
            "valid",
            _LegacyCredential(
                value=value,
                document=document,
                device=opened.st_dev,
                inode=opened.st_ino,
                size=opened.st_size,
                modified_ns=opened.st_mtime_ns,
            ),
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return _LegacyReadResult("invalid")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _remove_legacy_key(path: Path, legacy: _LegacyCredential) -> bool:
    sanitized = dict(legacy.document)
    sanitized.pop("gemini_api_key", None)
    payload = (json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    temp_path: str | None = None
    quarantine_path: str | None = None
    descriptor: int | None = None
    try:
        current = os.lstat(path)
        if not _same_legacy_file(current, legacy):
            return False
        descriptor, temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".migration", dir=path.parent
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                return False
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.lstat(path)
        if not _same_legacy_file(current, legacy):
            return False
        # Do not use os.replace(temp, path): a concurrent writer could replace
        # the checked target between lstat and replace, and its unrelated JSON
        # fields would be lost. Move the observed file aside, verify that exact
        # inode, then install with a no-clobber hard link. Any collision fails
        # closed without overwriting either writer. POSIX can retain the
        # displaced file with owner-only permissions for recovery; Windows
        # deletes a quarantine it cannot restore because chmod does not create
        # an owner-only DACL there.
        quarantine_path = f"{temp_path}.quarantine"
        os.rename(path, quarantine_path)
        displaced = os.lstat(quarantine_path)
        if not _same_legacy_file(displaced, legacy):
            _restore_quarantine(quarantine_path, path)
            quarantine_path = None
            return False
        if os.name != "nt":
            _make_private_regular(quarantine_path)
        _link_regular_no_clobber(temp_path, path)
        staged = os.lstat(temp_path)
        if not _verify_installed_payload(path, staged, payload):
            return False
        os.unlink(temp_path)
        temp_path = None
        if not _verify_installed_payload(path, staged, payload):
            return False
        displaced = os.lstat(quarantine_path)
        if not _same_legacy_file(displaced, legacy):
            # A writer that kept the old inode open changed it after the move.
            # The finally block restores or securely disposes of those bytes.
            return False
        # This is the last recoverable check before deleting the plaintext
        # source. Pin and compare both the installed inode and complete bytes;
        # an lstat-only check would miss an in-place concurrent rewrite.
        if not _verify_installed_payload(path, staged, payload):
            return False
        os.unlink(quarantine_path)
        quarantine_path = None
        _fsync_directory(path.parent)
        # A non-cooperating legacy writer can race any advisory lock. Never
        # report migration success if it replaced or rewrote the sanitized file
        # during finalization.
        return _verify_installed_payload(path, staged, payload)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if quarantine_path is not None:
            _restore_quarantine(quarantine_path, path)


def _same_legacy_file(current: os.stat_result, legacy: _LegacyCredential) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and current.st_dev == legacy.device
        and current.st_ino == legacy.inode
        and current.st_size == legacy.size
        and current.st_mtime_ns == legacy.modified_ns
    )


def _link_regular_no_clobber(source: str | Path, target: str | Path) -> None:
    source_stat = os.lstat(source)
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
        raise OSError("Credential migration source is not available.")
    kwargs: dict[str, object] = {}
    if os.link in os.supports_follow_symlinks:
        kwargs["follow_symlinks"] = False
    os.link(source, target, **kwargs)


def _verify_installed_payload(
    path: str | Path,
    expected: os.stat_result,
    payload: bytes,
) -> bool:
    """Pin, fully read, and identity-check the sanitized migration result."""

    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != expected.st_dev
            or before.st_ino != expected.st_ino
            or before.st_size != len(payload)
        ):
            return False
        chunks: list[bytes] = []
        remaining = len(payload) + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if not hmac.compare_digest(b"".join(chunks), payload):
            return False
        after = os.fstat(descriptor)
        current = os.lstat(path)
        return (
            stat.S_ISREG(after.st_mode)
            and stat.S_ISREG(current.st_mode)
            and after.st_dev == before.st_dev == current.st_dev
            and after.st_ino == before.st_ino == current.st_ino
            and after.st_size == before.st_size == current.st_size
            and after.st_mtime_ns == before.st_mtime_ns == current.st_mtime_ns
        )
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _make_private_regular(path: str | Path) -> None:
    current = os.lstat(path)
    if not stat.S_ISREG(current.st_mode):
        raise OSError("Credential quarantine is not a regular file.")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        os.chmod(path, 0o600)


def _restore_quarantine(quarantine: str | Path, original: str | Path) -> None:
    """Restore without overwrite; never retain unprotected plaintext on Windows."""

    try:
        current = os.lstat(quarantine)
        if not stat.S_ISREG(current.st_mode):
            raise OSError("Credential quarantine is not a regular file.")
        if os.name != "nt":
            _make_private_regular(quarantine)
    except OSError:
        # Never follow or preserve a raced symlink/special file.
        try:
            os.unlink(quarantine)
        except OSError:
            pass
        return
    try:
        _link_regular_no_clobber(quarantine, original)
    except OSError:
        if os.name == "nt":
            # NT rename is no-clobber. It preserves all legacy JSON fields when
            # hard links are unavailable, but never overwrites a concurrent
            # writer. chmod(0600) is not an ACL boundary on Windows, so a
            # quarantine that cannot be restored must not be retained.
            try:
                _rename_windows_no_clobber(quarantine, original)
                return
            except OSError:
                try:
                    os.unlink(quarantine)
                except OSError:
                    pass
        return
    try:
        os.unlink(quarantine)
    except OSError:
        pass


def _rename_windows_no_clobber(source: str | Path, target: str | Path) -> None:
    """Use Windows' documented os.rename no-replacement semantics."""

    if os.name != "nt":
        raise OSError("Windows no-clobber rename is not available.")
    os.rename(source, target)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CredentialResult",
    "GEMINI_ACCOUNT",
    "GEMINI_SERVICE",
    "MIGRATION_COMPLETED",
    "MIGRATION_FAILED",
    "MIGRATION_NOT_NEEDED",
    "delete_gemini_api_key",
    "load_gemini_api_key",
    "require_gemini_api_key",
    "store_gemini_api_key",
]

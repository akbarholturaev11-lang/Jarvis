"""Fail-closed updater transaction and honest platform adapter contracts.

The default platform factory never selects a mutating adapter in this
foundation build.  An explicitly constructed development-only macOS adapter
lives in :mod:`core.macos_update` for real temporary-filesystem integration;
it cannot be selected by a frozen production runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as stdlib_platform
import secrets
import stat
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Iterator

from core.interprocess_lock import InterProcessFileLock
from core.product_updates import VerifiedStagedUpdate
from core.product_version import ProductVersion, normalize_platform


DEFAULT_HEALTH_TIMEOUT_SECONDS: Final = 30.0
MAX_HEALTH_TIMEOUT_SECONDS: Final = 60.0
MAX_JOURNAL_BYTES: Final = 4096

_JOURNAL_SCHEMA: Final = "jarvis.update-rollback-checkpoint"
_JOURNAL_SCHEMA_VERSION: Final = 1
_BACKUP_REFERENCE_MAX_LENGTH: Final = 256
_ARTIFACT_CONSTRUCTION_TOKEN: Final = object()


def _validate_backup_reference(value: object) -> str:
    if (
        type(value) is not str
        or not 3 <= len(value) <= _BACKUP_REFERENCE_MAX_LENGTH
        or any(
            not (character.isalnum() or character in "._:@+-")
            for character in value
        )
    ):
        raise ValueError("backup reference must be opaque")
    return value


@dataclass(frozen=True, slots=True)
class UpdateRollbackCheckpoint:
    source: ProductVersion
    target: ProductVersion
    backup_reference: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, ProductVersion) or not isinstance(
            self.target, ProductVersion
        ):
            raise TypeError("checkpoint versions must be ProductVersion values")
        if not self.target.is_newer_than(self.source):
            raise ValueError("checkpoint target must be newer than source")
        _validate_backup_reference(self.backup_reference)


class PrivateUpdateJournal:
    """Atomic private checkpoint retained until install or rollback is proven."""

    __slots__ = ("_lock", "_path")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        supplied = Path(path).expanduser()
        if not supplied.is_absolute() or supplied.is_symlink():
            raise ValueError("update journal path must be an absolute non-symlink")
        parent = supplied.parent.resolve(strict=False)
        self._path = parent / supplied.name
        self._lock = InterProcessFileLock(
            self._path.with_name(self._path.name + ".lock")
        )

    @property
    def path(self) -> Path:
        return self._path

    def __repr__(self) -> str:
        return "PrivateUpdateJournal(path=<private>)"

    def acquire(self):
        return self._lock.acquire()

    @staticmethod
    def _document(checkpoint: UpdateRollbackCheckpoint) -> dict[str, object]:
        return {
            "schema": _JOURNAL_SCHEMA,
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "state": "rollback_required",
            "source_version": str(checkpoint.source.version),
            "source_build": checkpoint.source.build,
            "target_version": str(checkpoint.target.version),
            "target_build": checkpoint.target.build,
            "backup_reference": checkpoint.backup_reference,
        }

    @classmethod
    def _raw(cls, checkpoint: UpdateRollbackCheckpoint) -> bytes:
        return json.dumps(
            cls._document(checkpoint),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _ensure_parent(self) -> None:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink():
            raise OSError("journal parent is a symlink")
        opened = parent.stat()
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("journal parent is not a directory")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise OSError("journal parent owner mismatch")
        if os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077:
            raise OSError("journal parent permissions are unsafe")

    def load_locked(self) -> UpdateRollbackCheckpoint | None:
        self._ensure_parent()
        if self._path.is_symlink():
            raise ValueError("update journal is invalid")
        if not self._path.exists():
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not 1 <= opened.st_size <= MAX_JOURNAL_BYTES
                or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            ):
                raise ValueError("update journal is invalid")
            raw = os.read(descriptor, MAX_JOURNAL_BYTES + 1)
            if len(raw) != opened.st_size or os.read(descriptor, 1):
                raise ValueError("update journal is invalid")
        finally:
            os.close(descriptor)
        try:
            document = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("update journal is invalid") from exc
        expected_fields = {
            "schema",
            "schema_version",
            "state",
            "source_version",
            "source_build",
            "target_version",
            "target_build",
            "backup_reference",
        }
        if (
            type(document) is not dict
            or set(document) != expected_fields
            or document.get("schema") != _JOURNAL_SCHEMA
            or document.get("schema_version") != _JOURNAL_SCHEMA_VERSION
            or document.get("state") != "rollback_required"
        ):
            raise ValueError("update journal is invalid")
        checkpoint = UpdateRollbackCheckpoint(
            ProductVersion.parse(
                document["source_version"],
                document["source_build"],
            ),
            ProductVersion.parse(
                document["target_version"],
                document["target_build"],
            ),
            document["backup_reference"],
        )
        if self._raw(checkpoint) != raw:
            raise ValueError("update journal is invalid")
        return checkpoint

    def write_locked(self, checkpoint: UpdateRollbackCheckpoint) -> None:
        if not isinstance(checkpoint, UpdateRollbackCheckpoint):
            raise TypeError("checkpoint is invalid")
        self._ensure_parent()
        if self._path.is_symlink():
            raise OSError("journal target is a symlink")
        raw = self._raw(checkpoint)
        temporary = self._path.parent / (
            ".update-journal-" + secrets.token_hex(16) + ".tmp"
        )
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = None
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            if os.name != "nt":
                self._path.chmod(0o600)
            self._fsync_parent()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def clear_locked(self) -> None:
        if self._path.is_symlink():
            raise OSError("journal target is a symlink")
        self._path.unlink(missing_ok=True)
        self._fsync_parent()

    def _fsync_parent(self) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class AdapterStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    NOT_AVAILABLE = "not_available"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class UpdaterCapability:
    platform: str
    status: AdapterStatus
    mutation_enabled: bool
    blocker: str | None

    def __post_init__(self) -> None:
        if type(self.status) is not AdapterStatus:
            raise TypeError("status must be an AdapterStatus")
        if type(self.mutation_enabled) is not bool:
            raise TypeError("mutation_enabled must be a boolean")
        if self.mutation_enabled != (self.status is AdapterStatus.SUCCESS):
            raise ValueError("only successful capability may enable mutation")
        if self.mutation_enabled == (self.blocker is not None):
            raise ValueError("capability blocker is inconsistent")


@dataclass(frozen=True, slots=True)
class AdapterBackupResult:
    status: AdapterStatus
    backup_reference: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not AdapterStatus:
            raise TypeError("status must be an AdapterStatus")
        if self.status is AdapterStatus.SUCCESS:
            _validate_backup_reference(self.backup_reference)
        elif self.backup_reference is not None:
            raise ValueError("failed backup cannot carry a reference")


@dataclass(frozen=True, slots=True)
class AdapterMutationResult:
    status: AdapterStatus
    mutation_possible: bool

    def __post_init__(self) -> None:
        if type(self.status) is not AdapterStatus:
            raise TypeError("status must be an AdapterStatus")
        if type(self.mutation_possible) is not bool:
            raise TypeError("mutation_possible must be a boolean")
        if self.status in {AdapterStatus.NOT_AVAILABLE, AdapterStatus.UNSUPPORTED}:
            if self.mutation_possible:
                raise ValueError("unavailable adapter cannot report mutation")


@dataclass(frozen=True, slots=True)
class AdapterVerificationResult:
    status: AdapterStatus
    installed: ProductVersion | None = None
    healthy: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not AdapterStatus:
            raise TypeError("status must be an AdapterStatus")
        if self.installed is not None and not isinstance(
            self.installed, ProductVersion
        ):
            raise TypeError("installed must be a ProductVersion")
        if type(self.healthy) is not bool:
            raise TypeError("healthy must be a boolean")
        if self.status is AdapterStatus.SUCCESS:
            if self.installed is None or not self.healthy:
                raise ValueError("successful verification requires healthy identity")
        elif self.installed is not None or self.healthy:
            raise ValueError("failed verification cannot carry trusted identity")


class VerifiedArtifactHandle:
    """A verified staged artifact pinned to an open OS file descriptor.

    The coordinator owns the original descriptor and keeps it open for the
    complete synchronous ``install`` call.  No pathname is exposed for
    reopening.  Before any mutation, an adapter must copy the bytes into its
    own inaccessible or privileged temporary file through
    ``copy_verified_to_private_descriptor`` and consume only that independently
    verified copy.
    """

    __slots__ = ("_descriptor", "_byte_size", "_sha256")

    def __init__(
        self,
        descriptor: int,
        *,
        byte_size: int,
        sha256: str,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _ARTIFACT_CONSTRUCTION_TOKEN:
            raise TypeError("verified artifact handles are coordinator-owned")
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("artifact descriptor is invalid")
        if type(byte_size) is not int or byte_size <= 0:
            raise ValueError("artifact byte size is invalid")
        if (
            type(sha256) is not str
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("artifact digest is invalid")
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != byte_size
        ):
            raise ValueError("artifact descriptor does not match verified metadata")
        self._descriptor: int | None = descriptor
        self._byte_size = byte_size
        self._sha256 = sha256

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    def copy_verified_to_private_descriptor(
        self,
        destination_descriptor: int,
    ) -> bool:
        """Copy, hash, size-check, and fsync into an adapter-owned private file.

        The destination must already be an open regular file that is distinct
        from the staged source.  On POSIX it must have no group/other access.
        False means the destination contains no trusted update and mutation
        must not begin.
        """

        source_descriptor = self._descriptor
        if source_descriptor is None:
            raise RuntimeError("verified artifact handle is closed")
        if type(destination_descriptor) is not int or destination_descriptor < 0:
            raise ValueError("private artifact destination is invalid")

        def discard_destination() -> None:
            try:
                os.ftruncate(destination_descriptor, 0)
                os.lseek(destination_descriptor, 0, os.SEEK_SET)
            except OSError:
                pass

        try:
            source = os.fstat(source_descriptor)
            destination = os.fstat(destination_descriptor)
            posix_private_destination = (
                os.name == "nt"
                or (
                    stat.S_IMODE(destination.st_mode) & 0o077 == 0
                    and (
                        destination.st_nlink == 0
                        or (
                            hasattr(os, "getuid")
                            and destination.st_uid != os.getuid()
                        )
                    )
                )
            )
            if (
                not stat.S_ISREG(source.st_mode)
                or source.st_size != self._byte_size
                or not stat.S_ISREG(destination.st_mode)
                or (source.st_dev, source.st_ino)
                == (destination.st_dev, destination.st_ino)
                or not posix_private_destination
            ):
                discard_destination()
                return False

            os.ftruncate(destination_descriptor, 0)
            os.lseek(source_descriptor, 0, os.SEEK_SET)
            os.lseek(destination_descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            copied = 0
            while copied < self._byte_size:
                chunk = os.read(
                    source_descriptor,
                    min(self._byte_size - copied, 256 * 1024),
                )
                if not chunk:
                    discard_destination()
                    return False
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        discard_destination()
                        return False
                    view = view[written:]
                copied += len(chunk)
            if (
                os.read(source_descriptor, 1)
                or copied != self._byte_size
                or digest.hexdigest() != self._sha256
            ):
                discard_destination()
                return False
            os.fsync(destination_descriptor)
            copied_file = os.fstat(destination_descriptor)
            if copied_file.st_size != self._byte_size:
                discard_destination()
                return False
            os.lseek(destination_descriptor, 0, os.SEEK_SET)
            copied_digest = hashlib.sha256()
            remaining = self._byte_size
            while remaining:
                chunk = os.read(
                    destination_descriptor,
                    min(remaining, 256 * 1024),
                )
                if not chunk:
                    discard_destination()
                    return False
                copied_digest.update(chunk)
                remaining -= len(chunk)
            if (
                os.read(destination_descriptor, 1)
                or copied_digest.hexdigest() != self._sha256
            ):
                discard_destination()
                return False
            os.lseek(destination_descriptor, 0, os.SEEK_SET)
            return True
        except OSError:
            discard_destination()
            return False

    def _close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return f"VerifiedArtifactHandle(state={state!r}, artifact=<pinned>)"


class UpdaterPlatformAdapter(ABC):
    """Persisted-backup and atomic-replacement boundary.

    A real adapter must create a durable backup before replacement, consume the
    staged package from the supplied open verified handle without reopening a
    path, independently copy and verify it into adapter-owned inaccessible or
    privileged storage before mutation, perform an atomic swap (or equivalent
    platform transaction), expose a bounded exact version/health marker, and
    restore that same backup on rollback.
    """

    platform_key = "unknown"

    def validate_journal_path(self, journal_path: Path) -> bool:
        """Confirm a durable journal cannot be moved by adapter mutation."""

        return (
            isinstance(journal_path, Path)
            and journal_path.is_absolute()
            and not journal_path.is_symlink()
        )

    def capability(self) -> UpdaterCapability:
        return UpdaterCapability(
            self.platform_key,
            AdapterStatus.UNSUPPORTED,
            False,
            "verified_atomic_replacement_adapter_not_implemented",
        )

    @abstractmethod
    def prepare_persisted_backup(
        self,
        *,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterBackupResult:
        raise NotImplementedError

    @abstractmethod
    def install(
        self,
        staged_artifact: VerifiedArtifactHandle,
        *,
        backup_reference: str,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterMutationResult:
        raise NotImplementedError

    @abstractmethod
    def verify_installed(
        self,
        expected: ProductVersion,
        *,
        timeout_seconds: float,
    ) -> AdapterVerificationResult:
        raise NotImplementedError

    @abstractmethod
    def rollback(
        self,
        backup_reference: str,
        expected_previous: ProductVersion,
    ) -> AdapterMutationResult:
        raise NotImplementedError


class _UnavailableUpdaterAdapter(UpdaterPlatformAdapter):
    adapter_status = AdapterStatus.NOT_AVAILABLE
    blocker = "verified_atomic_replacement_adapter_not_configured"

    def capability(self) -> UpdaterCapability:
        return UpdaterCapability(
            self.platform_key,
            self.adapter_status,
            False,
            self.blocker,
        )

    def prepare_persisted_backup(
        self,
        *,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterBackupResult:
        return AdapterBackupResult(self.adapter_status)

    def install(
        self,
        staged_artifact: VerifiedArtifactHandle,
        *,
        backup_reference: str,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterMutationResult:
        return AdapterMutationResult(self.adapter_status, False)

    def verify_installed(
        self,
        expected: ProductVersion,
        *,
        timeout_seconds: float,
    ) -> AdapterVerificationResult:
        return AdapterVerificationResult(self.adapter_status)

    def rollback(
        self,
        backup_reference: str,
        expected_previous: ProductVersion,
    ) -> AdapterMutationResult:
        return AdapterMutationResult(self.adapter_status, False)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.adapter_status.value!r})"


class MacOSUpdaterAdapter(_UnavailableUpdaterAdapter):
    """Read-only production helper assessment; mutation stays fail-closed.

    Even a trusted helper remains unavailable until its privileged request,
    shutdown and atomic-swap protocol is implemented and independently audited.
    The fixed helper path cannot be redirected by client configuration.
    """

    platform_key = "macos"
    blocker = "signed_notarized_atomic_helper_not_configured"

    def __init__(
        self,
        *,
        expected_team_id: str | None = None,
        designated_requirement: str | None = None,
        frozen: bool | None = None,
        _validation_runner: object | None = None,
    ) -> None:
        self._expected_team_id = expected_team_id
        self._designated_requirement = designated_requirement
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._validation_runner = _validation_runner

    def capability(self) -> UpdaterCapability:
        from core.macos_update import (
            PRODUCTION_MACOS_HELPER_PATH,
            assess_production_macos_helper,
        )

        assessment = assess_production_macos_helper(
            PRODUCTION_MACOS_HELPER_PATH,
            expected_team_id=self._expected_team_id,
            designated_requirement=self._designated_requirement,
            frozen=self._frozen,
            runner=self._validation_runner,  # type: ignore[arg-type]
            required_uid=0,
        )
        return UpdaterCapability(
            self.platform_key,
            AdapterStatus.NOT_AVAILABLE,
            False,
            assessment.blocker or "privileged_helper_update_protocol_not_enabled",
        )

    def prepare_persisted_backup(
        self,
        *,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterBackupResult:
        self.capability()
        return AdapterBackupResult(AdapterStatus.NOT_AVAILABLE)

    def install(
        self,
        staged_artifact: VerifiedArtifactHandle,
        *,
        backup_reference: str,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterMutationResult:
        self.capability()
        return AdapterMutationResult(AdapterStatus.NOT_AVAILABLE, False)

    def verify_installed(
        self,
        expected: ProductVersion,
        *,
        timeout_seconds: float,
    ) -> AdapterVerificationResult:
        self.capability()
        return AdapterVerificationResult(AdapterStatus.NOT_AVAILABLE)

    def rollback(
        self,
        backup_reference: str,
        expected_previous: ProductVersion,
    ) -> AdapterMutationResult:
        self.capability()
        return AdapterMutationResult(AdapterStatus.NOT_AVAILABLE, False)


class WindowsUpdaterAdapter(_UnavailableUpdaterAdapter):
    """Honest placeholder until a verified Windows installer exists."""

    platform_key = "windows"
    blocker = "signed_installer_atomic_helper_not_configured"


class LinuxUpdaterAdapter(_UnavailableUpdaterAdapter):
    """Honest placeholder until a verified Linux package adapter exists."""

    platform_key = "linux"
    blocker = "signed_package_atomic_helper_not_configured"


class UnsupportedUpdaterAdapter(_UnavailableUpdaterAdapter):
    platform_key = "unknown"
    adapter_status = AdapterStatus.UNSUPPORTED
    blocker = "platform_not_supported"


def create_updater_adapter(system: str | None = None) -> UpdaterPlatformAdapter:
    detected = stdlib_platform.system() if system is None else system
    normalized = normalize_platform(detected)
    if normalized == "macos":
        return MacOSUpdaterAdapter()
    if normalized == "windows":
        return WindowsUpdaterAdapter()
    if normalized == "linux":
        return LinuxUpdaterAdapter()
    return UnsupportedUpdaterAdapter()


class TransactionStatus(StrEnum):
    INSTALLED = "installed"
    PRESERVED = "preserved"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_REQUIRED = "rollback_required"
    NOT_AVAILABLE = "not_available"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateTransactionResult:
    status: TransactionStatus
    message: str = field(repr=False)
    source: ProductVersion | None = None
    target: ProductVersion | None = None

    @property
    def installed(self) -> bool:
        return self.status is TransactionStatus.INSTALLED

    @property
    def safe(self) -> bool:
        return self.status in {
            TransactionStatus.INSTALLED,
            TransactionStatus.PRESERVED,
            TransactionStatus.ROLLED_BACK,
            TransactionStatus.NOT_AVAILABLE,
            TransactionStatus.UNSUPPORTED,
        }


def _transaction_result(
    status: TransactionStatus,
    message: str,
    source: ProductVersion | None = None,
    target: ProductVersion | None = None,
) -> UpdateTransactionResult:
    return UpdateTransactionResult(status, message, source, target)


def _verified_identity(
    verification: object,
    expected: ProductVersion,
) -> bool:
    return (
        isinstance(verification, AdapterVerificationResult)
        and verification.status is AdapterStatus.SUCCESS
        and verification.healthy
        and verification.installed == expected
    )


def _open_verified_staged_artifact(
    staged: VerifiedStagedUpdate,
) -> VerifiedArtifactHandle | None:
    descriptor: int | None = None
    try:
        path = staged.path
        if not path.is_absolute() or path.is_symlink():
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != staged.byte_size
        ):
            return None
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 256 * 1024))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or digest.hexdigest() != staged.sha256:
            return None
        os.lseek(descriptor, 0, os.SEEK_SET)
        artifact = VerifiedArtifactHandle(
            descriptor,
            byte_size=staged.byte_size,
            sha256=staged.sha256,
            _construction_token=_ARTIFACT_CONSTRUCTION_TOKEN,
        )
        descriptor = None
        return artifact
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def open_verified_staged_artifact(
    staged: VerifiedStagedUpdate,
) -> Iterator[VerifiedArtifactHandle | None]:
    """Verify once and pin the exact file object until the context exits."""

    artifact = (
        _open_verified_staged_artifact(staged)
        if isinstance(staged, VerifiedStagedUpdate)
        else None
    )
    try:
        yield artifact
    finally:
        if artifact is not None:
            artifact._close()


class UpdateTransactionCoordinator:
    """Orchestrate durable backup, atomic install, health proof, and rollback."""

    __slots__ = (
        "_adapter",
        "_health_timeout_seconds",
        "_journal",
        "_journal_invalid",
        "_pending_backup_reference",
        "_pending_source",
        "_pending_target",
    )

    def __init__(
        self,
        adapter: UpdaterPlatformAdapter,
        *,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        journal_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if not isinstance(adapter, UpdaterPlatformAdapter):
            raise TypeError("adapter must be an UpdaterPlatformAdapter")
        if (
            isinstance(health_timeout_seconds, bool)
            or not isinstance(health_timeout_seconds, (int, float))
            or not 0 < float(health_timeout_seconds) <= MAX_HEALTH_TIMEOUT_SECONDS
        ):
            raise ValueError("health timeout is outside the allowed range")
        if journal_path is None and not isinstance(
            adapter, _UnavailableUpdaterAdapter
        ):
            raise ValueError("a durable update journal is required")
        self._adapter = adapter
        self._health_timeout_seconds = float(health_timeout_seconds)
        self._journal = (
            None if journal_path is None else PrivateUpdateJournal(journal_path)
        )
        if self._journal is not None and not adapter.validate_journal_path(
            self._journal.path
        ):
            raise ValueError("update journal overlaps an adapter mutation path")
        self._journal_invalid = False
        self._pending_backup_reference: str | None = None
        self._pending_source: ProductVersion | None = None
        self._pending_target: ProductVersion | None = None
        if self._journal is not None:
            try:
                with self._journal.acquire():
                    self._reload_checkpoint_locked()
            except Exception:
                self._journal_invalid = True

    @property
    def rollback_required(self) -> bool:
        return self._journal_invalid or self._pending_source is not None

    def __repr__(self) -> str:
        state = "rollback_required" if self.rollback_required else "ready"
        return f"UpdateTransactionCoordinator(state={state!r}, adapter=<configured>)"

    def _verification(
        self,
        expected: ProductVersion,
    ) -> AdapterVerificationResult | None:
        try:
            verification = self._adapter.verify_installed(
                expected,
                timeout_seconds=self._health_timeout_seconds,
            )
        except Exception:
            return None
        return (
            verification
            if isinstance(verification, AdapterVerificationResult)
            else None
        )

    def _verify(self, expected: ProductVersion) -> bool:
        return _verified_identity(self._verification(expected), expected)

    def _reload_checkpoint_locked(self) -> None:
        if self._journal is None:
            self._journal_invalid = False
            return
        checkpoint = self._journal.load_locked()
        self._journal_invalid = False
        if checkpoint is None:
            self._pending_source = None
            self._pending_target = None
            self._pending_backup_reference = None
            return
        self._pending_source = checkpoint.source
        self._pending_target = checkpoint.target
        self._pending_backup_reference = checkpoint.backup_reference

    def _set_checkpoint_locked(
        self,
        *,
        source: ProductVersion,
        target: ProductVersion,
        backup_reference: str,
    ) -> None:
        if self._journal is None:
            raise OSError("durable update journal is unavailable")
        checkpoint = UpdateRollbackCheckpoint(
            source,
            target,
            backup_reference,
        )
        self._journal.write_locked(checkpoint)
        self._pending_source = source
        self._pending_target = target
        self._pending_backup_reference = backup_reference

    def _clear_checkpoint_locked(self) -> None:
        if self._journal is not None:
            self._journal.clear_locked()
        self._pending_source = None
        self._pending_target = None
        self._pending_backup_reference = None
        self._journal_invalid = False

    def _rollback_locked(self) -> UpdateTransactionResult:
        source = self._pending_source
        target = self._pending_target
        backup_reference = self._pending_backup_reference
        if source is None or target is None or backup_reference is None:
            return _transaction_result(
                TransactionStatus.ROLLBACK_REQUIRED,
                "Rollback checkpoint is unavailable or invalid.",
            )
        # A crash may happen after the durable checkpoint but before mutation,
        # or after rollback verification but before journal deletion.  Exact
        # source health is sufficient to safely clear either checkpoint.
        if self._verify(source):
            try:
                self._clear_checkpoint_locked()
            except OSError:
                return _transaction_result(
                    TransactionStatus.ROLLBACK_REQUIRED,
                    "Previous version is healthy but checkpoint cleanup failed.",
                    source,
                    target,
                )
            return _transaction_result(
                TransactionStatus.PRESERVED,
                "Previous version is present and verified.",
                source,
                target,
            )
        try:
            rollback = self._adapter.rollback(backup_reference, source)
        except Exception:
            rollback = AdapterMutationResult(AdapterStatus.FAILED, True)
        if (
            isinstance(rollback, AdapterMutationResult)
            and rollback.status is AdapterStatus.SUCCESS
            and self._verify(source)
        ):
            try:
                self._clear_checkpoint_locked()
            except OSError:
                return _transaction_result(
                    TransactionStatus.ROLLBACK_REQUIRED,
                    "Rollback succeeded but checkpoint cleanup failed.",
                    source,
                    target,
                )
            return _transaction_result(
                TransactionStatus.ROLLED_BACK,
                "Previous version was restored and verified.",
                source,
                target,
            )
        return _transaction_result(
            TransactionStatus.ROLLBACK_REQUIRED,
            "Rollback has not been verified; retry is blocked.",
            source,
            target,
        )

    def recover(self) -> UpdateTransactionResult:
        """Resolve an outstanding rollback checkpoint before any retry."""

        if self._journal is None:
            return _transaction_result(
                TransactionStatus.INVALID,
                "Durable update journal is not configured.",
            )
        try:
            with self._journal.acquire():
                self._reload_checkpoint_locked()
                if self._journal_invalid:
                    return _transaction_result(
                        TransactionStatus.ROLLBACK_REQUIRED,
                        "Rollback checkpoint is invalid.",
                    )
                if self._pending_source is None:
                    return _transaction_result(
                        TransactionStatus.INVALID,
                        "No rollback checkpoint exists.",
                    )
                return self._rollback_locked()
        except Exception:
            self._journal_invalid = True
            return _transaction_result(
                TransactionStatus.ROLLBACK_REQUIRED,
                "Rollback journal is not available.",
            )

    def recover_if_required(self) -> UpdateTransactionResult | None:
        """Freshly probe and resolve startup recovery under one journal lock.

        Returning ``None`` proves there was no checkpoint at the instant of the
        locked read. Any unreadable state returns ``ROLLBACK_REQUIRED`` so a
        stale in-memory flag can never open the runtime past a disk checkpoint.
        """

        if self._journal is None:
            return None
        try:
            with self._journal.acquire():
                self._reload_checkpoint_locked()
                if self._journal_invalid:
                    return _transaction_result(
                        TransactionStatus.ROLLBACK_REQUIRED,
                        "Rollback checkpoint is invalid.",
                    )
                if self._pending_source is None:
                    return None
                return self._rollback_locked()
        except Exception:
            self._journal_invalid = True
            return _transaction_result(
                TransactionStatus.ROLLBACK_REQUIRED,
                "Rollback journal is not available.",
            )

    def apply(self, staged: VerifiedStagedUpdate) -> UpdateTransactionResult:
        if self._journal is None:
            return self._apply_locked(staged)
        try:
            with self._journal.acquire():
                self._reload_checkpoint_locked()
                return self._apply_locked(staged)
        except Exception:
            self._journal_invalid = True
            return _transaction_result(
                TransactionStatus.ROLLBACK_REQUIRED,
                "Update journal is not available; installation is blocked.",
            )

    def _apply_locked(
        self,
        staged: VerifiedStagedUpdate,
    ) -> UpdateTransactionResult:
        if self.rollback_required:
            return _transaction_result(
                TransactionStatus.ROLLBACK_REQUIRED,
                "A previous rollback must be verified before retry.",
                self._pending_source,
                self._pending_target,
            )
        if (
            not isinstance(staged, VerifiedStagedUpdate)
            or not staged.target.is_newer_than(staged.source)
        ):
            return _transaction_result(
                TransactionStatus.INVALID,
                "Staged update is invalid.",
            )
        with open_verified_staged_artifact(staged) as staged_artifact:
            if staged_artifact is None:
                return _transaction_result(
                    TransactionStatus.INVALID,
                    "Staged update is invalid.",
                )
            return self._apply_verified_locked(staged, staged_artifact)

    def _apply_verified_locked(
        self,
        staged: VerifiedStagedUpdate,
        staged_artifact: VerifiedArtifactHandle,
    ) -> UpdateTransactionResult:
        preflight = self._verification(staged.source)
        if preflight is not None and preflight.status in {
            AdapterStatus.NOT_AVAILABLE,
            AdapterStatus.UNSUPPORTED,
        }:
            status = (
                TransactionStatus.NOT_AVAILABLE
                if preflight.status is AdapterStatus.NOT_AVAILABLE
                else TransactionStatus.UNSUPPORTED
            )
            return _transaction_result(
                status,
                "Updater adapter cannot verify the installed application.",
                staged.source,
                staged.target,
            )
        if not _verified_identity(preflight, staged.source):
            return _transaction_result(
                TransactionStatus.FAILED,
                "Current installed version could not be verified.",
                staged.source,
                staged.target,
            )
        try:
            backup = self._adapter.prepare_persisted_backup(
                source=staged.source,
                target=staged.target,
            )
        except Exception:
            backup = AdapterBackupResult(AdapterStatus.FAILED)
        if not isinstance(backup, AdapterBackupResult):
            backup = AdapterBackupResult(AdapterStatus.FAILED)
        if backup.status in {
            AdapterStatus.NOT_AVAILABLE,
            AdapterStatus.UNSUPPORTED,
        }:
            if self._verify(staged.source):
                status = (
                    TransactionStatus.NOT_AVAILABLE
                    if backup.status is AdapterStatus.NOT_AVAILABLE
                    else TransactionStatus.UNSUPPORTED
                )
                return _transaction_result(
                    status,
                    "Persisted backup capability is not available.",
                    staged.source,
                    staged.target,
                )
            return _transaction_result(
                TransactionStatus.FAILED,
                "Backup unavailable and old version could not be verified.",
                staged.source,
                staged.target,
            )
        if backup.status is not AdapterStatus.SUCCESS or backup.backup_reference is None:
            status = (
                TransactionStatus.PRESERVED
                if self._verify(staged.source)
                else TransactionStatus.FAILED
            )
            return _transaction_result(
                status,
                "Persisted backup could not be prepared.",
                staged.source,
                staged.target,
            )
        try:
            self._set_checkpoint_locked(
                source=staged.source,
                target=staged.target,
                backup_reference=backup.backup_reference,
            )
        except (OSError, TypeError, ValueError):
            return _transaction_result(
                TransactionStatus.FAILED,
                "Durable rollback checkpoint could not be written.",
                staged.source,
                staged.target,
            )
        try:
            installation = self._adapter.install(
                staged_artifact,
                backup_reference=backup.backup_reference,
                source=staged.source,
                target=staged.target,
            )
        except Exception:
            installation = AdapterMutationResult(AdapterStatus.FAILED, True)
        if not isinstance(installation, AdapterMutationResult):
            installation = AdapterMutationResult(AdapterStatus.FAILED, True)

        if installation.status in {
            AdapterStatus.NOT_AVAILABLE,
            AdapterStatus.UNSUPPORTED,
        }:
            if self._verify(staged.source):
                try:
                    self._clear_checkpoint_locked()
                except OSError:
                    return _transaction_result(
                        TransactionStatus.ROLLBACK_REQUIRED,
                        "Old version is healthy but checkpoint cleanup failed.",
                        staged.source,
                        staged.target,
                    )
                status = (
                    TransactionStatus.NOT_AVAILABLE
                    if installation.status is AdapterStatus.NOT_AVAILABLE
                    else TransactionStatus.UNSUPPORTED
                )
                return _transaction_result(
                    status,
                    "Updater adapter is not available; old version is verified.",
                    staged.source,
                    staged.target,
                )
            return self._rollback_locked()

        if installation.status is AdapterStatus.SUCCESS:
            if self._verify(staged.target):
                try:
                    self._clear_checkpoint_locked()
                except OSError:
                    return self._rollback_locked()
                return _transaction_result(
                    TransactionStatus.INSTALLED,
                    "Expected version was installed and passed health verification.",
                    staged.source,
                    staged.target,
                )
            return self._rollback_locked()

        if not installation.mutation_possible and self._verify(staged.source):
            try:
                self._clear_checkpoint_locked()
            except OSError:
                return _transaction_result(
                    TransactionStatus.ROLLBACK_REQUIRED,
                    "Old version is healthy but checkpoint cleanup failed.",
                    staged.source,
                    staged.target,
                )
            return _transaction_result(
                TransactionStatus.PRESERVED,
                "Installation failed before mutation; old version is verified.",
                staged.source,
                staged.target,
            )
        return self._rollback_locked()


__all__ = [
    "DEFAULT_HEALTH_TIMEOUT_SECONDS",
    "MAX_HEALTH_TIMEOUT_SECONDS",
    "MAX_JOURNAL_BYTES",
    "AdapterBackupResult",
    "AdapterMutationResult",
    "AdapterStatus",
    "AdapterVerificationResult",
    "LinuxUpdaterAdapter",
    "MacOSUpdaterAdapter",
    "PrivateUpdateJournal",
    "TransactionStatus",
    "UnsupportedUpdaterAdapter",
    "UpdateTransactionCoordinator",
    "UpdateTransactionResult",
    "UpdateRollbackCheckpoint",
    "UpdaterPlatformAdapter",
    "UpdaterCapability",
    "VerifiedArtifactHandle",
    "WindowsUpdaterAdapter",
    "create_updater_adapter",
    "open_verified_staged_artifact",
]

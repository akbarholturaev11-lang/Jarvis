"""Secure, read-only local release artifact storage adapter."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import threading
from pathlib import Path

from core.release_manifest import MAX_ARTIFACT_BYTES

from .models import (
    validate_byte_size,
    validate_sha256,
    validate_storage_key,
)


DEFAULT_LOCAL_ARTIFACT_READ_LIMIT = 1024 * 1024 * 1024
ARTIFACT_VERIFY_CHUNK_BYTES = 1024 * 1024
ARTIFACT_STREAM_MAX_READ_BYTES = 1024 * 1024


class ReleaseArtifactStorageError(RuntimeError):
    """Base class for sanitized release artifact storage failures."""


class ReleaseArtifactStorageValidationError(ReleaseArtifactStorageError):
    """An artifact read request is not safely bounded."""


class ReleaseArtifactStorageIntegrityError(ReleaseArtifactStorageError):
    """The stored object does not match trusted metadata or path rules."""


class ReleaseArtifactStorageNotAvailableError(ReleaseArtifactStorageError):
    """The private artifact root or object cannot be read safely."""


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_snapshot(opened: os.stat_result) -> tuple[int, ...]:
    return (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mode,
        opened.st_uid,
        opened.st_nlink,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )


class PinnedVerifiedReleaseArtifactStream:
    """Bounded reader over the exact descriptor verified by the object store."""

    __slots__ = (
        "_byte_size",
        "_closed",
        "_descriptor",
        "_expected_sha256",
        "_hasher",
        "_lock",
        "_remaining",
        "_snapshot",
        "_verified_complete",
    )

    def __init__(
        self,
        descriptor: int,
        *,
        byte_size: int,
        expected_sha256: str,
        snapshot: tuple[int, ...],
    ) -> None:
        self._descriptor = descriptor
        self._byte_size = byte_size
        self._expected_sha256 = expected_sha256
        self._snapshot = snapshot
        self._remaining = byte_size
        self._hasher = hashlib.sha256()
        self._closed = False
        self._verified_complete = False
        self._lock = threading.RLock()

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def sha256(self) -> str:
        return self._expected_sha256

    @property
    def closed(self) -> bool:
        return self._closed

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"PinnedVerifiedReleaseArtifactStream(state={state!r})"

    def read(self, maximum_bytes: int) -> bytes:
        if type(maximum_bytes) is not int or not (
            1 <= maximum_bytes <= ARTIFACT_STREAM_MAX_READ_BYTES
        ):
            raise ReleaseArtifactStorageValidationError(
                "Release artifact stream read bound is invalid."
            )
        with self._lock:
            if self._closed:
                raise ReleaseArtifactStorageNotAvailableError(
                    "Release artifact stream is closed."
                )
            if self._verified_complete:
                return b""
            try:
                chunk = os.read(
                    self._descriptor,
                    min(maximum_bytes, self._remaining),
                )
                if not chunk:
                    raise ReleaseArtifactStorageIntegrityError(
                        "Release artifact stream ended early."
                    )
                self._remaining -= len(chunk)
                self._hasher.update(chunk)
                if self._remaining == 0:
                    extra = os.read(self._descriptor, 1)
                    after = os.fstat(self._descriptor)
                    if (
                        extra
                        or _file_snapshot(after) != self._snapshot
                        or not hmac.compare_digest(
                            self._hasher.hexdigest(),
                            self._expected_sha256,
                        )
                    ):
                        raise ReleaseArtifactStorageIntegrityError(
                            "Release artifact changed during streaming."
                        )
                    self._verified_complete = True
                return chunk
            except ReleaseArtifactStorageError:
                self.close()
                raise
            except OSError as exc:
                self.close()
                raise ReleaseArtifactStorageNotAvailableError(
                    "Release artifact stream is not available."
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            except OSError:
                pass

    def __enter__(self) -> PinnedVerifiedReleaseArtifactStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


class LocalReadOnlyReleaseArtifactStore:
    """Read exact bytes beneath one pinned, non-symlink local root.

    The adapter never creates, publishes, replaces, or deletes release files.
    A separate release pipeline must place already-approved objects under the
    root before their metadata is attached to a release.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        maximum_artifact_bytes: int = DEFAULT_LOCAL_ARTIFACT_READ_LIMIT,
    ) -> None:
        if (
            not hasattr(os, "geteuid")
            or not hasattr(os, "O_NOFOLLOW")
            or os.open not in getattr(os, "supports_dir_fd", set())
        ):
            raise ReleaseArtifactStorageNotAvailableError(
                "Secure local artifact reads are not available on this host."
            )
        root_path = Path(root).expanduser()
        if not root_path.is_absolute() or root_path == Path(os.sep):
            raise ReleaseArtifactStorageValidationError(
                "Release artifact root is invalid."
            )
        if type(maximum_artifact_bytes) is not int or not (
            1 <= maximum_artifact_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ReleaseArtifactStorageValidationError(
                "Release artifact size bound is invalid."
            )
        lexical = Path(os.path.abspath(os.fspath(root_path)))
        if os.path.islink(lexical):
            raise ReleaseArtifactStorageIntegrityError(
                "Release artifact root must not be a symbolic link."
            )
        self._root = lexical
        self._maximum_artifact_bytes = maximum_artifact_bytes
        self._lock = threading.RLock()
        descriptor = self._open_root()
        try:
            opened = os.fstat(descriptor)
            self._root_identity = (opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)

    @property
    def root(self) -> Path:
        return self._root

    def __repr__(self) -> str:
        return "LocalReadOnlyReleaseArtifactStore(root=<private>)"

    def _open_root(self) -> int:
        try:
            descriptor = os.open(self._root, _directory_flags())
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_mode & 0o022
            ):
                raise ReleaseArtifactStorageIntegrityError(
                    "Release artifact root permissions are not trusted."
                )
            return descriptor
        except ReleaseArtifactStorageError:
            raise
        except OSError as exc:
            raise ReleaseArtifactStorageNotAvailableError(
                "Release artifact root is not available."
            ) from exc

    @staticmethod
    def _key_parts(storage_key: object) -> tuple[str, ...]:
        try:
            normalized = validate_storage_key(storage_key, field="storage_key")
        except ValueError as exc:
            raise ReleaseArtifactStorageValidationError(
                "Release artifact storage key is invalid."
            ) from exc
        if "\\" in normalized:
            raise ReleaseArtifactStorageValidationError(
                "Release artifact storage key is invalid."
            )
        parts = tuple(normalized.split("/"))
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ReleaseArtifactStorageValidationError(
                "Release artifact storage key is invalid."
            )
        return parts

    def open_verified_release_artifact(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_byte_size: int,
    ) -> PinnedVerifiedReleaseArtifactStream:
        parts = self._key_parts(storage_key)
        try:
            digest = validate_sha256(expected_sha256)
            byte_size = validate_byte_size(expected_byte_size)
        except ValueError as exc:
            raise ReleaseArtifactStorageValidationError(
                "Release artifact metadata is invalid."
            ) from exc
        if byte_size > self._maximum_artifact_bytes:
            raise ReleaseArtifactStorageValidationError(
                "Release artifact exceeds the configured read bound."
            )

        with self._lock:
            root_fd = self._open_root()
            current_fd = root_fd
            file_fd: int | None = None
            try:
                opened_root = os.fstat(root_fd)
                if (opened_root.st_dev, opened_root.st_ino) != self._root_identity:
                    raise ReleaseArtifactStorageIntegrityError(
                        "Release artifact root identity changed."
                    )
                for component in parts[:-1]:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
                    opened_directory = os.fstat(next_fd)
                    if (
                        not stat.S_ISDIR(opened_directory.st_mode)
                        or opened_directory.st_uid != os.geteuid()
                        or opened_directory.st_mode & 0o022
                    ):
                        os.close(next_fd)
                        raise ReleaseArtifactStorageIntegrityError(
                            "Release artifact directory is not trusted."
                        )
                    if current_fd != root_fd:
                        os.close(current_fd)
                    current_fd = next_fd
                file_fd = os.open(parts[-1], _file_flags(), dir_fd=current_fd)
                before = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.geteuid()
                    or before.st_nlink != 1
                    or before.st_mode & 0o022
                    or before.st_size != byte_size
                ):
                    raise ReleaseArtifactStorageIntegrityError(
                        "Release artifact file is not trusted."
                    )
                hasher = hashlib.sha256()
                remaining = byte_size
                while remaining:
                    chunk = os.read(
                        file_fd,
                        min(ARTIFACT_VERIFY_CHUNK_BYTES, remaining),
                    )
                    if not chunk:
                        raise ReleaseArtifactStorageIntegrityError(
                            "Release artifact read was incomplete."
                        )
                    hasher.update(chunk)
                    remaining -= len(chunk)
                if os.read(file_fd, 1):
                    raise ReleaseArtifactStorageIntegrityError(
                        "Release artifact grew during verification."
                    )
                after = os.fstat(file_fd)
                if (
                    _file_snapshot(before) != _file_snapshot(after)
                    or not hmac.compare_digest(hasher.hexdigest(), digest)
                ):
                    raise ReleaseArtifactStorageIntegrityError(
                        "Release artifact integrity verification failed."
                    )
                os.lseek(file_fd, 0, os.SEEK_SET)
                stream = PinnedVerifiedReleaseArtifactStream(
                    file_fd,
                    byte_size=byte_size,
                    expected_sha256=digest,
                    snapshot=_file_snapshot(after),
                )
                file_fd = None
                return stream
            except ReleaseArtifactStorageError:
                raise
            except OSError as exc:
                raise ReleaseArtifactStorageNotAvailableError(
                    "Release artifact is not available."
                ) from exc
            finally:
                if file_fd is not None:
                    try:
                        os.close(file_fd)
                    except OSError:
                        pass
                if current_fd != root_fd:
                    try:
                        os.close(current_fd)
                    except OSError:
                        pass
                try:
                    os.close(root_fd)
                except OSError:
                    pass


__all__ = [
    "ARTIFACT_STREAM_MAX_READ_BYTES",
    "ARTIFACT_VERIFY_CHUNK_BYTES",
    "DEFAULT_LOCAL_ARTIFACT_READ_LIMIT",
    "LocalReadOnlyReleaseArtifactStore",
    "PinnedVerifiedReleaseArtifactStream",
    "ReleaseArtifactStorageError",
    "ReleaseArtifactStorageIntegrityError",
    "ReleaseArtifactStorageNotAvailableError",
    "ReleaseArtifactStorageValidationError",
]

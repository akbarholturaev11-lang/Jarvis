"""Small cross-platform advisory file lock for single-writer local operations."""

from __future__ import annotations

import os
import platform
import stat
import errno
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final, Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS: Final = 5.0
_POLL_SECONDS: Final = 0.05


class InterProcessLockError(RuntimeError):
    """Base class for fixed, non-sensitive lock errors."""


class InterProcessLockNotAvailable(InterProcessLockError):
    """The current platform cannot provide the required lock semantics."""


class InterProcessLockTimeout(InterProcessLockError):
    """Another process held the lock beyond the bounded wait."""


class InterProcessFileLock:
    """Advisory lock held by an open descriptor; the lock file is never deleted."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        system: str | None = None,
    ) -> None:
        lock_path = Path(path).expanduser()
        if not lock_path.is_absolute():
            raise ValueError("inter-process lock path must be absolute")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 30
        ):
            raise ValueError("inter-process lock timeout is invalid")
        detected = platform.system() if system is None else system
        self._system = detected.strip().casefold() if isinstance(detected, str) else ""
        # macOS exposes /var and /tmp as root-owned compatibility symlinks to
        # /private. Normalize only these fixed system aliases; arbitrary caller
        # symlinks still fail the component-by-component O_NOFOLLOW walk.
        if self._system in {"darwin", "macos"} and len(lock_path.parts) > 1:
            if lock_path.parts[1] in {"var", "tmp"}:
                lock_path = Path("/private").joinpath(*lock_path.parts[1:])
        self._path = lock_path
        self._timeout = float(timeout_seconds)

    @property
    def path(self) -> Path:
        return self._path

    def __repr__(self) -> str:
        return "InterProcessFileLock(path=<local>, state=<idle>)"

    @contextmanager
    def acquire(self) -> Iterator[None]:
        """Acquire within a bounded wait and release on every exit path."""

        if self._system not in {"darwin", "macos", "linux", "windows"}:
            raise InterProcessLockNotAvailable(
                "Inter-process locking is not available on this platform."
            )
        try:
            if os.name == "posix":
                descriptor = self._open_posix_descriptor()
            else:
                self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self._path.parent.is_symlink() or self._path.is_symlink():
                    raise InterProcessLockNotAvailable(
                        "Inter-process lock path is not available."
                    )
                descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except InterProcessLockError:
            raise
        except OSError as exc:
            raise InterProcessLockNotAvailable(
                "Inter-process lock path is not available."
            ) from exc

        locked = False
        try:
            # Re-check the opened object, not only the pathname.  This closes
            # the common symlink/hard-link substitution window between the
            # preflight checks and ``open``.
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise InterProcessLockNotAvailable(
                    "Inter-process lock path is not available."
                )
            if os.name != "nt" and opened.st_uid != os.getuid():
                raise InterProcessLockNotAvailable(
                    "Inter-process lock path is not available."
                )
            if os.name != "nt":
                with suppress(OSError):
                    os.fchmod(descriptor, 0o600)
            deadline = time.monotonic() + self._timeout
            while True:
                try:
                    self._try_lock(descriptor)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise InterProcessLockTimeout(
                            "Inter-process lock acquisition timed out."
                        )
                    time.sleep(_POLL_SECONDS)
            yield
        finally:
            if locked:
                with suppress(OSError):
                    self._unlock(descriptor)
            with suppress(OSError):
                os.close(descriptor)

    def _open_posix_descriptor(self) -> int:
        """Walk every component by dir-fd and open the final file no-follow."""

        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required) or os.open not in os.supports_dir_fd:
            raise InterProcessLockNotAvailable(
                "Secure inter-process lock paths are not available."
            )
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        current = os.open("/", directory_flags)
        try:
            for component in self._path.parent.parts[1:]:
                if component in {"", ".", ".."}:
                    raise InterProcessLockNotAvailable(
                        "Inter-process lock path is not available."
                    )
                try:
                    child = os.open(component, directory_flags, dir_fd=current)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(component, directory_flags, dir_fd=current)
                opened_directory = os.fstat(child)
                if not stat.S_ISDIR(opened_directory.st_mode):
                    os.close(child)
                    raise InterProcessLockNotAvailable(
                        "Inter-process lock path is not available."
                    )
                os.close(current)
                current = child

            descriptor = os.open(
                self._path.name,
                descriptor_flags,
                0o600,
                dir_fd=current,
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
            ):
                os.close(descriptor)
                raise InterProcessLockNotAvailable(
                    "Inter-process lock path is not available."
                )
            return descriptor
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                raise InterProcessLockNotAvailable(
                    "Inter-process lock path is not available."
                ) from exc
            raise
        finally:
            with suppress(OSError):
                os.close(current)

    def _try_lock(self, descriptor: int) -> None:
        if self._system in {"darwin", "macos", "linux"}:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                raise
            except (ImportError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno in {11, 13, 35}:
                    raise BlockingIOError from exc
                raise InterProcessLockNotAvailable(
                    "POSIX file locking is not available."
                ) from exc
        if self._system == "windows":
            try:
                import msvcrt

                if os.fstat(descriptor).st_size < 1:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) in {33, 36} or exc.errno in {
                    11,
                    13,
                }:
                    raise BlockingIOError from exc
                raise InterProcessLockNotAvailable(
                    "Windows file locking is not available."
                ) from exc
            except ImportError as exc:
                raise InterProcessLockNotAvailable(
                    "Windows file locking is not available."
                ) from exc
        raise InterProcessLockNotAvailable(
            "Inter-process locking is not available on this platform."
        )

    def _unlock(self, descriptor: int) -> None:
        if self._system in {"darwin", "macos", "linux"}:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        if self._system == "windows":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "InterProcessFileLock",
    "InterProcessLockError",
    "InterProcessLockNotAvailable",
    "InterProcessLockTimeout",
]

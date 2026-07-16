"""Shared helpers for the cross-platform ops tooling.

Secret output uses create-only, owner-only files by default.  On POSIX, every
parent directory is opened one component at a time with ``O_NOFOLLOW`` and all
publication happens relative to that already-open directory.  This prevents a
symlink swap from redirecting a write outside the requested tree.  Existing
hard-linked destinations are rejected rather than replaced.

Windows mode bits cannot prove an owner-only ACL.  Mutating helpers therefore
return honest ``not_available`` errors on non-POSIX hosts until a native ACL and
no-follow implementation exists; they never write first and call it success.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

POSIX: Final = os.name == "posix"
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_COPY_CHUNK: Final = 1024 * 1024

class UnsafePathError(OSError):
    """A path is a symlink, special file, hard link, or unsafe parent chain."""


class OpsNotAvailableError(RuntimeError):
    """Secure ops primitives are unavailable on the current platform."""


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """Verified owner-only result on a supported platform."""

    applied: bool
    status: str  # successful public calls return only "applied"
    note: str


@dataclass(frozen=True, slots=True)
class StableFileResult:
    """Digest and byte count from a no-follow, identity-stable file read."""

    sha256: str
    byte_size: int


def require_permission_applied(
    result: PermissionResult,
    *,
    label: str,
) -> PermissionResult:
    """Fail closed unless a permission operation was positively verified."""

    if not result.applied or result.status != "applied":
        raise PermissionError(f"owner-only permission was not applied: {label}")
    return result


def _canonical_system_alias(path: Path) -> Path:
    """Normalize only macOS's fixed ``/var`` and ``/tmp`` system aliases.

    User-controlled symlinks are deliberately not resolved.  macOS exposes
    ``/var`` and ``/tmp`` as stable aliases into ``/private``; normalizing these
    two aliases lets no-follow traversal work for ``tempfile`` paths without
    weakening the custom-parent symlink rule.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    if sys.platform != "darwin" or len(absolute.parts) < 2:
        return absolute
    first = absolute.parts[1]
    expected = {
        "etc": "private/etc",
        "var": "private/var",
        "tmp": "private/tmp",
    }.get(first)
    if expected is None:
        return absolute
    alias = Path("/") / first
    try:
        if not alias.is_symlink() or os.readlink(alias) != expected:
            return absolute
    except OSError:
        return absolute
    return Path("/private") / Path(*absolute.parts[1:])


def canonical_safe_path(path: Path) -> Path:
    """Canonicalize only fixed OS aliases, never user-controlled symlinks."""

    return _canonical_system_alias(Path(path))


def require_secure_ops_platform() -> None:
    """Fail before mutation where native no-follow/owner-only guarantees lack."""

    if not POSIX:
        raise OpsNotAvailableError(
            "secure ops tooling is not_available on this platform; "
            "a native ACL/no-follow implementation is required"
        )


def _repository_output_roots() -> tuple[Path, ...]:
    code_root = Path(__file__).resolve().parents[1]
    roots = [code_root]
    for candidate in code_root.parents:
        if (candidate / ".git").exists():
            roots.append(candidate)
    return tuple(dict.fromkeys(canonical_safe_path(root) for root in roots))


def _resolved_existing_ancestor(path: Path) -> Path:
    """Resolve the nearest existing ancestor for rejection-only checks.

    Output primitives still traverse the requested path with ``O_NOFOLLOW``.
    Resolution is intentionally limited to this deny-list check so filesystem
    aliases, including case-insensitive macOS spellings and symlinks that point
    into the repository, cannot bypass the repository boundary.
    """

    candidate = Path(path)
    while True:
        try:
            return candidate.resolve(strict=True)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise UnsafePathError("ops output has no existing filesystem ancestor")
            candidate = parent
        except (OSError, RuntimeError) as exc:
            raise UnsafePathError("ops output path could not be safely resolved") from exc


def reject_repository_output_path(path: Path) -> Path:
    """Reject generated secrets/state anywhere inside this repository/worktree."""

    normalized = canonical_safe_path(path)
    existing = _resolved_existing_ancestor(normalized)
    try:
        ancestry_identities: set[tuple[int, int]] = set()
        for entry in (existing, *existing.parents):
            info = entry.stat()
            ancestry_identities.add((info.st_dev, info.st_ino))
    except OSError as exc:
        raise UnsafePathError("ops output ancestry could not be verified") from exc
    for root in _repository_output_roots():
        try:
            root_info = root.stat()
        except OSError as exc:
            raise UnsafePathError("repository output boundary is unavailable") from exc
        if (root_info.st_dev, root_info.st_ino) in ancestry_identities:
            raise UnsafePathError("ops output inside the repository is forbidden")
    return normalized


def _is_protected_system_directory(path: Path) -> bool:
    filesystem_root = Path(path.anchor)
    return (
        path == filesystem_root
        or path.parent == filesystem_root
        or path
        in {
            Path("/private/etc"),
            Path("/private/tmp"),
            Path("/private/var"),
        }
    )


def _open_directory(path: Path, *, create: bool) -> tuple[int, Path]:
    """Open a directory chain without following any non-system symlink."""

    normalized = _canonical_system_alias(Path(path))
    if not POSIX:
        current = Path(normalized.anchor)
        for component in normalized.parts[1:]:
            if component in {"", ".", ".."}:
                raise UnsafePathError(
                    f"unsafe directory component: {component!r}"
                )
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=0o700)
                info = current.lstat()
            is_junction = bool(
                getattr(current, "is_junction", lambda: False)()
            )
            if (
                not stat.S_ISDIR(info.st_mode)
                or current.is_symlink()
                or is_junction
            ):
                raise UnsafePathError(f"unsafe directory: {current}")
        return -1, normalized

    flags = os.O_RDONLY | _DIRECTORY | _CLOEXEC | _NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in normalized.parts[1:]:
            if component in {"", ".", ".."}:
                raise UnsafePathError(f"unsafe directory component: {component!r}")
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    # A racing creator is accepted only if the no-follow open
                    # below proves it created a real directory.
                    pass
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafePathError(
                        f"symlink or non-directory parent rejected: {normalized}"
                    ) from exc
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, normalized
    except Exception:
        os.close(descriptor)
        raise


def validate_directory(path: Path) -> Path:
    """Return a normalized safe directory path without changing permissions."""

    descriptor, normalized = _open_directory(Path(path), create=False)
    if descriptor >= 0:
        os.close(descriptor)
    return normalized


def ensure_private_directory(path: Path, *, mode: int = 0o700) -> PermissionResult:
    """Create/open a directory without symlinks and make it owner-only on POSIX."""

    require_secure_ops_platform()
    descriptor, normalized = _open_directory(Path(path), create=True)
    if _is_protected_system_directory(normalized):
        if descriptor >= 0:
            os.close(descriptor)
        raise UnsafePathError(
            "refusing to change permissions on a filesystem/system root directory"
        )
    try:
        info = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise UnsafePathError(
            "private output directory is not owned by the effective user"
        )
    try:
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError as exc:
        raise PermissionError("directory hardening failed") from exc
    finally:
        os.close(descriptor)
    return PermissionResult(True, "applied", f"mode set to {oct(mode)}")


def validate_private_directory(path: Path) -> Path:
    """Validate a private, effective-user-owned directory for atomic publish."""

    descriptor, normalized = _open_directory(Path(path), create=False)
    if _is_protected_system_directory(normalized):
        if descriptor >= 0:
            os.close(descriptor)
        raise UnsafePathError("system directory is not a private publish boundary")
    if not POSIX:
        return normalized
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise UnsafePathError(
                "publish directory must be owner-only"
            )
    finally:
        os.close(descriptor)
    return normalized


def fsync_directory(path: Path) -> None:
    """Durably flush a no-follow directory on POSIX; best effort elsewhere."""

    require_secure_ops_platform()
    descriptor, _ = _open_directory(Path(path), create=False)
    if descriptor < 0:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_private_directory(staged: Path, destination: Path) -> None:
    """Atomically rename one complete sibling tree to a fresh destination."""

    require_secure_ops_platform()
    staged = canonical_safe_path(staged)
    destination = reject_repository_output_path(destination)
    if staged.parent != destination.parent:
        raise UnsafePathError("staging and destination must be siblings")
    if staged.name in {"", ".", ".."} or destination.name in {"", ".", ".."}:
        raise UnsafePathError("unsafe directory publication name")
    parent = validate_private_directory(destination.parent)

    if not POSIX:
        staged_info = staged.lstat()
        if not stat.S_ISDIR(staged_info.st_mode) or staged.is_symlink():
            raise UnsafePathError("staging path is not a safe directory")
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(destination)
        os.rename(staged, destination)
        return

    parent_fd, _ = _open_directory(parent, create=False)
    try:
        staged_info = _stat_at(parent_fd, staged.name)
        if (
            staged_info is None
            or not stat.S_ISDIR(staged_info.st_mode)
            or staged_info.st_uid != os.geteuid()
            or staged_info.st_mode & 0o077
        ):
            raise UnsafePathError("staging path is not an owner-only directory")
        if _stat_at(parent_fd, destination.name) is not None:
            raise FileExistsError(destination)
        # The parent is owner-controlled and not writable by group/other, so no
        # untrusted process can create the destination between this check and
        # rename.  rename itself is the single filesystem publication point.
        os.rename(
            staged.name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        published = _stat_at(parent_fd, destination.name)
        if published is None or (published.st_dev, published.st_ino) != (
            staged_info.st_dev,
            staged_info.st_ino,
        ):
            raise UnsafePathError("published directory identity could not be verified")
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_regular(info: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"{label} is not a regular file")
    if info.st_nlink != 1:
        raise UnsafePathError(f"{label} has multiple hard links")


def _validate_owned_destination(info: os.stat_result, *, label: str) -> None:
    _validate_regular(info, label=label)
    if POSIX and info.st_uid != os.geteuid():
        raise UnsafePathError(f"{label} is not owned by the effective user")


def validate_write_target(path: Path, *, allow_existing: bool = False) -> bool:
    """Prevalidate a destination; return whether a safe file already exists."""

    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise UnsafePathError("unsafe destination name")
    if not POSIX:
        parent = _canonical_system_alias(path).parent
        validate_directory(parent)
        try:
            info = _canonical_system_alias(path).lstat()
        except FileNotFoundError:
            return False
        _validate_owned_destination(info, label="destination")
        if not allow_existing:
            raise FileExistsError(path)
        return True

    parent_fd, normalized_parent = _open_directory(path.parent, create=True)
    try:
        info = _stat_at(parent_fd, path.name)
        if info is None:
            return False
        _validate_owned_destination(info, label="destination")
        if not allow_existing:
            raise FileExistsError(normalized_parent / path.name)
        return True
    finally:
        os.close(parent_fd)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing secret file")
        view = view[written:]


def _publish_posix_temp(
    parent_fd: int,
    *,
    temp_name: str,
    final_name: str,
    overwrite: bool,
) -> None:
    existing = _stat_at(parent_fd, final_name)
    if existing is not None:
        _validate_owned_destination(existing, label="destination")
        if not overwrite:
            raise FileExistsError(final_name)

    if overwrite:
        os.replace(
            temp_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    else:
        # link/unlink is the portable POSIX create-if-absent publication.  It
        # cannot overwrite a destination created by a racing process.
        os.link(
            temp_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(temp_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _new_temp_name(name: str) -> str:
    return f".{name}.{secrets.token_hex(12)}.tmp"


def harden_file(path: Path, *, mode: int = 0o600) -> PermissionResult:
    """Owner-only hardening that rejects symlinks, special files, and hard links."""

    require_secure_ops_platform()
    path = Path(path)
    try:
        parent_fd, _ = _open_directory(path.parent, create=False)
        try:
            info = _stat_at(parent_fd, path.name)
            if info is None:
                raise FileNotFoundError(path)
            _validate_regular(info, label="file")
            descriptor = os.open(
                path.name,
                os.O_RDONLY | _CLOEXEC | _NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                _validate_owned_destination(opened, label="file")
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise UnsafePathError("file identity changed before hardening")
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise PermissionError("file hardening failed") from exc
    return PermissionResult(True, "applied", f"mode set to {oct(mode)}")


def harden_directory(path: Path, *, mode: int = 0o700) -> PermissionResult:
    return ensure_private_directory(path, mode=mode)


def write_secret_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    overwrite: bool = False,
) -> PermissionResult:
    """Atomically publish secret bytes without following or replacing links.

    The default is create-only.  ``overwrite=True`` is available for controlled
    cutovers, but the existing destination must be one regular file with a single
    link.  Publication replaces the directory entry itself; it never truncates an
    existing inode.
    """

    require_secure_ops_platform()
    path = reject_repository_output_path(Path(path))
    if path.name in {"", ".", ".."}:
        raise UnsafePathError("unsafe destination name")

    parent_fd, _ = _open_directory(path.parent, create=True)
    temp_name = _new_temp_name(path.name)
    descriptor = -1
    try:
        # Validate before doing work so existing hard links fail explicitly.
        existing = _stat_at(parent_fd, path.name)
        if existing is not None:
            _validate_owned_destination(existing, label="destination")
            if not overwrite:
                raise FileExistsError(path)
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        _write_all(descriptor, data)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        _validate_owned_destination(info, label="temporary output")
        os.close(descriptor)
        descriptor = -1
        _publish_posix_temp(
            parent_fd,
            temp_name=temp_name,
            final_name=path.name,
            overwrite=overwrite,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)
    return PermissionResult(True, "applied", f"mode set to {oct(mode)}")


def write_secret_text(
    path: Path,
    text: str,
    *,
    mode: int = 0o600,
    overwrite: bool = False,
) -> PermissionResult:
    return write_secret_bytes(
        path,
        text.encode("utf-8"),
        mode=mode,
        overwrite=overwrite,
    )


def _open_stable_source(path: Path) -> tuple[int, int, os.stat_result]:
    path = Path(path)
    if not POSIX:
        normalized = _canonical_system_alias(path)
        _open_directory(normalized.parent, create=False)
        info = normalized.lstat()
        _validate_regular(info, label="source")
        descriptor = os.open(
            normalized,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        return descriptor, -1, info
    parent_fd, _ = _open_directory(path.parent, create=False)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | _CLOEXEC | _NOFOLLOW,
            dir_fd=parent_fd,
        )
    except Exception:
        os.close(parent_fd)
        raise
    info = os.fstat(descriptor)
    try:
        _validate_regular(info, label="source")
        current = _stat_at(parent_fd, path.name)
        if current is None or (current.st_dev, current.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise UnsafePathError("source identity changed while opening")
    except Exception:
        os.close(descriptor)
        os.close(parent_fd)
        raise
    return descriptor, parent_fd, info


def _source_unchanged(
    descriptor: int,
    parent_fd: int,
    name: str,
    before: os.stat_result,
) -> None:
    after = os.fstat(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise UnsafePathError("source changed while it was being read")
    if POSIX:
        current = _stat_at(parent_fd, name)
        if current is None or any(
            getattr(before, field) != getattr(current, field)
            for field in stable_fields
        ):
            raise UnsafePathError("source path changed while it was being read")
        _validate_regular(current, label="source")


def read_stable_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read one bounded regular file and prove its identity stayed unchanged."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    path = Path(path)
    descriptor, parent_fd, before = _open_stable_source(path)
    try:
        if before.st_size > max_bytes:
            raise ValueError(f"source exceeds the {max_bytes}-byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_COPY_CHUNK, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"source exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
        _source_unchanged(descriptor, parent_fd, path.name, before)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def hash_stable_file(path: Path, *, max_bytes: int) -> StableFileResult:
    """Hash a bounded file through a stable no-follow descriptor."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    path = Path(path)
    descriptor, parent_fd, before = _open_stable_source(path)
    digest = hashlib.sha256()
    total = 0
    try:
        if before.st_size > max_bytes:
            raise ValueError(f"source exceeds the {max_bytes}-byte limit")
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"source exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
        _source_unchanged(descriptor, parent_fd, path.name, before)
    finally:
        os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
    return StableFileResult(digest.hexdigest(), total)


def copy_stable_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    mode: int = 0o600,
    overwrite: bool = False,
) -> StableFileResult:
    """Stable-read, verify, then atomically publish a bounded regular file."""

    require_secure_ops_platform()
    source = Path(source)
    destination = Path(destination)
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    source_fd, source_parent_fd, before = _open_stable_source(source)
    try:
        destination_parent_fd, _ = _open_directory(destination.parent, create=True)
    except Exception:
        os.close(source_fd)
        os.close(source_parent_fd)
        raise
    temp_name = _new_temp_name(destination.name)
    output_fd = -1
    digest_obj = hashlib.sha256()
    total = 0
    try:
        existing = _stat_at(destination_parent_fd, destination.name)
        if existing is not None:
            _validate_owned_destination(existing, label="destination")
            if (existing.st_dev, existing.st_ino) == (before.st_dev, before.st_ino):
                raise UnsafePathError("source and destination are the same file")
            if not overwrite:
                raise FileExistsError(destination)

        output_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
            mode,
            dir_fd=destination_parent_fd,
        )
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"source exceeds the {max_bytes}-byte limit")
            _write_all(output_fd, chunk)
            digest_obj.update(chunk)
        _source_unchanged(source_fd, source_parent_fd, source.name, before)
        digest = digest_obj.hexdigest()
        if expected_size is not None and total != expected_size:
            raise ValueError("source size does not match its declared size")
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("source digest does not match its declared digest")

        os.fchmod(output_fd, mode)
        os.fsync(output_fd)
        _validate_owned_destination(os.fstat(output_fd), label="temporary output")
        os.close(output_fd)
        output_fd = -1
        _publish_posix_temp(
            destination_parent_fd,
            temp_name=temp_name,
            final_name=destination.name,
            overwrite=overwrite,
        )
        return StableFileResult(digest, total)
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        os.close(source_fd)
        os.close(source_parent_fd)
        try:
            os.unlink(temp_name, dir_fd=destination_parent_fd)
        except FileNotFoundError:
            pass
        os.close(destination_parent_fd)


def file_is_owner_only(path: Path) -> bool:
    """True only on POSIX for one regular, non-symlink, single-link file."""

    if not POSIX:
        return False
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and not (info.st_mode & 0o077)
    )


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def emit(message: str) -> None:
    print(message)


__all__ = [
    "POSIX",
    "PermissionResult",
    "StableFileResult",
    "OpsNotAvailableError",
    "UnsafePathError",
    "canonical_safe_path",
    "copy_stable_file",
    "emit",
    "ensure_private_directory",
    "eprint",
    "file_is_owner_only",
    "fsync_directory",
    "harden_directory",
    "harden_file",
    "hash_stable_file",
    "read_stable_bytes",
    "publish_private_directory",
    "reject_repository_output_path",
    "require_permission_applied",
    "require_secure_ops_platform",
    "validate_directory",
    "validate_private_directory",
    "validate_write_target",
    "write_secret_bytes",
    "write_secret_text",
]

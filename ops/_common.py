"""Shared helpers for the cross-platform ops tooling.

Owner-only file hardening uses POSIX mode bits when available and returns an
honest ``manual`` status on platforms (Windows) where those bits do not express
owner-only access, rather than silently claiming a hardened file.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

POSIX: Final = os.name == "posix"

# Windows NTFS ACL guidance shown when POSIX mode bits are unavailable.
_WINDOWS_ACL_HINT: Final = (
    "Restrict this file to the owner with an NTFS ACL, e.g. "
    "icacls <file> /inheritance:r /grant:r %USERNAME%:F"
)


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """Outcome of trying to make a path owner-only."""

    applied: bool
    status: str  # "applied", "manual", or "failed"
    note: str


def harden_file(path: Path, *, mode: int = 0o600) -> PermissionResult:
    """Best-effort owner-only hardening; honest status on non-POSIX hosts."""

    if not POSIX:
        return PermissionResult(False, "manual", _WINDOWS_ACL_HINT)
    try:
        os.chmod(path, mode)
    except OSError as exc:
        return PermissionResult(False, "failed", f"chmod failed: {exc}")
    return PermissionResult(True, "applied", f"mode set to {oct(mode)}")


def harden_directory(path: Path, *, mode: int = 0o700) -> PermissionResult:
    return harden_file(path, mode=mode)


def write_secret_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> PermissionResult:
    """Atomically write secret bytes, applying owner-only mode where supported."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if POSIX:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(path, flags, mode)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        return harden_file(path, mode=mode)
    path.write_bytes(data)
    return PermissionResult(False, "manual", _WINDOWS_ACL_HINT)


def write_secret_text(path: Path, text: str, *, mode: int = 0o600) -> PermissionResult:
    return write_secret_bytes(path, text.encode("utf-8"), mode=mode)


def file_is_owner_only(path: Path) -> bool:
    """True only on POSIX when the file exists and has no group/other access."""

    if not POSIX:
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not (info.st_mode & 0o077)


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def emit(message: str) -> None:
    print(message)


__all__ = [
    "POSIX",
    "PermissionResult",
    "emit",
    "eprint",
    "file_is_owner_only",
    "harden_directory",
    "harden_file",
    "write_secret_bytes",
    "write_secret_text",
]

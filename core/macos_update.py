"""Fail-closed macOS application update primitives.

The customer runtime never selects the development adapter in this module.
``MacOSDevelopmentUpdaterAdapter`` exists solely for local integration tests and
must be constructed explicitly with ``development_mode=True`` and a health
launcher.  The production adapter remains in :mod:`core.update_transaction` and
uses :func:`assess_production_macos_helper` only as a read-only trust check.

The accepted package format is ``jarvis_macos_app_zip_v1``: one top-level
``.app`` bundle, regular files/directories, safe relative in-bundle framework
links, and no path aliases.  Archive bytes have already passed release-manifest
signature, size and SHA-256 checks before they reach this layer; this module
independently copies those pinned bytes before parsing them.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion, SemanticVersion
from core.update_transaction import (
    AdapterBackupResult,
    AdapterMutationResult,
    AdapterStatus,
    AdapterVerificationResult,
    UpdaterCapability,
    UpdaterPlatformAdapter,
    VerifiedArtifactHandle,
)


MACOS_APP_ZIP_FORMAT: Final = "jarvis_macos_app_zip_v1"
PRODUCTION_MACOS_HELPER_PATH: Final = Path(
    "/Library/PrivilegedHelperTools/com.jarvis.assistant.updater"
)

_INFO_PLIST_MAX_BYTES: Final = 1024 * 1024
_HEALTH_DOCUMENT_MAX_BYTES: Final = 4096
_BACKUP_METADATA_MAX_BYTES: Final = 4096
_HEALTH_REQUEST_SCHEMA: Final = "jarvis.update-health-request.v1"
_HEALTH_RESPONSE_SCHEMA: Final = "jarvis.update-health-response.v1"
_BACKUP_SCHEMA: Final = "jarvis.macos-update-backup.v1"
_TEAM_ID_RE: Final = re.compile(r"[A-Z0-9]{10}")
_NONCE_RE: Final = re.compile(r"[0-9a-f]{64}")


class MacOSArchiveError(ValueError):
    """The candidate archive is not the strict JARVIS macOS ZIP format."""


@dataclass(frozen=True, slots=True)
class MacOSArchiveLimits:
    max_entries: int = 8192
    max_member_bytes: int = 512 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: int = 200

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_member_bytes",
            "max_expanded_bytes",
            "max_compression_ratio",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_member_bytes > self.max_expanded_bytes:
            raise ValueError("member limit cannot exceed the expanded limit")


@dataclass(frozen=True, slots=True)
class ExtractedMacOSApp:
    app_path: Path
    identity: ProductVersion
    bundle_id: str


@dataclass(frozen=True, slots=True)
class HelperCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:
            raise TypeError("helper command return code must be an integer")
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise TypeError("helper command output must be text")


class HelperValidationRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        timeout_seconds: float,
    ) -> HelperCommandResult: ...


@dataclass(frozen=True, slots=True)
class MacOSHelperAssessment:
    trusted: bool
    blocker: str | None

    def __post_init__(self) -> None:
        if type(self.trusted) is not bool:
            raise TypeError("helper trust status must be boolean")
        if self.trusted == (self.blocker is not None):
            raise ValueError("helper assessment blocker is inconsistent")


class HealthLauncher(Protocol):
    def __call__(
        self,
        app_path: Path,
        request_path: Path,
        response_path: Path,
    ) -> None: ...


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise OSError("private directory path is invalid")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    opened = path.stat()
    if not stat.S_ISDIR(opened.st_mode):
        raise OSError("private path is not a directory")
    if hasattr(os, "getuid") and opened.st_uid != os.getuid():
        raise OSError("private directory owner mismatch")
    if os.name != "nt":
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise OSError("private directory permissions are unsafe")
    return path


def _read_bounded_regular_file(path: Path, maximum: int, *, private: bool = False) -> bytes:
    if path.is_symlink():
        raise OSError("file is a symlink")
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum
            or (private and hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or (private and os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077)
        ):
            raise OSError("file metadata is unsafe")
        raw = os.read(descriptor, maximum + 1)
        if len(raw) != opened.st_size or os.read(descriptor, 1):
            raise OSError("file changed while it was read")
        return raw
    finally:
        os.close(descriptor)


def _atomic_private_json(path: Path, document: Mapping[str, object]) -> None:
    raw = json.dumps(
        dict(document),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not raw or len(raw) > _HEALTH_DOCUMENT_MAX_BYTES:
        raise ValueError("private update document is too large")
    _private_directory(path.parent, create=True)
    if path.is_symlink():
        raise OSError("private update document is a symlink")
    temporary = path.parent / f".{path.name}-{secrets.token_hex(16)}.tmp"
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
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _strict_json(path: Path, maximum: int, expected_fields: set[str]) -> dict[str, object]:
    raw = _read_bounded_regular_file(path, maximum, private=True)
    try:
        document = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("private update document is invalid") from exc
    if type(document) is not dict or set(document) != expected_fields:
        raise ValueError("private update document fields are invalid")
    canonical = json.dumps(
        document,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != raw:
        raise ValueError("private update document is not canonical")
    return document


def read_macos_app_identity(
    app_path: str | os.PathLike[str],
    *,
    expected_bundle_id: str = BUNDLE_ID,
) -> ProductVersion:
    """Read identity from a no-follow, regular ``Info.plist``."""

    app = Path(app_path)
    if not app.is_absolute() or app.is_symlink() or app.suffix.casefold() != ".app":
        raise MacOSArchiveError("application bundle path is invalid")
    for directory in (app, app / "Contents"):
        opened = directory.stat()
        if directory.is_symlink() or not stat.S_ISDIR(opened.st_mode):
            raise MacOSArchiveError("application bundle directory is invalid")
    plist_path = app / "Contents" / "Info.plist"
    try:
        raw = _read_bounded_regular_file(plist_path, _INFO_PLIST_MAX_BYTES)
        document = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise MacOSArchiveError("application Info.plist is invalid") from exc
    if type(document) is not dict:
        raise MacOSArchiveError("application Info.plist must be a dictionary")
    bundle_id = document.get("CFBundleIdentifier")
    version = document.get("CFBundleShortVersionString")
    build = document.get("CFBundleVersion")
    if bundle_id != expected_bundle_id or type(version) is not str or type(build) is not str:
        raise MacOSArchiveError("application identity does not match JARVIS")
    if not re.fullmatch(r"[1-9][0-9]*", build):
        raise MacOSArchiveError("application build identity is invalid")
    try:
        return ProductVersion(SemanticVersion.parse(version), int(build))
    except (TypeError, ValueError) as exc:
        raise MacOSArchiveError("application version identity is invalid") from exc


def _resolved_link_target(app: Path, link: Path) -> Path:
    try:
        raw_target = os.readlink(link)
    except OSError as exc:
        raise MacOSArchiveError("application link cannot be read") from exc
    if (
        not raw_target
        or "\x00" in raw_target
        or "\\" in raw_target
        or len(raw_target) > 1024
        or Path(raw_target).is_absolute()
    ):
        raise MacOSArchiveError("application link target is unsafe")
    try:
        app_root = app.resolve(strict=True)
        link_parent = link.parent.resolve(strict=True)
        target = (link.parent / raw_target).resolve(strict=True)
        target.relative_to(app_root)
        if target.is_dir() and (target == link_parent or target in link_parent.parents):
            raise ValueError("application link introduces a directory cycle")
    except (OSError, RuntimeError, ValueError) as exc:
        raise MacOSArchiveError("application link escapes the bundle or is dangling") from exc
    return target


def _validate_regular_app_tree(app: Path) -> None:
    """Accept only directories, regular files and resolved in-bundle links."""

    root = app.lstat()
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
        raise MacOSArchiveError("application bundle is not a directory")
    for current, directories, files in os.walk(app, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            opened = child.lstat()
            if stat.S_ISLNK(opened.st_mode):
                _resolved_link_target(app, child)
            elif not stat.S_ISDIR(opened.st_mode):
                raise MacOSArchiveError("application contains an unsafe directory")
        for name in files:
            child = current_path / name
            opened = child.lstat()
            if stat.S_ISLNK(opened.st_mode):
                _resolved_link_target(app, child)
            elif not stat.S_ISREG(opened.st_mode):
                raise MacOSArchiveError("application contains a non-regular file")


def _app_tree_digest(app: Path) -> str:
    """Hash paths, safe link targets, executable bits and regular-file bytes."""

    _validate_regular_app_tree(app)
    digest = hashlib.sha256(b"jarvis-macos-app-tree-v1\x00")

    def frame(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.encode("utf-8"))
        for entry in entries:
            path = directory / entry.name
            relative = path.relative_to(app).as_posix().encode("utf-8")
            opened = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(opened.st_mode):
                frame(b"link")
                frame(relative)
                frame(os.readlink(path).encode("utf-8"))
                continue
            if stat.S_ISDIR(opened.st_mode):
                frame(b"directory")
                frame(relative)
                visit(path)
                continue
            if not stat.S_ISREG(opened.st_mode):
                raise MacOSArchiveError("application contains a special file")
            frame(b"file")
            frame(relative)
            frame(b"executable" if opened.st_mode & 0o111 else b"non-executable")
            descriptor = os.open(
                path,
                os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
            )
            try:
                pinned = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(pinned.st_mode)
                    or (pinned.st_dev, pinned.st_ino, pinned.st_size)
                    != (opened.st_dev, opened.st_ino, opened.st_size)
                ):
                    raise MacOSArchiveError("application changed while it was hashed")
                frame(pinned.st_size.to_bytes(8, "big"))
                remaining = pinned.st_size
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 256 * 1024))
                    if not chunk:
                        raise MacOSArchiveError("application changed while it was hashed")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise MacOSArchiveError("application changed while it was hashed")
            finally:
                os.close(descriptor)

    visit(app)
    return digest.hexdigest()


def _archive_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name or "\\" in name or len(name) > 1024:
        raise MacOSArchiveError("archive entry name is invalid")
    if name.startswith("/"):
        raise MacOSArchiveError("archive entry is absolute")
    stripped = name[:-1] if name.endswith("/") else name
    raw_parts = stripped.split("/")
    path = PurePosixPath(stripped)
    parts = tuple(raw_parts)
    if (
        not parts
        or path.is_absolute()
        or any(
            part in {"", ".", ".."}
            or part.rstrip(" .") != part
            or ":" in part
            or unicodedata.normalize("NFC", part) != part
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise MacOSArchiveError("archive entry path is unsafe")
    return tuple(parts)


def _validated_archive_entries(
    archive: zipfile.ZipFile,
    limits: MacOSArchiveLimits,
) -> tuple[list[tuple[zipfile.ZipInfo, tuple[str, ...], str]], str]:
    entries = archive.infolist()
    if not entries or len(entries) > limits.max_entries:
        raise MacOSArchiveError("archive entry count is outside the limit")
    seen: set[str] = set()
    expanded = 0
    app_components: set[tuple[str, ...]] = set()
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], str]] = []
    link_paths: list[tuple[str, ...]] = []
    for info in entries:
        if info.flag_bits & 0x1:
            raise MacOSArchiveError("encrypted archive entries are not accepted")
        if type(info.orig_filename) is not str:
            raise MacOSArchiveError("archive entry name is invalid")
        parts = _archive_parts(info.orig_filename)
        key = "/".join(parts).casefold()
        if key in seen:
            raise MacOSArchiveError("archive contains duplicate path aliases")
        seen.add(key)
        is_directory = info.is_dir() or info.filename.endswith("/")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type and file_type not in {stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK}:
            raise MacOSArchiveError("archive contains a special file")
        kind = (
            "symlink"
            if file_type == stat.S_IFLNK
            else ("directory" if is_directory else "file")
        )
        if kind == "symlink":
            if is_directory or not 1 <= info.file_size <= 1024:
                raise MacOSArchiveError("archive link metadata is invalid")
            link_paths.append(parts)
        if is_directory and file_type == stat.S_IFREG:
            raise MacOSArchiveError("archive directory metadata is inconsistent")
        if not is_directory and file_type == stat.S_IFDIR:
            raise MacOSArchiveError("archive file metadata is inconsistent")
        if info.file_size < 0 or info.compress_size < 0:
            raise MacOSArchiveError("archive member size is invalid")
        if info.file_size > limits.max_member_bytes:
            raise MacOSArchiveError("archive member exceeds the expanded limit")
        expanded += info.file_size
        if expanded > limits.max_expanded_bytes:
            raise MacOSArchiveError("archive exceeds the total expanded limit")
        if (
            info.file_size > 1024
            and info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio
        ):
            raise MacOSArchiveError("archive compression ratio is unsafe")
        for index, part in enumerate(parts):
            if part.casefold().endswith(".app"):
                app_components.add(parts[: index + 1])
        validated.append((info, parts, kind))
    if len(app_components) != 1:
        raise MacOSArchiveError("archive must contain exactly one application bundle")
    (app_parts,) = tuple(app_components)
    if len(app_parts) != 1 or not app_parts[0].casefold().endswith(".app"):
        raise MacOSArchiveError("application bundle must be the archive root")
    if any(parts[0] != app_parts[0] for _, parts, _ in validated):
        raise MacOSArchiveError("archive contains data outside the application bundle")
    link_prefixes = set(link_paths)
    for _, parts, _ in validated:
        if any(parts[:index] in link_prefixes for index in range(1, len(parts))):
            raise MacOSArchiveError("archive entry uses a link as an extraction parent")
    return validated, app_parts[0]


def _ensure_extract_directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts:
        current = current / part
        try:
            opened = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            opened = current.lstat()
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode):
            raise MacOSArchiveError("archive extraction parent is unsafe")
    return current


def _validated_link_target(link_parts: tuple[str, ...], raw: bytes) -> str:
    try:
        target = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise MacOSArchiveError("archive link target is not UTF-8") from exc
    if (
        not target
        or "\x00" in target
        or "\\" in target
        or len(target) > 1024
        or target.startswith("/")
    ):
        raise MacOSArchiveError("archive link target is unsafe")
    target_parts = tuple(target.split("/"))
    resolved = list(link_parts[:-1])
    for part in target_parts:
        if part == "":
            raise MacOSArchiveError("archive link target contains an empty component")
        if part == ".":
            continue
        if part == "..":
            if len(resolved) <= 1:
                raise MacOSArchiveError("archive link escapes the application bundle")
            resolved.pop()
            continue
        if (
            part.rstrip(" .") != part
            or ":" in part
            or unicodedata.normalize("NFC", part) != part
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
        ):
            raise MacOSArchiveError("archive link target is unsafe")
        resolved.append(part)
    if not resolved or resolved[0] != link_parts[0]:
        raise MacOSArchiveError("archive link escapes the application bundle")
    return target


def extract_macos_app_zip_v1(
    archive_descriptor: int,
    destination_root: str | os.PathLike[str],
    *,
    expected_bundle_id: str,
    expected_identity: ProductVersion,
    limits: MacOSArchiveLimits | None = None,
) -> ExtractedMacOSApp:
    """Extract and validate one strict macOS app bundle from an open file."""

    if type(archive_descriptor) is not int or archive_descriptor < 0:
        raise ValueError("archive descriptor is invalid")
    if not isinstance(expected_identity, ProductVersion):
        raise TypeError("expected identity must be a ProductVersion")
    selected_limits = limits or MacOSArchiveLimits()
    if not isinstance(selected_limits, MacOSArchiveLimits):
        raise TypeError("archive limits are invalid")
    root = Path(destination_root)
    _private_directory(root, create=True)
    if any(root.iterdir()):
        raise MacOSArchiveError("archive extraction destination must be empty")
    duplicate = os.dup(archive_descriptor)
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as raw_archive:
            duplicate = -1
            raw_archive.seek(0)
            try:
                archive = zipfile.ZipFile(raw_archive, mode="r")
            except (OSError, zipfile.BadZipFile) as exc:
                raise MacOSArchiveError("update artifact is not a valid ZIP") from exc
            with archive:
                entries, app_name = _validated_archive_entries(archive, selected_limits)
                written_total = 0
                pending_links: list[tuple[tuple[str, ...], str]] = []
                for info, parts, kind in entries:
                    if kind == "directory":
                        _ensure_extract_directory(root, parts)
                        continue
                    if kind == "symlink":
                        with archive.open(info, mode="r") as source:
                            raw_target = source.read(1025)
                            if source.read(1):
                                raise MacOSArchiveError("archive link target is too large")
                        if len(raw_target) != info.file_size:
                            raise MacOSArchiveError("archive link size is inconsistent")
                        written_total += len(raw_target)
                        if written_total > selected_limits.max_expanded_bytes:
                            raise MacOSArchiveError("archive expanded past its declared limit")
                        pending_links.append(
                            (parts, _validated_link_target(parts, raw_target))
                        )
                        continue
                    parent = _ensure_extract_directory(root, parts[:-1])
                    destination = parent / parts[-1]
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(destination, flags, 0o600)
                    try:
                        member_written = 0
                        with archive.open(info, mode="r") as source:
                            while True:
                                chunk = source.read(256 * 1024)
                                if not chunk:
                                    break
                                member_written += len(chunk)
                                written_total += len(chunk)
                                if (
                                    member_written > info.file_size
                                    or member_written > selected_limits.max_member_bytes
                                    or written_total > selected_limits.max_expanded_bytes
                                ):
                                    raise MacOSArchiveError(
                                        "archive expanded past its declared limit"
                                    )
                                view = memoryview(chunk)
                                while view:
                                    count = os.write(descriptor, view)
                                    if count <= 0:
                                        raise OSError("archive extraction write failed")
                                    view = view[count:]
                        if member_written != info.file_size:
                            raise MacOSArchiveError("archive member size changed during extraction")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    executable = bool(((info.external_attr >> 16) & 0o111))
                    if os.name != "nt":
                        destination.chmod(0o700 if executable else 0o600)
                for parts, target in pending_links:
                    parent = _ensure_extract_directory(root, parts[:-1])
                    destination = parent / parts[-1]
                    if destination.exists() or destination.is_symlink():
                        raise MacOSArchiveError("archive link destination already exists")
                    os.symlink(target, destination)
                expected_total = sum(info.file_size for info, _, _ in entries)
                if written_total != expected_total:
                    raise MacOSArchiveError("archive expanded size is inconsistent")
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if isinstance(exc, MacOSArchiveError):
            raise
        raise MacOSArchiveError("update archive extraction failed") from exc
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    app_path = root / app_name
    _validate_regular_app_tree(app_path)
    identity = read_macos_app_identity(app_path, expected_bundle_id=expected_bundle_id)
    if identity != expected_identity:
        raise MacOSArchiveError("candidate application identity does not match the release")
    for current, _, _ in os.walk(app_path, topdown=False, followlinks=False):
        _fsync_directory(Path(current))
    _fsync_directory(root)
    return ExtractedMacOSApp(app_path, identity, expected_bundle_id)


def _copy_regular_tree(source: Path, destination: Path) -> None:
    _validate_regular_app_tree(source)
    def copy_directory(source_directory: Path, destination_directory: Path) -> None:
        destination_directory.mkdir(mode=0o700)
        with os.scandir(source_directory) as entries:
            for entry in entries:
                source_path = source_directory / entry.name
                destination_path = destination_directory / entry.name
                source_metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(source_metadata.st_mode):
                    os.symlink(os.readlink(source_path), destination_path)
                    continue
                if stat.S_ISDIR(source_metadata.st_mode):
                    copy_directory(source_path, destination_path)
                    continue
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise OSError("application copy source is not regular")
                source_descriptor = os.open(
                    source_path,
                    os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
                )
                destination_descriptor: int | None = None
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    destination_descriptor = os.open(destination_path, flags, 0o600)
                    while True:
                        chunk = os.read(source_descriptor, 256 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            count = os.write(destination_descriptor, view)
                            if count <= 0:
                                raise OSError("application copy failed")
                            view = view[count:]
                    os.fsync(destination_descriptor)
                    if os.name != "nt" and stat.S_IMODE(source_metadata.st_mode) & 0o111:
                        destination_path.chmod(0o700)
                finally:
                    os.close(source_descriptor)
                    if destination_descriptor is not None:
                        os.close(destination_descriptor)
        _fsync_directory(destination_directory)

    copy_directory(source, destination)
    _validate_regular_app_tree(destination)
    for current, _, _ in os.walk(destination, topdown=False):
        _fsync_directory(Path(current))


def _default_helper_runner(
    argv: tuple[str, ...],
    timeout_seconds: float,
) -> HelperCommandResult:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return HelperCommandResult(127)
    return HelperCommandResult(
        completed.returncode,
        completed.stdout[:16384],
        completed.stderr[:16384],
    )


def assess_production_macos_helper(
    helper_path: Path,
    *,
    expected_team_id: str | None,
    designated_requirement: str | None,
    frozen: bool,
    runner: HelperValidationRunner | None = None,
    required_uid: int = 0,
) -> MacOSHelperAssessment:
    """Read-only Developer ID/Gatekeeper/notarization assessment.

    No signing credentials are accepted or invoked.  All commands are fixed,
    argv-only validation commands and their output is never logged here.
    """

    if type(frozen) is not bool or not frozen:
        return MacOSHelperAssessment(False, "frozen_runtime_required")
    if type(expected_team_id) is not str or _TEAM_ID_RE.fullmatch(expected_team_id) is None:
        return MacOSHelperAssessment(False, "expected_team_id_not_configured")
    if (
        type(designated_requirement) is not str
        or not 8 <= len(designated_requirement) <= 2048
        or "\x00" in designated_requirement
        or "\n" in designated_requirement
        or "\r" in designated_requirement
    ):
        return MacOSHelperAssessment(False, "designated_requirement_not_configured")
    if not helper_path.is_absolute() or helper_path.is_symlink():
        return MacOSHelperAssessment(False, "helper_path_is_unsafe")
    try:
        opened = helper_path.lstat()
    except OSError:
        return MacOSHelperAssessment(False, "signed_helper_not_installed")
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != required_uid
        or stat.S_IMODE(opened.st_mode) & 0o022
        or not stat.S_IMODE(opened.st_mode) & 0o100
    ):
        return MacOSHelperAssessment(False, "helper_file_metadata_is_unsafe")
    try:
        parent = helper_path.parent.lstat()
    except OSError:
        return MacOSHelperAssessment(False, "helper_parent_is_unsafe")
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != required_uid
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        return MacOSHelperAssessment(False, "helper_parent_is_unsafe")
    pinned_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_uid,
        opened.st_nlink,
        stat.S_IMODE(opened.st_mode),
    )
    execute = runner or _default_helper_runner
    commands = (
        ("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(helper_path)),
        ("/usr/bin/codesign", "-dv", "--verbose=4", str(helper_path)),
        ("/usr/bin/codesign", "-dr", "-", str(helper_path)),
        ("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(helper_path)),
        ("/usr/bin/xcrun", "stapler", "validate", str(helper_path)),
    )
    results: list[HelperCommandResult] = []
    for command in commands:
        try:
            result = execute(command, 15.0)
        except Exception:
            return MacOSHelperAssessment(False, "helper_validation_command_failed")
        if not isinstance(result, HelperCommandResult) or result.returncode != 0:
            return MacOSHelperAssessment(False, "helper_signature_or_notarization_invalid")
        try:
            current = helper_path.lstat()
        except OSError:
            return MacOSHelperAssessment(False, "helper_changed_during_validation")
        if (
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_nlink,
            stat.S_IMODE(current.st_mode),
        ) != pinned_identity:
            return MacOSHelperAssessment(False, "helper_changed_during_validation")
        results.append(result)
    identity_output = (results[1].stdout + "\n" + results[1].stderr)[:32768]
    requirement_output = (results[2].stdout + "\n" + results[2].stderr)[:32768]
    team_matches = re.findall(r"(?:^|\n)TeamIdentifier=([A-Z0-9]{10})(?:\n|$)", identity_output)
    requirement_matches = re.findall(
        r"(?:^|\n)designated => ([^\r\n]+)(?:\n|$)",
        requirement_output,
    )
    if team_matches != [expected_team_id]:
        return MacOSHelperAssessment(False, "helper_team_id_mismatch")
    if requirement_matches != [designated_requirement]:
        return MacOSHelperAssessment(False, "helper_designated_requirement_mismatch")
    return MacOSHelperAssessment(True, None)


class FileNonceHealthProbe:
    """Bounded exact-identity health handshake with a fresh nonce."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        launcher: HealthLauncher,
        *,
        poll_interval_seconds: float = 0.02,
    ) -> None:
        supplied = Path(root).expanduser()
        if not supplied.is_absolute() or supplied.is_symlink():
            raise ValueError("health marker root must be absolute and non-symlink")
        selected = supplied.resolve(strict=False)
        if not callable(launcher):
            raise TypeError("health launcher must be callable")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not 0 < float(poll_interval_seconds) <= 1
        ):
            raise ValueError("health poll interval is invalid")
        self._root = selected
        self._launcher = launcher
        self._poll_interval = float(poll_interval_seconds)

    @property
    def root(self) -> Path:
        return self._root

    def _paths(self, nonce: str) -> tuple[Path, Path]:
        if _NONCE_RE.fullmatch(nonce) is None:
            raise ValueError("health nonce is invalid")
        return (
            self._root / f"request-{nonce}.json",
            self._root / f"response-{nonce}.json",
        )

    def arm(self, expected: ProductVersion) -> str:
        if not isinstance(expected, ProductVersion):
            raise TypeError("health identity must be a ProductVersion")
        _private_directory(self._root, create=True)
        self.cancel()
        nonce = secrets.token_hex(32)
        request_path, response_path = self._paths(nonce)
        response_path.unlink(missing_ok=True)
        _atomic_private_json(
            request_path,
            {
                "schema": _HEALTH_REQUEST_SCHEMA,
                "product_id": PRODUCT_ID,
                "bundle_id": BUNDLE_ID,
                "nonce": nonce,
                "version": str(expected.version),
                "build": expected.build,
            },
        )
        return nonce

    def cancel(self, nonce: str | None = None) -> None:
        if not self._root.exists() or self._root.is_symlink():
            return
        if nonce is not None:
            paths = self._paths(nonce)
        else:
            paths = tuple(
                path
                for path in self._root.iterdir()
                if re.fullmatch(r"(?:request|response)-[0-9a-f]{64}\.json", path.name)
            )
        for path in paths:
            path.unlink(missing_ok=True)
        if self._root.exists() and not self._root.is_symlink():
            _fsync_directory(self._root)

    def verify(
        self,
        app_path: Path,
        expected: ProductVersion,
        nonce: str,
        timeout_seconds: float,
    ) -> bool:
        if (
            _NONCE_RE.fullmatch(nonce) is None
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60
        ):
            return False
        deadline = time.monotonic() + float(timeout_seconds)
        request_path, response_path = self._paths(nonce)
        launcher_done = threading.Event()
        launcher_expired = threading.Event()

        def invoke_launcher() -> None:
            try:
                self._launcher(app_path, request_path, response_path)
            except Exception:
                pass
            finally:
                launcher_done.set()
                if launcher_expired.is_set():
                    self.cancel(nonce)

        launcher_thread = threading.Thread(
            target=invoke_launcher,
            name="jarvis-update-health-launcher",
            daemon=True,
        )
        launcher_thread.start()
        expected_fields = {
            "schema",
            "product_id",
            "bundle_id",
            "nonce",
            "version",
            "build",
            "healthy",
        }
        try:
            while time.monotonic() < deadline:
                try:
                    response = _strict_json(
                        response_path,
                        _HEALTH_DOCUMENT_MAX_BYTES,
                        expected_fields,
                    )
                except (FileNotFoundError, OSError, ValueError):
                    if launcher_done.is_set():
                        return False
                    time.sleep(min(self._poll_interval, max(deadline - time.monotonic(), 0)))
                    continue
                return response == {
                    "schema": _HEALTH_RESPONSE_SCHEMA,
                    "product_id": PRODUCT_ID,
                    "bundle_id": BUNDLE_ID,
                    "nonce": nonce,
                    "version": str(expected.version),
                    "build": expected.build,
                    "healthy": True,
                }
            return False
        finally:
            if launcher_thread.is_alive():
                launcher_expired.set()
            self.cancel(nonce)


def write_health_response(
    request_path: str | os.PathLike[str],
    response_path: str | os.PathLike[str],
    *,
    actual_product_id: str,
    actual_bundle_id: str,
    actual_identity: ProductVersion,
    healthy: bool,
) -> None:
    """Write health only from independently loaded packaged runtime identity.

    The caller must load ``actual_*`` from immutable packaged build metadata,
    not from this request.  Echoing request claims without that comparison does
    not prove which application binary reached healthy startup.
    """

    request = _strict_json(
        Path(request_path),
        _HEALTH_DOCUMENT_MAX_BYTES,
        {"schema", "product_id", "bundle_id", "nonce", "version", "build"},
    )
    if (
        request.get("schema") != _HEALTH_REQUEST_SCHEMA
        or request.get("product_id") != PRODUCT_ID
        or request.get("bundle_id") != BUNDLE_ID
        or type(request.get("nonce")) is not str
        or _NONCE_RE.fullmatch(request["nonce"]) is None
        or type(request.get("version")) is not str
        or type(request.get("build")) is not int
        or actual_product_id != PRODUCT_ID
        or actual_bundle_id != BUNDLE_ID
        or not isinstance(actual_identity, ProductVersion)
        or type(healthy) is not bool
    ):
        raise ValueError("health request is invalid")
    requested_identity = ProductVersion.parse(request["version"], request["build"])
    if requested_identity != actual_identity:
        raise ValueError("running application identity does not match health request")
    _atomic_private_json(
        Path(response_path),
        {
            "schema": _HEALTH_RESPONSE_SCHEMA,
            "product_id": actual_product_id,
            "bundle_id": actual_bundle_id,
            "nonce": request["nonce"],
            "version": str(actual_identity.version),
            "build": actual_identity.build,
            "healthy": healthy,
        },
    )


class MacOSDevelopmentUpdaterAdapter(UpdaterPlatformAdapter):
    """Real same-volume replacement adapter for explicit local tests only."""

    platform_key = "macos"

    def __init__(
        self,
        *,
        installed_app: str | os.PathLike[str],
        backup_root: str | os.PathLike[str],
        health_probe: FileNonceHealthProbe,
        development_mode: bool,
        frozen: bool | None = None,
        expected_bundle_id: str = BUNDLE_ID,
        archive_limits: MacOSArchiveLimits | None = None,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        if frozen is not None and type(frozen) is not bool:
            raise TypeError("frozen override must be boolean")
        runtime_frozen = bool(getattr(sys, "frozen", False)) or frozen is True
        if development_mode is not True or runtime_frozen:
            raise RuntimeError("development updater is disabled outside explicit source tests")
        if not isinstance(health_probe, FileNonceHealthProbe):
            raise TypeError("development updater requires a nonce health probe")
        if type(expected_bundle_id) is not str or not expected_bundle_id:
            raise ValueError("expected bundle identifier is invalid")
        if phase_hook is not None and not callable(phase_hook):
            raise TypeError("phase hook must be callable")
        supplied_installed = Path(installed_app).expanduser()
        supplied_backups = Path(backup_root).expanduser()
        if (
            not supplied_installed.is_absolute()
            or not supplied_backups.is_absolute()
            or supplied_installed.is_symlink()
            or supplied_backups.is_symlink()
        ):
            raise ValueError("development updater paths must be absolute")
        installed = supplied_installed.resolve(strict=False)
        backups = supplied_backups.resolve(strict=False)
        health_root = health_probe.root
        if (
            backups == installed
            or backups.is_relative_to(installed)
            or installed.is_relative_to(backups)
            or health_root == installed
            or health_root.is_relative_to(installed)
            or installed.is_relative_to(health_root)
            or health_root == backups
            or health_root.is_relative_to(backups)
            or backups.is_relative_to(health_root)
        ):
            raise ValueError("updater storage paths must not overlap")
        self._installed_app = installed
        self._backup_root = backups
        self._health_probe = health_probe
        self._expected_bundle_id = expected_bundle_id
        self._limits = archive_limits or MacOSArchiveLimits()
        self._phase_hook = phase_hook
        self._pending_health: tuple[ProductVersion, str] | None = None

    def capability(self) -> UpdaterCapability:
        try:
            parent = self._installed_app.parent
            if parent.is_symlink() or not parent.is_dir():
                raise OSError
            _private_directory(self._backup_root, create=True)
            if parent.stat().st_dev != self._backup_root.stat().st_dev:
                raise OSError
        except OSError:
            return UpdaterCapability(
                self.platform_key,
                AdapterStatus.NOT_AVAILABLE,
                False,
                "development_same_volume_storage_not_available",
            )
        return UpdaterCapability(self.platform_key, AdapterStatus.SUCCESS, True, None)

    def validate_journal_path(self, journal_path: Path) -> bool:
        if not super().validate_journal_path(journal_path):
            return False
        protected_roots = (
            self._installed_app.parent,
            self._backup_root,
            self._health_probe.root,
        )
        return all(
            journal_path != root and not journal_path.is_relative_to(root)
            for root in protected_roots
        )

    def _hook(self, phase: str) -> None:
        if self._phase_hook is not None:
            self._phase_hook(phase)

    def _identity(self) -> ProductVersion | None:
        try:
            _validate_regular_app_tree(self._installed_app)
            return read_macos_app_identity(
                self._installed_app,
                expected_bundle_id=self._expected_bundle_id,
            )
        except (OSError, MacOSArchiveError):
            return None

    def _backup_metadata(
        self,
        reference: str,
        *,
        expected_previous: ProductVersion | None = None,
        expected_target: ProductVersion | None = None,
    ) -> tuple[Path, ProductVersion, ProductVersion, str]:
        if not re.fullmatch(r"backup-[0-9]+-[0-9a-f]{32}", reference):
            raise ValueError("backup reference is invalid")
        root = self._backup_root / reference
        if root.is_symlink() or root.parent != self._backup_root or not root.is_dir():
            raise OSError("backup path is unsafe")
        document = _strict_json(
            root / "metadata.json",
            _BACKUP_METADATA_MAX_BYTES,
            {
                "schema",
                "bundle_id",
                "source_version",
                "source_build",
                "target_version",
                "target_build",
                "tree_sha256",
            },
        )
        if (
            document.get("schema") != _BACKUP_SCHEMA
            or document.get("bundle_id") != self._expected_bundle_id
        ):
            raise ValueError("backup identity is invalid")
        source = ProductVersion.parse(document["source_version"], document["source_build"])
        target = ProductVersion.parse(document["target_version"], document["target_build"])
        tree_sha256 = document.get("tree_sha256")
        if (
            type(tree_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", tree_sha256) is None
        ):
            raise ValueError("backup tree digest is invalid")
        if expected_previous is not None and source != expected_previous:
            raise ValueError("backup source identity mismatch")
        if expected_target is not None and target != expected_target:
            raise ValueError("backup target identity mismatch")
        app = root / "JARVIS.app"
        if read_macos_app_identity(app, expected_bundle_id=self._expected_bundle_id) != source:
            raise ValueError("persisted backup application identity mismatch")
        _validate_regular_app_tree(app)
        if _app_tree_digest(app) != tree_sha256:
            raise ValueError("persisted backup application digest mismatch")
        return app, source, target, tree_sha256

    def prepare_persisted_backup(
        self,
        *,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterBackupResult:
        if self.capability().status is not AdapterStatus.SUCCESS or self._identity() != source:
            return AdapterBackupResult(AdapterStatus.NOT_AVAILABLE)
        reference = f"backup-{source.build}-{secrets.token_hex(16)}"
        temporary = self._backup_root / f".backup-{secrets.token_hex(16)}.tmp"
        final = self._backup_root / reference
        try:
            temporary.mkdir(mode=0o700)
            source_digest_before = _app_tree_digest(self._installed_app)
            backup_app = temporary / "JARVIS.app"
            _copy_regular_tree(self._installed_app, backup_app)
            tree_sha256 = _app_tree_digest(backup_app)
            source_digest_after = _app_tree_digest(self._installed_app)
            if not source_digest_before == tree_sha256 == source_digest_after:
                raise OSError("installed application changed during backup")
            _atomic_private_json(
                temporary / "metadata.json",
                {
                    "schema": _BACKUP_SCHEMA,
                    "bundle_id": self._expected_bundle_id,
                    "source_version": str(source.version),
                    "source_build": source.build,
                    "target_version": str(target.version),
                    "target_build": target.build,
                    "tree_sha256": tree_sha256,
                },
            )
            _fsync_directory(temporary)
            os.replace(temporary, final)
            _fsync_directory(self._backup_root)
            self._backup_metadata(
                reference,
                expected_previous=source,
                expected_target=target,
            )
            return AdapterBackupResult(AdapterStatus.SUCCESS, reference)
        except Exception:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)
            return AdapterBackupResult(AdapterStatus.FAILED)

    def install(
        self,
        staged_artifact: VerifiedArtifactHandle,
        *,
        backup_reference: str,
        source: ProductVersion,
        target: ProductVersion,
    ) -> AdapterMutationResult:
        if not isinstance(staged_artifact, VerifiedArtifactHandle):
            raise TypeError("verified artifact handle is required")
        stage_root: Path | None = None
        displaced: Path | None = None
        old_was_displaced = False
        candidate_installed = False
        try:
            self._backup_metadata(
                backup_reference,
                expected_previous=source,
                expected_target=target,
            )
            if self._identity() != source:
                return AdapterMutationResult(AdapterStatus.FAILED, False)
            stage_root = Path(
                tempfile.mkdtemp(prefix=".jarvis-update-", dir=self._installed_app.parent)
            )
            stage_root.chmod(0o700)
            with tempfile.TemporaryFile(mode="w+b", dir=stage_root) as private_archive:
                if not staged_artifact.copy_verified_to_private_descriptor(
                    private_archive.fileno()
                ):
                    return AdapterMutationResult(AdapterStatus.FAILED, False)
                extracted = extract_macos_app_zip_v1(
                    private_archive.fileno(),
                    stage_root / "extract",
                    expected_bundle_id=self._expected_bundle_id,
                    expected_identity=target,
                    limits=self._limits,
                )
            displaced = self._installed_app.parent / f".jarvis-old-{backup_reference}.app"
            if displaced.exists() or displaced.is_symlink():
                return AdapterMutationResult(AdapterStatus.FAILED, False)
            nonce = self._health_probe.arm(target)
            self._pending_health = (target, nonce)
            self._hook("before_displace")
            os.replace(self._installed_app, displaced)
            old_was_displaced = True
            _fsync_directory(self._installed_app.parent)
            self._hook("after_displace")
            os.replace(extracted.app_path, self._installed_app)
            candidate_installed = True
            _fsync_directory(self._installed_app.parent)
            self._hook("after_candidate_replace")
            shutil.rmtree(displaced)
            displaced = None
            _fsync_directory(self._installed_app.parent)
            return AdapterMutationResult(AdapterStatus.SUCCESS, True)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._pending_health = None
            self._health_probe.cancel()
            if old_was_displaced and not candidate_installed and displaced is not None:
                try:
                    os.replace(displaced, self._installed_app)
                    _fsync_directory(self._installed_app.parent)
                    old_was_displaced = False
                except OSError:
                    pass
            return AdapterMutationResult(
                AdapterStatus.FAILED,
                old_was_displaced or candidate_installed,
            )
        finally:
            if stage_root is not None and stage_root.exists() and not stage_root.is_symlink():
                shutil.rmtree(stage_root, ignore_errors=True)

    def verify_installed(
        self,
        expected: ProductVersion,
        *,
        timeout_seconds: float,
    ) -> AdapterVerificationResult:
        if self._identity() != expected:
            return AdapterVerificationResult(AdapterStatus.FAILED)
        matching_backup_digests: set[str] = set()
        try:
            if self._backup_root.is_dir() and not self._backup_root.is_symlink():
                candidates = sorted(self._backup_root.iterdir(), key=lambda path: path.name)
                if len(candidates) > 32:
                    raise OSError("too many persisted backups")
                for candidate in candidates:
                    if not candidate.name.startswith("backup-"):
                        continue
                    try:
                        _, source, _, tree_sha256 = self._backup_metadata(candidate.name)
                    except (OSError, TypeError, ValueError):
                        return AdapterVerificationResult(AdapterStatus.FAILED)
                    if source == expected:
                        matching_backup_digests.add(tree_sha256)
        except (OSError, MacOSArchiveError):
            return AdapterVerificationResult(AdapterStatus.FAILED)
        if matching_backup_digests:
            try:
                installed_digest = _app_tree_digest(self._installed_app)
            except (OSError, MacOSArchiveError):
                return AdapterVerificationResult(AdapterStatus.FAILED)
            if installed_digest not in matching_backup_digests:
                return AdapterVerificationResult(AdapterStatus.FAILED)
        pending = self._pending_health
        self._pending_health = None
        try:
            if pending is not None and pending[0] == expected:
                nonce = pending[1]
            else:
                self._health_probe.cancel()
                nonce = self._health_probe.arm(expected)
            if not self._health_probe.verify(
                self._installed_app,
                expected,
                nonce,
                timeout_seconds,
            ):
                return AdapterVerificationResult(AdapterStatus.FAILED)
        except (OSError, TypeError, ValueError):
            return AdapterVerificationResult(AdapterStatus.FAILED)
        return AdapterVerificationResult(
            AdapterStatus.SUCCESS,
            installed=expected,
            healthy=True,
        )

    def rollback(
        self,
        backup_reference: str,
        expected_previous: ProductVersion,
    ) -> AdapterMutationResult:
        restore_root: Path | None = None
        displaced: Path | None = None
        mutated = False
        try:
            backup_app, _, _, backup_digest = self._backup_metadata(
                backup_reference,
                expected_previous=expected_previous,
            )
            interrupted_old = (
                self._installed_app.parent / f".jarvis-old-{backup_reference}.app"
            )
            restore_root = Path(
                tempfile.mkdtemp(prefix=".jarvis-restore-", dir=self._installed_app.parent)
            )
            restore_root.chmod(0o700)
            restore_app = restore_root / "JARVIS.app"
            _copy_regular_tree(backup_app, restore_app)
            self._hook("before_rollback_replace")
            if self._installed_app.exists():
                if self._installed_app.is_symlink():
                    raise OSError("installed application path is unsafe")
                displaced = self._installed_app.parent / (
                    f".jarvis-failed-{secrets.token_hex(16)}.app"
                )
                os.replace(self._installed_app, displaced)
                mutated = True
                _fsync_directory(self._installed_app.parent)
            self._hook("after_rollback_displace")
            os.replace(restore_app, self._installed_app)
            mutated = True
            _fsync_directory(self._installed_app.parent)
            if _app_tree_digest(self._installed_app) != backup_digest:
                raise OSError("restored application digest mismatch")
            self._hook("after_rollback_replace")
            if displaced is not None:
                shutil.rmtree(displaced)
                displaced = None
            if interrupted_old.exists() and interrupted_old != self._installed_app:
                if interrupted_old.is_symlink():
                    raise OSError("interrupted update path is unsafe")
                shutil.rmtree(interrupted_old)
            self._pending_health = None
            self._health_probe.cancel()
            return AdapterMutationResult(AdapterStatus.SUCCESS, True)
        except Exception:
            if displaced is not None and displaced.exists() and not self._installed_app.exists():
                try:
                    os.replace(displaced, self._installed_app)
                    _fsync_directory(self._installed_app.parent)
                    displaced = None
                except OSError:
                    pass
            return AdapterMutationResult(AdapterStatus.FAILED, mutated)
        finally:
            if restore_root is not None and restore_root.exists() and not restore_root.is_symlink():
                shutil.rmtree(restore_root, ignore_errors=True)


__all__ = [
    "MACOS_APP_ZIP_FORMAT",
    "PRODUCTION_MACOS_HELPER_PATH",
    "ExtractedMacOSApp",
    "FileNonceHealthProbe",
    "HelperCommandResult",
    "MacOSArchiveError",
    "MacOSArchiveLimits",
    "MacOSDevelopmentUpdaterAdapter",
    "MacOSHelperAssessment",
    "assess_production_macos_helper",
    "extract_macos_app_zip_v1",
    "read_macos_app_identity",
    "write_health_response",
]

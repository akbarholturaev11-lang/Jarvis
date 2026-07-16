"""Restore backend state from a strictly validated, privately staged backup.

The manifest schema, canonical relative paths, declared sizes and hashes are all
validated before the target tree is touched.  Database files additionally pass
SQLite ``integrity_check`` so an attacker cannot make a corrupt database valid by
merely recomputing its manifest hash.  Every source/staging/target file rejects
symlinks, special files and multiple hard links.

The backend service must be stopped and a fresh target directory is required.
The complete tree is staged in an owner-only sibling directory on the target
filesystem, verified, flushed, and exposed with one directory rename.  Existing
targets are never overlaid.  The legacy ``force`` argument is retained only for
CLI/API compatibility and is rejected as ``not_available``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from product_backend.migrations import KNOWN_DATABASES

from ._common import (
    OpsNotAvailableError,
    UnsafePathError,
    canonical_safe_path,
    copy_stable_file,
    emit,
    ensure_private_directory,
    fsync_directory,
    hash_stable_file,
    publish_private_directory,
    read_stable_bytes,
    reject_repository_output_path,
    require_permission_applied,
    require_secure_ops_platform,
    validate_directory,
    validate_private_directory,
)
from .backup import (
    EVIDENCE_DIRNAME,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    MAX_DATABASE_BYTES,
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_FILES,
    MAX_EVIDENCE_TOTAL_BYTES,
    MAX_MANIFEST_RELPATH_LENGTH,
    portable_evidence_relpath,
    validate_database_snapshot,
)

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_BACKUP_FILES = len(KNOWN_DATABASES) + MAX_EVIDENCE_FILES
MAX_BACKUP_TOTAL_BYTES = (
    len(KNOWN_DATABASES) * MAX_DATABASE_BYTES + MAX_EVIDENCE_TOTAL_BYTES
)


class RestoreError(RuntimeError):
    """A backup is incomplete, unsafe, unreadable, or fails verification."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError(f"duplicate manifest field: {key}")
        result[key] = value
    return result


def _canonical_relpath(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MANIFEST_RELPATH_LENGTH
        or "\x00" in value
    ):
        raise RestoreError("manifest contains an invalid file path")
    # Backslashes are separators on Windows and ordinary characters on POSIX;
    # accepting them would make one manifest resolve differently by host.
    if "\\" in value:
        raise RestoreError(f"manifest path is not canonical: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise RestoreError(f"manifest path is not canonical: {value!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RestoreError(f"unsafe manifest path: {value!r}")

    if value in KNOWN_DATABASES:
        return value
    if len(path.parts) >= 2 and path.parts[0] == EVIDENCE_DIRNAME:
        try:
            portable = portable_evidence_relpath(Path(*path.parts[1:]))
        except (OSError, ValueError) as exc:
            raise RestoreError(f"manifest path is not portable: {value!r}") from exc
        if f"{EVIDENCE_DIRNAME}/{portable}" != value:
            raise RestoreError(f"manifest path is not canonical: {value!r}")
        return value
    raise RestoreError(f"manifest path is outside the backup allowlist: {value!r}")


def _load_manifest(backup_dir: Path) -> dict[str, dict[str, object]]:
    manifest_path = backup_dir / MANIFEST_NAME
    try:
        raw = read_stable_bytes(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    except (OSError, ValueError) as exc:
        raise RestoreError("backup manifest is missing or unsafe") from exc
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except UnicodeDecodeError as exc:
        raise RestoreError("backup manifest is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RestoreError("backup manifest is not valid JSON") from exc
    except RecursionError as exc:
        raise RestoreError("backup manifest nesting is too deep") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "created_at",
        "files",
    }:
        raise RestoreError("backup manifest has unexpected fields")
    if document["schema"] != MANIFEST_SCHEMA:
        raise RestoreError("backup manifest schema is not supported")
    created_at = document["created_at"]
    if not isinstance(created_at, str) or len(created_at) > 64:
        raise RestoreError("backup manifest timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise RestoreError("backup manifest timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RestoreError("backup manifest timestamp must include a timezone")

    files = document["files"]
    if not isinstance(files, dict):
        raise RestoreError("backup manifest has no file list")
    if len(files) > MAX_BACKUP_FILES:
        raise RestoreError("backup manifest file-count limit exceeded")
    missing_databases = set(KNOWN_DATABASES).difference(files)
    if missing_databases:
        raise RestoreError("backup manifest is missing required backend databases")

    normalized: dict[str, dict[str, object]] = {}
    collision_keys: dict[str, str] = {}
    total_bytes = 0
    evidence_files = 0
    evidence_bytes = 0
    for raw_path, raw_meta in files.items():
        relpath = _canonical_relpath(raw_path)
        collision_key = unicodedata.normalize("NFC", relpath).casefold()
        previous = collision_keys.get(collision_key)
        if previous is not None and previous != relpath:
            raise RestoreError("manifest paths collide across supported filesystems")
        collision_keys[collision_key] = relpath
        if not isinstance(raw_meta, dict) or set(raw_meta) != {"sha256", "bytes"}:
            raise RestoreError(f"invalid metadata for {relpath}")
        digest = raw_meta["sha256"]
        byte_size = raw_meta["bytes"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RestoreError(f"invalid SHA-256 for {relpath}")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise RestoreError(f"invalid byte size for {relpath}")
        if relpath in KNOWN_DATABASES:
            if byte_size > MAX_DATABASE_BYTES:
                raise RestoreError(f"database exceeds restore limit: {relpath}")
        else:
            evidence_files += 1
            evidence_bytes += byte_size
            if byte_size > MAX_EVIDENCE_FILE_BYTES:
                raise RestoreError(f"payment evidence exceeds restore limit: {relpath}")
            if evidence_files > MAX_EVIDENCE_FILES:
                raise RestoreError("payment evidence file-count limit exceeded")
            if evidence_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                raise RestoreError("payment evidence aggregate-size limit exceeded")
        total_bytes += byte_size
        if total_bytes > MAX_BACKUP_TOTAL_BYTES:
            raise RestoreError("backup aggregate-size limit exceeded")
        normalized[relpath] = {"sha256": digest, "bytes": byte_size}
    return normalized


def _sqlite_integrity(path: Path, *, relpath: str) -> None:
    try:
        validate_database_snapshot(path, relpath)
    except (RuntimeError, sqlite3.Error) as exc:
        raise RestoreError(
            f"SQLite integrity/schema check failed for {relpath}"
        ) from exc


def _evidence_relpaths(backup_dir: Path) -> set[str]:
    """Enumerate the complete safe evidence tree represented by a backup."""

    evidence_root = backup_dir / EVIDENCE_DIRNAME
    try:
        root_info = evidence_root.lstat()
    except FileNotFoundError as exc:
        raise RestoreError("backup is missing the payment-evidence directory") from exc
    if not stat.S_ISDIR(root_info.st_mode) or evidence_root.is_symlink():
        raise RestoreError("backup payment-evidence root is unsafe")

    relpaths: set[str] = set()
    try:
        validate_directory(evidence_root)
        for current, dirnames, filenames in os.walk(
            evidence_root,
            followlinks=False,
        ):
            current_path = Path(current)
            validate_directory(current_path)
            dirnames.sort()
            filenames.sort()
            for dirname in dirnames:
                child = current_path / dirname
                info = child.lstat()
                if not stat.S_ISDIR(info.st_mode) or child.is_symlink():
                    raise RestoreError("backup contains an unsafe evidence directory")
                portable_evidence_relpath(child.relative_to(evidence_root))
            for filename in filenames:
                child = current_path / filename
                info = child.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RestoreError("backup contains an unsafe evidence object")
                portable = portable_evidence_relpath(child.relative_to(evidence_root))
                relpaths.add(f"{EVIDENCE_DIRNAME}/{portable}")
    except RestoreError:
        raise
    except (OSError, ValueError, UnsafePathError) as exc:
        raise RestoreError("backup payment-evidence tree is unsafe") from exc
    return relpaths


def verify_backup(backup_dir: Path) -> dict[str, dict[str, object]]:
    """Strictly verify manifest structure, every source, and every database."""

    backup_dir = Path(backup_dir)
    try:
        validate_directory(backup_dir)
        files = _load_manifest(backup_dir)
        declared_evidence = {
            relpath for relpath in files if relpath not in KNOWN_DATABASES
        }
        if _evidence_relpaths(backup_dir) != declared_evidence:
            raise RestoreError(
                "backup payment-evidence tree does not match the manifest"
            )
        for relpath, meta in files.items():
            source = backup_dir / PurePosixPath(relpath)
            result = hash_stable_file(
                source,
                max_bytes=int(meta["bytes"]),
            )
            if result.byte_size != meta["bytes"]:
                raise RestoreError(f"size check failed for {relpath}")
            if result.sha256 != meta["sha256"]:
                raise RestoreError(f"integrity check failed for {relpath}")
            if relpath in KNOWN_DATABASES:
                _sqlite_integrity(source, relpath=relpath)
    except RestoreError:
        raise
    except (OSError, ValueError, UnsafePathError) as exc:
        raise RestoreError("backup contains an unsafe or unreadable file") from exc
    return files


def _paths_overlap(left: Path, right: Path) -> bool:
    left_abs = canonical_safe_path(left)
    right_abs = canonical_safe_path(right)
    return (
        left_abs == right_abs
        or left_abs in right_abs.parents
        or right_abs in left_abs.parents
    )


def restore(backup_dir: Path, data_dir: Path, *, force: bool = False) -> int:
    """Verify and atomically publish a complete tree to a nonexistent target."""

    require_secure_ops_platform()
    backup_dir = Path(backup_dir)
    data_dir = reject_repository_output_path(Path(data_dir))
    if force:
        raise RestoreError(
            "force overlay restore is not_available; use a fresh nonexistent target"
        )
    files = verify_backup(backup_dir)
    if _paths_overlap(backup_dir, data_dir):
        raise RestoreError("backup and target data directories must not overlap")

    try:
        data_dir.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RestoreError("target data directory must not exist")
    try:
        target_parent = validate_private_directory(data_dir.parent)
    except OSError as exc:
        raise RestoreError(
            "target parent must be an existing owner-only safe directory"
        ) from exc

    staging_root = Path(
        tempfile.mkdtemp(prefix=".jarvis-restore-", dir=target_parent)
    )
    published = False
    try:
        require_permission_applied(
            ensure_private_directory(staging_root),
            label="restore staging directory",
        )
        require_permission_applied(
            ensure_private_directory(staging_root / EVIDENCE_DIRNAME),
            label="restore payment-evidence directory",
        )
        for relpath, meta in files.items():
            source = backup_dir / PurePosixPath(relpath)
            staged = staging_root / PurePosixPath(relpath)
            try:
                copy_stable_file(
                    source,
                    staged,
                    max_bytes=int(meta["bytes"]),
                    expected_size=int(meta["bytes"]),
                    expected_sha256=str(meta["sha256"]),
                )
            except (OSError, ValueError) as exc:
                raise RestoreError(f"staging verification failed for {relpath}") from exc
            if relpath in KNOWN_DATABASES:
                _sqlite_integrity(staged, relpath=relpath)

        for current, _dirnames, _filenames in os.walk(
            staging_root,
            topdown=False,
            followlinks=False,
        ):
            fsync_directory(Path(current))
        try:
            publish_private_directory(staging_root, data_dir)
        except OSError as exc:
            raise RestoreError("atomic restore publication failed") from exc
        published = True
        return len(files)
    finally:
        if not published:
            shutil.rmtree(staging_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Restore a JARVIS backend backup while the backend is stopped; "
            "a fresh target directory is required."
        )
    )
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="compatibility flag; overlay restore is rejected as not_available",
    )
    args = parser.parse_args(argv)
    try:
        restored = restore(args.backup_dir, args.data_dir, force=args.force)
    except (RestoreError, OpsNotAvailableError, UnsafePathError) as exc:
        emit(f"[fail] {exc}")
        return 1
    emit(f"[ok] restored {restored} file(s) into {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

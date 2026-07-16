"""Restore backend databases and payment evidence from a verified backup.

Every file is re-hashed against the backup ``manifest.json`` before it is copied
into the target data directory, so a corrupted or tampered backup fails closed.
The restore refuses to overwrite an existing non-empty data directory unless
``--force`` is given.

Usage::

    python -m ops.restore --backup-dir /var/backups/jarvis/latest \
        --data-dir /var/lib/jarvis/data
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from product_backend.migrations import KNOWN_DATABASES

from ._common import emit, harden_directory, harden_file
from .backup import EVIDENCE_DIRNAME, MANIFEST_NAME

_HASH_CHUNK = 1024 * 1024


class RestoreError(RuntimeError):
    """A backup is incomplete, unreadable, or fails integrity verification."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(backup_dir: Path) -> dict[str, dict[str, object]]:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RestoreError("backup manifest is missing")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RestoreError("backup manifest is not valid JSON") from exc
    files = document.get("files")
    if not isinstance(files, dict):
        raise RestoreError("backup manifest has no file list")
    return files


def verify_backup(backup_dir: Path) -> dict[str, dict[str, object]]:
    """Verify every manifest file exists in the backup and hashes match."""

    backup_dir = Path(backup_dir)
    files = _load_manifest(backup_dir)
    for relpath, meta in files.items():
        if not isinstance(relpath, str) or ".." in Path(relpath).parts:
            raise RestoreError(f"unsafe manifest path: {relpath!r}")
        source = backup_dir / relpath
        if not source.is_file():
            raise RestoreError(f"backup is missing {relpath}")
        expected = meta.get("sha256") if isinstance(meta, dict) else None
        if _sha256_file(source) != expected:
            raise RestoreError(f"integrity check failed for {relpath}")
    return files


def restore(backup_dir: Path, data_dir: Path, *, force: bool = False) -> int:
    """Restore a verified backup into ``data_dir``; returns files restored."""

    backup_dir = Path(backup_dir)
    data_dir = Path(data_dir)
    files = verify_backup(backup_dir)

    existing = [
        name for name in KNOWN_DATABASES if (data_dir / name).is_file()
    ]
    if existing and not force:
        raise RestoreError(
            "target data directory already contains databases; pass force=True "
            f"to overwrite: {', '.join(existing)}"
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    harden_directory(data_dir)
    restored = 0
    for relpath in files:
        source = backup_dir / relpath
        target = data_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if relpath.startswith(f"{EVIDENCE_DIRNAME}/") or relpath == EVIDENCE_DIRNAME:
            harden_directory(target.parent)
        target.write_bytes(source.read_bytes())
        harden_file(target)
        restored += 1
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a JARVIS backend backup.")
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        restored = restore(args.backup_dir, args.data_dir, force=args.force)
    except RestoreError as exc:
        emit(f"[fail] {exc}")
        return 1
    emit(f"[ok] restored {restored} file(s) into {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

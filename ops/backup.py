"""Consistent backup of the backend SQLite databases and payment evidence.

Databases are copied with the SQLite online backup API so the snapshot is
transaction-consistent even while the process is running.  Payment evidence
objects are copied byte-for-byte.  Every copied file is hashed and recorded in a
``manifest.json`` so :mod:`ops.restore` can verify integrity before restoring.

Usage::

    python -m ops.backup --data-dir /var/lib/jarvis/data \
        --backup-dir /var/backups/jarvis/2026-07-16T00-00Z
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from product_backend.migrations import KNOWN_DATABASES

from ._common import emit, harden_directory, harden_file

MANIFEST_NAME = "manifest.json"
EVIDENCE_DIRNAME = "payment-evidence"
_MANIFEST_SCHEMA = "jarvis.backend-backup.v1"
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupEntry:
    relpath: str
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema: str
    created_at: str
    entries: tuple[BackupEntry, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": self.schema,
                "created_at": self.created_at,
                "files": {
                    entry.relpath: {
                        "sha256": entry.sha256,
                        "bytes": entry.byte_size,
                    }
                    for entry in self.entries
                },
            },
            indent=2,
            sort_keys=True,
        )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _online_backup(src_path: Path, dest_path: Path) -> None:
    source = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(dest_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def backup(data_dir: Path, backup_dir: Path) -> BackupManifest:
    """Snapshot every present database and payment-evidence object."""

    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise FileExistsError(f"backup directory is not empty: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    harden_directory(backup_dir)

    entries: list[BackupEntry] = []
    for name in KNOWN_DATABASES:
        source = data_dir / name
        if not source.is_file():
            continue
        target = backup_dir / name
        _online_backup(source, target)
        harden_file(target)
        sha256, size = _sha256_file(target)
        entries.append(BackupEntry(name, sha256, size))

    evidence_src = data_dir / EVIDENCE_DIRNAME
    if evidence_src.is_dir():
        evidence_dest = backup_dir / EVIDENCE_DIRNAME
        evidence_dest.mkdir(parents=True, exist_ok=True)
        harden_directory(evidence_dest)
        for item in sorted(evidence_src.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(evidence_src)
            copy_path = evidence_dest / relative
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            copy_path.write_bytes(item.read_bytes())
            harden_file(copy_path)
            sha256, size = _sha256_file(copy_path)
            entries.append(
                BackupEntry(f"{EVIDENCE_DIRNAME}/{relative.as_posix()}", sha256, size)
            )

    manifest = BackupManifest(
        _MANIFEST_SCHEMA,
        datetime.now(timezone.utc).isoformat(),
        tuple(entries),
    )
    manifest_path = backup_dir / MANIFEST_NAME
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    harden_file(manifest_path)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the JARVIS backend state.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = backup(args.data_dir, args.backup_dir)
    emit(
        f"[ok] backed up {len(manifest.entries)} file(s) to {args.backup_dir} "
        f"at {manifest.created_at}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Inspect and apply backend database migrations.

Subcommands::

    python -m ops.migrate report  --data-dir DIR   # show version + integrity
    python -m ops.migrate apply   --data-dir DIR   # apply forward migrations
    python -m ops.migrate verify  --data-dir DIR   # fail closed if not current

Only the commerce database carries an explicit schema version; the other stores
apply their idempotent ``CREATE TABLE IF NOT EXISTS`` schema on process start.
``apply`` runs the real repository migration code path, never a duplicated
schema.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from product_backend import migrations
from product_backend.migrations import COMMERCE_DATABASE, MigrationError

from ._common import emit


def _print_report(data_dir: Path) -> None:
    for status in migrations.report(data_dir):
        version = "-" if status.user_version is None else str(status.user_version)
        if not status.exists:
            emit(f"  {status.filename}: absent")
        else:
            integrity = "ok" if status.integrity_ok else "FAILED"
            emit(
                f"  {status.filename}: version={version} integrity={integrity}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JARVIS backend migrations.")
    parser.add_argument(
        "command", choices=("report", "apply", "verify")
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "report":
        _print_report(args.data_dir)
        return 0

    if args.command == "apply":
        commerce_path = args.data_dir / COMMERCE_DATABASE
        try:
            before, after = migrations.migrate_commerce_database(commerce_path)
        except MigrationError as exc:
            emit(f"[fail] {exc}")
            return 1
        emit(
            f"[ok] commerce schema: {before if before is not None else 'new'} -> "
            f"{after}"
        )
        _print_report(args.data_dir)
        return 0

    try:
        migrations.verify(args.data_dir)
    except MigrationError as exc:
        emit(f"[fail] {exc}")
        return 1
    emit("[ok] all databases are at the expected schema version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

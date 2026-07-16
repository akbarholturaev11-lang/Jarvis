"""Schema-version inspection and forward migration for the backend databases.

The commerce database is the one explicitly versioned store (``PRAGMA
user_version``); its forward, additive migrations live in
:class:`~product_backend.sqlite_repository.SQLiteCommerceRepository` and are
applied by simply opening it.  The device-challenge, activation, MFA, and
admin-credential stores use ``CREATE TABLE IF NOT EXISTS`` schemas that are
(re)applied idempotently when the app process starts, so they have no separate
version to migrate.

This module offers read-only inspection (no heavy dependencies) plus a single
standalone entry point that applies the commerce migration through the real
repository code path — never a hand-rolled duplicate of the schema.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXPECTED_COMMERCE_SCHEMA_VERSION: Final = 4
COMMERCE_DATABASE: Final = "commerce.sqlite3"

# Every SQLite file the backend runtime materializes under the data directory.
KNOWN_DATABASES: Final = (
    COMMERCE_DATABASE,
    "admin-credentials.sqlite3",
    "device-challenges.sqlite3",
    "activation.sqlite3",
    "admin-mfa.sqlite3",
)


class MigrationError(RuntimeError):
    """A database is missing, unreadable, or at an unexpected schema version."""


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    filename: str
    exists: bool
    user_version: int | None
    integrity_ok: bool


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        isolation_level=None,
    )


def inspect_database(path: Path) -> DatabaseStatus:
    """Read the schema version and integrity of one database without writing."""

    filename = path.name
    if not path.is_file():
        return DatabaseStatus(filename, False, None, False)
    connection: sqlite3.Connection | None = None
    try:
        connection = _read_only_connection(path)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity_row = connection.execute("PRAGMA quick_check(1)").fetchone()
        integrity_ok = bool(integrity_row) and str(integrity_row[0]) == "ok"
        return DatabaseStatus(filename, True, version, integrity_ok)
    except sqlite3.Error:
        return DatabaseStatus(filename, True, None, False)
    finally:
        if connection is not None:
            connection.close()


def report(data_dir: Path) -> tuple[DatabaseStatus, ...]:
    """Return the status of every known backend database under ``data_dir``."""

    base = Path(data_dir)
    return tuple(inspect_database(base / name) for name in KNOWN_DATABASES)


def migrate_commerce_database(path: Path) -> tuple[int | None, int]:
    """Apply the commerce forward migration by opening the real repository.

    Returns ``(before_version, after_version)``.  ``before_version`` is ``None``
    when the database did not exist yet.  Raises :class:`MigrationError` if the
    stored schema is newer than this runtime understands.
    """

    from .sqlite_repository import SQLiteCommerceRepository

    resolved = Path(path)
    before = None
    if resolved.is_file():
        pre = inspect_database(resolved)
        before = pre.user_version
    try:
        repository = SQLiteCommerceRepository(resolved)
    except Exception as exc:  # noqa: BLE001 - surface a clear migration failure
        raise MigrationError(f"commerce migration failed: {exc}") from exc
    try:
        after = int(
            repository._connection.execute(  # noqa: SLF001 - trusted internal read
                "PRAGMA user_version"
            ).fetchone()[0]
        )
    finally:
        repository.close()
    return before, after


def verify(data_dir: Path, *, require_commerce: bool = True) -> None:
    """Fail closed unless present databases are intact and at the expected version.

    The commerce database must exist (unless ``require_commerce`` is False) and
    report ``EXPECTED_COMMERCE_SCHEMA_VERSION``.  Any present database that fails
    its integrity check raises :class:`MigrationError`.
    """

    statuses = report(data_dir)
    by_name = {status.filename: status for status in statuses}
    commerce = by_name[COMMERCE_DATABASE]
    if commerce.exists:
        if not commerce.integrity_ok:
            raise MigrationError("commerce database failed its integrity check")
        if commerce.user_version != EXPECTED_COMMERCE_SCHEMA_VERSION:
            raise MigrationError(
                "commerce schema version is "
                f"{commerce.user_version}, expected "
                f"{EXPECTED_COMMERCE_SCHEMA_VERSION}"
            )
    elif require_commerce:
        raise MigrationError("commerce database is missing")
    for status in statuses:
        if status.exists and not status.integrity_ok:
            raise MigrationError(
                f"{status.filename} failed its integrity check"
            )


__all__ = [
    "COMMERCE_DATABASE",
    "DatabaseStatus",
    "EXPECTED_COMMERCE_SCHEMA_VERSION",
    "KNOWN_DATABASES",
    "MigrationError",
    "inspect_database",
    "migrate_commerce_database",
    "report",
    "verify",
]

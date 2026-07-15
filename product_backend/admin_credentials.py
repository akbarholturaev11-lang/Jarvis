"""Durable, owner-private storage for salted admin password hashes.

The bootstrap credential still comes from explicit deployment configuration,
but after first start a password rotation must survive process restarts.  This
store persists only PBKDF2 salt/digest records, never plaintext passwords.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .api_auth import (
    AdminPasswordCredential,
    BackendConfigurationError,
)
from .models import format_utc_timestamp


_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_password_credentials (
    subject TEXT PRIMARY KEY,
    salt BLOB NOT NULL,
    password_digest BLOB NOT NULL,
    iterations INTEGER NOT NULL,
    updated_at TEXT NOT NULL CHECK (substr(updated_at, -1) = 'Z')
);
"""


class SQLiteAdminCredentialStore:
    """Thread-safe persistent credential hash store with one-time bootstrap."""

    def __init__(
        self,
        database: str | Path,
        bootstrap_credentials: Sequence[AdminPasswordCredential],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        credentials = tuple(bootstrap_credentials)
        if not credentials or len({item.subject for item in credentials}) != len(
            credentials
        ):
            raise BackendConfigurationError("admin credential bootstrap is invalid")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._path = None if str(database) == ":memory:" else Path(database)
        if self._path is not None:
            if not self._path.is_absolute() or self._path.is_symlink():
                raise BackendConfigurationError("admin credential store path is invalid")
            try:
                parent = self._path.parent.stat()
            except OSError as exc:
                raise BackendConfigurationError(
                    "admin credential store directory is unavailable"
                ) from exc
            if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
                raise BackendConfigurationError(
                    "admin credential store directory is not private"
                )
            if hasattr(os, "geteuid") and parent.st_uid != os.geteuid():
                raise BackendConfigurationError(
                    "admin credential store directory owner is invalid"
                )
            try:
                existing = os.lstat(self._path)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise BackendConfigurationError(
                    "admin credential store is unavailable"
                ) from exc
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_nlink != 1
                or existing.st_mode & 0o077
                or (hasattr(os, "geteuid") and existing.st_uid != os.geteuid())
            ):
                raise BackendConfigurationError(
                    "admin credential store permissions are invalid"
                )
        try:
            self._connection = sqlite3.connect(
                str(database), isolation_level=None, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.executescript(_SCHEMA)
            if self._path is not None:
                self._path.chmod(0o600)
                opened = self._path.stat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_mode & 0o077
                    or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
                ):
                    raise BackendConfigurationError(
                        "admin credential store permissions are invalid"
                    )
            self._bootstrap(credentials)
        except BackendConfigurationError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise BackendConfigurationError(
                "admin credential store is unavailable"
            ) from exc

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BackendConfigurationError("admin credential clock is invalid")
        return format_utc_timestamp(value.astimezone(timezone.utc))

    def _bootstrap(
        self, credentials: tuple[AdminPasswordCredential, ...]
    ) -> None:
        configured_subjects = {item.subject for item in credentials}
        with self._lock:
            rows = self._connection.execute(
                "SELECT subject FROM admin_password_credentials"
            ).fetchall()
            stored_subjects = {str(row["subject"]) for row in rows}
            if stored_subjects and stored_subjects != configured_subjects:
                raise BackendConfigurationError(
                    "stored admin credential subjects do not match configuration"
                )
            if stored_subjects:
                return
            stamp = self._timestamp()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.executemany(
                    "INSERT INTO admin_password_credentials("
                    "subject, salt, password_digest, iterations, updated_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            item.subject,
                            item.salt,
                            item.password_digest,
                            item.iterations,
                            stamp,
                        )
                        for item in credentials
                    ],
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def load_credentials(self) -> tuple[AdminPasswordCredential, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT subject, salt, password_digest, iterations "
                "FROM admin_password_credentials ORDER BY subject"
            ).fetchall()
        try:
            return tuple(
                AdminPasswordCredential(
                    str(row["subject"]),
                    bytes(row["salt"]),
                    bytes(row["password_digest"]),
                    int(row["iterations"]),
                )
                for row in rows
            )
        except (TypeError, ValueError) as exc:
            raise BackendConfigurationError(
                "stored admin credentials are invalid"
            ) from exc

    def replace_credential(self, credential: AdminPasswordCredential) -> None:
        if not isinstance(credential, AdminPasswordCredential):
            raise BackendConfigurationError("replacement admin credential is invalid")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                updated = self._connection.execute(
                    "UPDATE admin_password_credentials SET salt = ?, "
                    "password_digest = ?, iterations = ?, updated_at = ? "
                    "WHERE subject = ?",
                    (
                        credential.salt,
                        credential.password_digest,
                        credential.iterations,
                        self._timestamp(),
                        credential.subject,
                    ),
                )
                if updated.rowcount != 1:
                    raise BackendConfigurationError(
                        "admin credential subject is not configured"
                    )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = ["SQLiteAdminCredentialStore"]

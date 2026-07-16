"""Consistent, bounded backup of backend SQLite data and payment evidence.

SQLite databases use the online backup API.  Evidence is copied through stable,
no-follow descriptors and every output is recorded in a strict manifest for
``ops.restore``.  Symlinks, special files and hard-linked source files are
rejected: an operator backup must never become a file-exfiltration primitive.
All backend SQLite stores must be present, and the caller must explicitly confirm
the service is stopped: sequential per-store snapshots are cross-store coherent
only inside that maintenance window.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from product_backend.migrations import (
    EXPECTED_COMMERCE_SCHEMA_VERSION,
    KNOWN_DATABASES,
)

from ._common import (
    OpsNotAvailableError,
    UnsafePathError,
    canonical_safe_path,
    copy_stable_file,
    emit,
    ensure_private_directory,
    harden_file,
    hash_stable_file,
    reject_repository_output_path,
    require_permission_applied,
    require_secure_ops_platform,
    validate_directory,
    validate_write_target,
    write_secret_text,
)

MANIFEST_NAME = "manifest.json"
EVIDENCE_DIRNAME = "payment-evidence"
MANIFEST_SCHEMA = "jarvis.backend-backup.v1"

# Payment evidence uploads are already bounded by the API at 10 MiB.  Keep the
# same per-object ceiling here and add aggregate/count ceilings so a compromised
# data tree cannot make the backup job consume unbounded resources.
MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
MAX_EVIDENCE_FILES = 100_000
MAX_DATABASE_BYTES = 64 * 1024 * 1024 * 1024
MAX_MANIFEST_RELPATH_LENGTH = 1024
_PORTABLE_COMPONENT = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9_-])?\Z"
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
EXPECTED_DATABASE_TABLES = {
    "commerce.sqlite3": frozenset(
        {
            "accounts",
            "admin_decision_audits",
            "artifact_compatible_sources",
            "device_bindings",
            "entitlements",
            "licenses",
            "payment_submissions",
            "release_artifacts",
            "releases",
        }
    ),
    "admin-credentials.sqlite3": frozenset({"admin_password_credentials"}),
    "device-challenges.sqlite3": frozenset({"device_challenges"}),
    "activation.sqlite3": frozenset(
        {"activation_credentials", "activation_challenges"}
    ),
    "admin-mfa.sqlite3": frozenset(
        {"admin_mfa", "admin_recovery_codes", "admin_mfa_audit"}
    ),
}


@dataclass(frozen=True, slots=True)
class _SchemaIndex:
    table: str
    columns: tuple[str, ...]
    unique: bool
    partial: bool
    sql: str


@dataclass(frozen=True, slots=True)
class _SchemaTrigger:
    table: str
    sql: str


def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.split()).replace("( ", "(").replace(" )", ")")


# These contracts intentionally describe the schema created by the real backend
# stores.  Column nullability is not compared because a commerce database that
# was upgraded from schema v3 has the same enforced v4 contract but SQLite keeps
# ALTER-added columns nullable and enforces them with the v4 triggers below.
EXPECTED_DATABASE_COLUMNS: dict[
    str, dict[str, dict[str, str]]
] = {
    "commerce.sqlite3": {
        "accounts": {
            "id": "TEXT",
            "external_subject": "TEXT",
            "created_at": "TEXT",
        },
        "licenses": {
            "id": "TEXT",
            "account_id": "TEXT",
            "plan_code": "TEXT",
            "created_at": "TEXT",
        },
        "device_bindings": {
            "id": "TEXT",
            "license_id": "TEXT",
            "device_key_fingerprint": "TEXT",
            "platform": "TEXT",
            "architecture": "TEXT",
            "device_label": "TEXT",
            "activated_at": "TEXT",
            "deactivated_at": "TEXT",
            "replaced_by_binding_id": "TEXT",
            "replacement_reason": "TEXT",
        },
        "releases": {
            "id": "TEXT",
            "version": "TEXT",
            "state": "TEXT",
            "price_minor": "INTEGER",
            "currency": "TEXT",
            "features_en": "TEXT",
            "features_ru": "TEXT",
            "fixes_en": "TEXT",
            "fixes_ru": "TEXT",
            "created_at": "TEXT",
            "published_at": "TEXT",
        },
        "release_artifacts": {
            "id": "TEXT",
            "release_id": "TEXT",
            "platform": "TEXT",
            "architecture": "TEXT",
            "artifact_kind": "TEXT",
            "build": "INTEGER",
            "sha256": "TEXT",
            "byte_size": "INTEGER",
            "storage_key": "TEXT",
            "signature": "TEXT",
            "signing_key_id": "TEXT",
            "signature_verified_at": "TEXT",
            "verification_key_id": "TEXT",
            "created_at": "TEXT",
        },
        "artifact_compatible_sources": {
            "artifact_id": "TEXT",
            "source_version": "TEXT",
        },
        "payment_submissions": {
            "id": "TEXT",
            "license_id": "TEXT",
            "release_id": "TEXT",
            "amount_minor": "INTEGER",
            "currency": "TEXT",
            "screenshot_storage_key": "TEXT",
            "screenshot_sha256": "TEXT",
            "screenshot_byte_size": "INTEGER",
            "screenshot_mime_type": "TEXT",
            "paid_at": "TEXT",
            "submitted_at": "TEXT",
            "client_submission_id": "TEXT",
            "supersedes_payment_id": "TEXT",
            "state": "TEXT",
            "review_started_at": "TEXT",
            "review_started_by": "TEXT",
            "decided_at": "TEXT",
            "decided_by": "TEXT",
            "rejection_reason": "TEXT",
        },
        "entitlements": {
            "id": "TEXT",
            "license_id": "TEXT",
            "release_id": "TEXT",
            "granted_by_payment_id": "TEXT",
            "granted_at": "TEXT",
        },
        "admin_decision_audits": {
            "id": "TEXT",
            "payment_id": "TEXT",
            "actor_admin_subject": "TEXT",
            "decision": "TEXT",
            "reason": "TEXT",
            "occurred_at": "TEXT",
        },
    },
    "admin-credentials.sqlite3": {
        "admin_password_credentials": {
            "subject": "TEXT",
            "salt": "BLOB",
            "password_digest": "BLOB",
            "iterations": "INTEGER",
            "updated_at": "TEXT",
        },
    },
    "device-challenges.sqlite3": {
        "device_challenges": {
            "id": "TEXT",
            "license_id": "TEXT",
            "device_key_fingerprint": "TEXT",
            "action": "TEXT",
            "resource_id": "TEXT",
            "nonce_sha256": "TEXT",
            "issued_at": "TEXT",
            "expires_at": "TEXT",
            "consumed_at": "TEXT",
            "outcome": "TEXT",
        },
    },
    "activation.sqlite3": {
        "activation_credentials": {
            "id": "TEXT",
            "credential_digest": "TEXT",
            "license_id": "TEXT",
            "version": "TEXT",
            "issued_at": "TEXT",
            "expires_at": "TEXT",
            "consumed_at": "TEXT",
        },
        "activation_challenges": {
            "id": "TEXT",
            "credential_id": "TEXT",
            "license_id": "TEXT",
            "version": "TEXT",
            "device_key_fingerprint": "TEXT",
            "platform": "TEXT",
            "architecture": "TEXT",
            "nonce_sha256": "TEXT",
            "issued_at": "TEXT",
            "expires_at": "TEXT",
            "consumed_at": "TEXT",
            "outcome": "TEXT",
        },
    },
    "admin-mfa.sqlite3": {
        "admin_mfa": {
            "subject": "TEXT",
            "state": "TEXT",
            "secret_nonce": "TEXT",
            "secret_ciphertext": "TEXT",
            "last_used_step": "INTEGER",
            "created_at": "TEXT",
            "activated_at": "TEXT",
            "disabled_at": "TEXT",
        },
        "admin_recovery_codes": {
            "id": "TEXT",
            "subject": "TEXT",
            "batch_id": "TEXT",
            "code_hmac": "TEXT",
            "created_at": "TEXT",
            "used_at": "TEXT",
            "revoked_at": "TEXT",
        },
        "admin_mfa_audit": {
            "id": "TEXT",
            "subject": "TEXT",
            "event": "TEXT",
            "detail": "TEXT",
            "occurred_at": "TEXT",
        },
    },
}

EXPECTED_DATABASE_PRIMARY_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    database_name: {
        table_name: (
            ("artifact_id", "source_version")
            if table_name == "artifact_compatible_sources"
            else (
                "subject",
            )
            if table_name in {"admin_password_credentials", "admin_mfa"}
            else ("id",)
        )
        for table_name in tables
    }
    for database_name, tables in EXPECTED_DATABASE_COLUMNS.items()
}

EXPECTED_DATABASE_UNIQUE_KEYS: dict[
    str, dict[str, frozenset[tuple[str, ...]]]
] = {
    database_name: {table_name: frozenset() for table_name in tables}
    for database_name, tables in EXPECTED_DATABASE_COLUMNS.items()
}
EXPECTED_DATABASE_UNIQUE_KEYS["commerce.sqlite3"].update(
    {
        "accounts": frozenset({("external_subject",)}),
        "releases": frozenset({("version",)}),
        "release_artifacts": frozenset(
            {
                ("storage_key",),
                ("release_id", "platform", "architecture", "artifact_kind", "build"),
                ("platform", "architecture", "artifact_kind", "build"),
            }
        ),
        "payment_submissions": frozenset(
            {
                ("screenshot_storage_key",),
                ("id", "license_id", "release_id"),
            }
        ),
        "entitlements": frozenset(
            {("granted_by_payment_id",), ("license_id", "release_id")}
        ),
        "admin_decision_audits": frozenset({("payment_id",)}),
    }
)
EXPECTED_DATABASE_UNIQUE_KEYS["device-challenges.sqlite3"].update(
    {"device_challenges": frozenset({("nonce_sha256",)})}
)
EXPECTED_DATABASE_UNIQUE_KEYS["activation.sqlite3"].update(
    {
        "activation_credentials": frozenset({("credential_digest",)}),
        "activation_challenges": frozenset({("nonce_sha256",)}),
    }
)
EXPECTED_DATABASE_UNIQUE_KEYS["admin-mfa.sqlite3"].update(
    {"admin_recovery_codes": frozenset({("code_hmac",)})}
)


def _index(
    table: str,
    columns: tuple[str, ...],
    *,
    unique: bool,
    partial: bool,
    sql: str,
) -> _SchemaIndex:
    return _SchemaIndex(table, columns, unique, partial, _normalize_schema_sql(sql))


EXPECTED_DATABASE_INDEXES: dict[str, dict[str, _SchemaIndex]] = {
    "commerce.sqlite3": {
        "licenses_by_account_created": _index(
            "licenses",
            ("account_id", "created_at", "id"),
            unique=False,
            partial=False,
            sql="CREATE INDEX licenses_by_account_created "
            "ON licenses(account_id, created_at DESC, id DESC)",
        ),
        "one_active_device_per_license": _index(
            "device_bindings",
            ("license_id",),
            unique=True,
            partial=True,
            sql="CREATE UNIQUE INDEX one_active_device_per_license "
            "ON device_bindings(license_id) WHERE deactivated_at IS NULL",
        ),
        "one_open_payment_per_license_release": _index(
            "payment_submissions",
            ("license_id", "release_id"),
            unique=True,
            partial=True,
            sql="CREATE UNIQUE INDEX one_open_payment_per_license_release "
            "ON payment_submissions(license_id, release_id) "
            "WHERE state IN ('pending', 'under_review')",
        ),
        "one_approved_payment_per_license_release": _index(
            "payment_submissions",
            ("license_id", "release_id"),
            unique=True,
            partial=True,
            sql="CREATE UNIQUE INDEX one_approved_payment_per_license_release "
            "ON payment_submissions(license_id, release_id) WHERE state = 'approved'",
        ),
        "payment_client_submission_identity": _index(
            "payment_submissions",
            ("license_id", "client_submission_id"),
            unique=True,
            partial=False,
            sql="CREATE UNIQUE INDEX payment_client_submission_identity "
            "ON payment_submissions(license_id, client_submission_id)",
        ),
        "payment_single_successor": _index(
            "payment_submissions",
            ("supersedes_payment_id",),
            unique=True,
            partial=True,
            sql="CREATE UNIQUE INDEX payment_single_successor "
            "ON payment_submissions(supersedes_payment_id) "
            "WHERE supersedes_payment_id IS NOT NULL",
        ),
        "entitlements_by_license_granted": _index(
            "entitlements",
            ("license_id", "granted_at", "id"),
            unique=False,
            partial=False,
            sql="CREATE INDEX entitlements_by_license_granted "
            "ON entitlements(license_id, granted_at DESC, id DESC)",
        ),
    },
    "admin-credentials.sqlite3": {},
    "device-challenges.sqlite3": {
        "device_challenges_expiry": _index(
            "device_challenges",
            ("expires_at",),
            unique=False,
            partial=False,
            sql="CREATE INDEX device_challenges_expiry "
            "ON device_challenges(expires_at)",
        )
    },
    "activation.sqlite3": {
        "activation_credentials_expiry": _index(
            "activation_credentials",
            ("expires_at",),
            unique=False,
            partial=False,
            sql="CREATE INDEX activation_credentials_expiry "
            "ON activation_credentials(expires_at)",
        ),
        "activation_one_live_challenge": _index(
            "activation_challenges",
            ("credential_id",),
            unique=True,
            partial=True,
            sql="CREATE UNIQUE INDEX activation_one_live_challenge "
            "ON activation_challenges(credential_id) WHERE consumed_at IS NULL",
        ),
    },
    "admin-mfa.sqlite3": {
        "admin_recovery_codes_subject": _index(
            "admin_recovery_codes",
            ("subject",),
            unique=False,
            partial=False,
            sql="CREATE INDEX admin_recovery_codes_subject "
            "ON admin_recovery_codes(subject)",
        ),
        "admin_mfa_audit_time": _index(
            "admin_mfa_audit",
            ("occurred_at",),
            unique=False,
            partial=False,
            sql="CREATE INDEX admin_mfa_audit_time ON admin_mfa_audit(occurred_at)",
        ),
    },
}


def _trigger(table: str, sql: str) -> _SchemaTrigger:
    return _SchemaTrigger(table, _normalize_schema_sql(sql))


EXPECTED_DATABASE_TRIGGERS: dict[str, dict[str, _SchemaTrigger]] = {
    database_name: {} for database_name in EXPECTED_DATABASE_COLUMNS
}
EXPECTED_DATABASE_TRIGGERS["commerce.sqlite3"].update(
    {
        "entitlement_requires_approved_payment": _trigger(
            "entitlements",
            """CREATE TRIGGER entitlement_requires_approved_payment
            BEFORE INSERT ON entitlements FOR EACH ROW WHEN NOT EXISTS (
                SELECT 1 FROM payment_submissions p
                WHERE p.id = NEW.granted_by_payment_id
                  AND p.license_id = NEW.license_id
                  AND p.release_id = NEW.release_id
                  AND p.state = 'approved'
            ) BEGIN
                SELECT RAISE(ABORT, 'entitlement requires approved payment');
            END""",
        ),
        "admin_decision_audits_are_append_only_update": _trigger(
            "admin_decision_audits",
            """CREATE TRIGGER admin_decision_audits_are_append_only_update
            BEFORE UPDATE ON admin_decision_audits BEGIN
                SELECT RAISE(ABORT, 'admin decision audits are append-only');
            END""",
        ),
        "admin_decision_audits_are_append_only_delete": _trigger(
            "admin_decision_audits",
            """CREATE TRIGGER admin_decision_audits_are_append_only_delete
            BEFORE DELETE ON admin_decision_audits BEGIN
                SELECT RAISE(ABORT, 'admin decision audits are append-only');
            END""",
        ),
        "payment_submission_identity_required_insert": _trigger(
            "payment_submissions",
            """CREATE TRIGGER payment_submission_identity_required_insert
            BEFORE INSERT ON payment_submissions
            WHEN NEW.client_submission_id IS NULL
            OR length(NEW.client_submission_id) NOT BETWEEN 3 AND 128
            BEGIN SELECT RAISE(ABORT,
            'payment submission identity is required'); END""",
        ),
        "payment_submission_identity_required_update": _trigger(
            "payment_submissions",
            """CREATE TRIGGER payment_submission_identity_required_update
            BEFORE UPDATE OF client_submission_id ON payment_submissions
            WHEN NEW.client_submission_id IS NULL
            OR length(NEW.client_submission_id) NOT BETWEEN 3 AND 128
            BEGIN SELECT RAISE(ABORT,
            'payment submission identity is required'); END""",
        ),
        "payment_supersession_is_rejected_insert": _trigger(
            "payment_submissions",
            """CREATE TRIGGER payment_supersession_is_rejected_insert
            BEFORE INSERT ON payment_submissions
            WHEN NEW.supersedes_payment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM payment_submissions previous
            WHERE previous.id = NEW.supersedes_payment_id
            AND previous.license_id = NEW.license_id
            AND previous.release_id = NEW.release_id
            AND previous.state = 'rejected')
            BEGIN SELECT RAISE(ABORT,
            'payment supersession requires rejected payment'); END""",
        ),
        "payment_supersession_is_rejected_update": _trigger(
            "payment_submissions",
            """CREATE TRIGGER payment_supersession_is_rejected_update
            BEFORE UPDATE OF supersedes_payment_id, license_id, release_id
            ON payment_submissions
            WHEN NEW.supersedes_payment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM payment_submissions previous
            WHERE previous.id = NEW.supersedes_payment_id
            AND previous.license_id = NEW.license_id
            AND previous.release_id = NEW.release_id
            AND previous.state = 'rejected')
            BEGIN SELECT RAISE(ABORT,
            'payment supersession requires rejected payment'); END""",
        ),
    }
)


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


def _paths_overlap(left: Path, right: Path) -> bool:
    left_abs = canonical_safe_path(left)
    right_abs = canonical_safe_path(right)
    return (
        left_abs == right_abs
        or left_abs in right_abs.parents
        or right_abs in left_abs.parents
    )


def portable_evidence_relpath(relative: Path) -> str:
    """Return one cross-platform-safe evidence path or fail closed."""

    parts = relative.parts
    if not parts:
        raise UnsafePathError("empty evidence path")
    for component in parts:
        if _PORTABLE_COMPONENT.fullmatch(component) is None:
            raise UnsafePathError(f"non-portable evidence path component: {component!r}")
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise UnsafePathError(f"reserved evidence path component: {component!r}")
    rendered = "/".join(parts)
    if len(rendered) > MAX_MANIFEST_RELPATH_LENGTH:
        raise UnsafePathError("evidence path exceeds manifest limit")
    return rendered


def _regular_single_link(path: Path, *, label: str) -> os.stat_result:
    validate_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"{label} is not a regular file: {path}")
    if info.st_nlink != 1:
        raise UnsafePathError(f"{label} has multiple hard links: {path}")
    return info


def _index_columns(
    connection: sqlite3.Connection,
    index_name: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
            (index_name,),
        ).fetchall()
    )


def _validate_application_schema(
    connection: sqlite3.Connection,
    database_name: str,
) -> None:
    expected_columns = EXPECTED_DATABASE_COLUMNS[database_name]
    expected_primary_keys = EXPECTED_DATABASE_PRIMARY_KEYS[database_name]
    expected_unique_keys = EXPECTED_DATABASE_UNIQUE_KEYS[database_name]

    actual_named_indexes: dict[str, _SchemaIndex] = {}
    for table_name, table_columns in expected_columns.items():
        column_rows = connection.execute(
            "SELECT name, type, pk FROM pragma_table_info(?)",
            (table_name,),
        ).fetchall()
        actual_columns = {
            str(row[0]): str(row[1]).strip().upper() for row in column_rows
        }
        if actual_columns != table_columns:
            raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")

        actual_primary_key = tuple(
            str(row[0])
            for row in sorted(
                ((row[0], int(row[2])) for row in column_rows if int(row[2]) > 0),
                key=lambda item: item[1],
            )
        )
        if actual_primary_key != expected_primary_keys[table_name]:
            raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")

        actual_unique_keys: set[tuple[str, ...]] = set()
        for index_row in connection.execute(
            'SELECT name, "unique", origin, partial FROM pragma_index_list(?)',
            (table_name,),
        ).fetchall():
            index_name = str(index_row[0])
            unique = bool(index_row[1])
            origin = str(index_row[2])
            partial = bool(index_row[3])
            columns = _index_columns(connection, index_name)
            if origin == "u":
                if not unique or partial:
                    raise RuntimeError(
                        f"SQLite backup schema is invalid: {database_name}"
                    )
                actual_unique_keys.add(columns)
            elif origin == "c":
                sql_row = connection.execute(
                    "SELECT tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = ?",
                    (index_name,),
                ).fetchone()
                if sql_row is None or not isinstance(sql_row[1], str):
                    raise RuntimeError(
                        f"SQLite backup schema is invalid: {database_name}"
                    )
                actual_named_indexes[index_name] = _SchemaIndex(
                    str(sql_row[0]),
                    columns,
                    unique,
                    partial,
                    _normalize_schema_sql(sql_row[1]),
                )
            elif origin != "pk":
                raise RuntimeError(
                    f"SQLite backup schema is invalid: {database_name}"
                )
        if frozenset(actual_unique_keys) != expected_unique_keys[table_name]:
            raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")

    if actual_named_indexes != EXPECTED_DATABASE_INDEXES[database_name]:
        raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")

    trigger_rows = connection.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()
    if any(not isinstance(sql, str) for _name, _table, sql in trigger_rows):
        raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")
    actual_triggers = {
        str(name): _SchemaTrigger(str(table), _normalize_schema_sql(sql))
        for name, table, sql in trigger_rows
    }
    if actual_triggers != EXPECTED_DATABASE_TRIGGERS[database_name]:
        raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")


def validate_database_snapshot(path: Path, database_name: str) -> None:
    """Require integrity plus the complete expected application schema contract."""

    expected_tables = EXPECTED_DATABASE_TABLES.get(database_name)
    if expected_tables is None:
        raise RuntimeError(f"unknown backend database: {database_name}")
    uri = f"{path.absolute().as_uri()}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if tables == expected_tables:
            _validate_application_schema(connection, database_name)
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite backup is unreadable: {path.name}") from exc
    finally:
        if connection is not None:
            connection.close()
    if not row or str(row[0]) != "ok":
        raise RuntimeError(f"SQLite backup failed integrity_check: {path.name}")
    if tables != expected_tables:
        raise RuntimeError(f"SQLite backup schema is invalid: {database_name}")
    if (
        database_name == "commerce.sqlite3"
        and user_version != EXPECTED_COMMERCE_SCHEMA_VERSION
    ):
        raise RuntimeError("commerce backup schema version is invalid")


def _online_backup(src_path: Path, dest_path: Path, *, database_name: str) -> None:
    before = _regular_single_link(src_path, label="database source")
    if before.st_size > MAX_DATABASE_BYTES:
        raise ValueError(f"database exceeds backup limit: {src_path.name}")
    validate_write_target(dest_path)

    # Path.as_uri() percent-encodes '?' and '#' in legitimate file names.  A
    # hand-built ``file:{path}?mode=ro`` URI mis-parses those names as options.
    source_uri = f"{src_path.absolute().as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, isolation_level=None)
    try:
        destination = sqlite3.connect(dest_path, isolation_level=None)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    after = _regular_single_link(src_path, label="database source")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        dest_path.unlink(missing_ok=True)
        raise UnsafePathError("database source identity changed during backup")
    try:
        require_permission_applied(
            harden_file(dest_path),
            label=dest_path.name,
        )
    except PermissionError:
        dest_path.unlink(missing_ok=True)
        raise
    validate_database_snapshot(dest_path, database_name)


def _backup_evidence(source_root: Path, destination_root: Path) -> list[BackupEntry]:
    entries: list[BackupEntry] = []
    file_count = 0
    total_bytes = 0
    validate_directory(source_root)
    require_permission_applied(
        ensure_private_directory(destination_root),
        label="payment evidence backup directory",
    )

    for current, dirnames, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        validate_directory(current_path)
        dirnames.sort()
        filenames.sort()

        for dirname in list(dirnames):
            child = current_path / dirname
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or child.is_symlink():
                raise UnsafePathError(f"unsafe evidence directory: {child}")
            relative_dir = child.relative_to(source_root)
            require_permission_applied(
                ensure_private_directory(destination_root / relative_dir),
                label="payment evidence backup subdirectory",
            )

        for filename in filenames:
            source = current_path / filename
            info = _regular_single_link(source, label="evidence source")
            if info.st_size > MAX_EVIDENCE_FILE_BYTES:
                raise ValueError(f"payment evidence exceeds per-file limit: {source}")
            file_count += 1
            total_bytes += info.st_size
            if file_count > MAX_EVIDENCE_FILES:
                raise ValueError("payment evidence file-count limit exceeded")
            if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                raise ValueError("payment evidence aggregate-size limit exceeded")

            relative = source.relative_to(source_root)
            portable_relative = portable_evidence_relpath(relative)
            destination = destination_root / relative
            result = copy_stable_file(
                source,
                destination,
                max_bytes=MAX_EVIDENCE_FILE_BYTES,
                expected_size=info.st_size,
            )
            entries.append(
                BackupEntry(
                    f"{EVIDENCE_DIRNAME}/{portable_relative}",
                    result.sha256,
                    result.byte_size,
                )
            )
    return entries


def backup(
    data_dir: Path,
    backup_dir: Path,
    *,
    service_stopped: bool = False,
) -> BackupManifest:
    """Snapshot all backend databases and bounded payment evidence."""

    require_secure_ops_platform()
    if not service_stopped:
        raise RuntimeError(
            "backup requires explicit confirmation that the service is stopped"
        )
    data_dir = Path(data_dir)
    backup_dir = reject_repository_output_path(Path(backup_dir))
    validate_directory(data_dir)
    if _paths_overlap(data_dir, backup_dir):
        raise ValueError("data and backup directories must not overlap")

    database_sources: list[tuple[str, Path]] = []
    for name in KNOWN_DATABASES:
        source = data_dir / name
        try:
            info = _regular_single_link(source, label="database source")
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"required backend database is missing: {name}"
            ) from exc
        if info.st_size <= 0:
            raise ValueError(f"required backend database is empty: {name}")
        database_sources.append((name, source))

    evidence_src = data_dir / EVIDENCE_DIRNAME
    try:
        evidence_info = evidence_src.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "required payment-evidence directory is missing"
        ) from exc
    if not stat.S_ISDIR(evidence_info.st_mode) or evidence_src.is_symlink():
        raise UnsafePathError("payment-evidence source is not a safe directory")
    validate_directory(evidence_src)

    require_permission_applied(
        ensure_private_directory(backup_dir),
        label="backup directory",
    )
    if any(backup_dir.iterdir()):
        raise FileExistsError(f"backup directory is not empty: {backup_dir}")

    entries: list[BackupEntry] = []
    for name, source in database_sources:
        target = backup_dir / name
        _online_backup(source, target, database_name=name)
        result = hash_stable_file(target, max_bytes=MAX_DATABASE_BYTES)
        entries.append(BackupEntry(name, result.sha256, result.byte_size))

    entries.extend(
        _backup_evidence(
            evidence_src,
            backup_dir / EVIDENCE_DIRNAME,
        )
    )

    manifest = BackupManifest(
        MANIFEST_SCHEMA,
        datetime.now(timezone.utc).isoformat(),
        tuple(entries),
    )
    collision_keys: dict[str, str] = {}
    for entry in manifest.entries:
        collision_key = unicodedata.normalize("NFC", entry.relpath).casefold()
        previous = collision_keys.get(collision_key)
        if previous is not None and previous != entry.relpath:
            raise ValueError("backup paths collide across supported filesystems")
        collision_keys[collision_key] = entry.relpath
    require_permission_applied(
        write_secret_text(backup_dir / MANIFEST_NAME, manifest.to_json()),
        label="backup manifest",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the JARVIS backend state.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--confirm-service-stopped", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_service_stopped:
        emit(
            "[fail] pass --confirm-service-stopped only during a real "
            "maintenance window"
        )
        return 2
    try:
        manifest = backup(
            args.data_dir,
            args.backup_dir,
            service_stopped=True,
        )
    except OpsNotAvailableError:
        emit("[not_available] secure backup is not implemented on this platform")
        return 1
    except Exception:  # noqa: BLE001 - keep filesystem details out of automation logs
        emit(
            "[fail] backup did not complete; partial private output may remain, "
            "must not be restored, and should be reviewed by the operator"
        )
        return 1
    emit(
        f"[ok] backed up {len(manifest.entries)} file(s) to {args.backup_dir} "
        f"at {manifest.created_at}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

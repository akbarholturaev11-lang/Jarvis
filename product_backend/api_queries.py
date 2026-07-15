"""Read-only SQLite query adapter for product backend/API projections."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from core.product_state import PaymentState
from core.product_version import ProductVersion, SemanticVersion
from core.release_manifest import ArtifactKind

from .api_ports import (
    AdminAccountPage,
    AdminAccountSummary,
    AdminLicensePage,
    AdminLicenseSummary,
    AdminReleasePage,
    AdminReleaseSummary,
    ArtifactTargetSummary,
    PaymentStatusRecord,
    ReleaseCatalogRecord,
)
from .models import (
    Account,
    ArtifactIdentity,
    DeviceBinding,
    Entitlement,
    License,
    PaymentSubmission,
    Release,
    ReleaseArtifact,
    ReleaseState,
    normalize_semver,
    normalize_target_architecture,
    normalize_target_platform,
    validate_build,
    validate_opaque_identifier,
)


_MAX_ADMIN_QUERY_OFFSET = 100_000
_MAX_ADMIN_ENTITLEMENTS_PER_LICENSE = 100


class ProductReadNotAvailableError(RuntimeError):
    """The read projection cannot be queried safely."""


class SQLiteProductReadStore:
    """Separate query projection over an existing commerce SQLite database.

    It opens the database in URI ``mode=ro`` for every query and never creates or
    migrates schema.  The command repository remains the only writer.
    """

    def __init__(self, database: str | os.PathLike[str]) -> None:
        path = Path(database).expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ProductReadNotAvailableError(
                "Product read database is not available."
            )
        self._database = path.resolve(strict=True)
        self._uri = f"file:{quote(str(self._database), safe='/')}?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._uri, uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Product read database is not available."
            ) from exc

    @staticmethod
    def _limit(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value

    @staticmethod
    def _offset(value: int) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_ADMIN_QUERY_OFFSET:
            raise ValueError(
                f"offset must be between 0 and {_MAX_ADMIN_QUERY_OFFSET}"
            )
        return value

    @classmethod
    def _pagination(cls, limit: int, offset: int) -> tuple[int, int]:
        return cls._limit(limit), cls._offset(offset)

    @staticmethod
    def _entitlements_limit(value: int) -> int:
        if (
            type(value) is not int
            or not 1 <= value <= _MAX_ADMIN_ENTITLEMENTS_PER_LICENSE
        ):
            raise ValueError(
                "entitlements_limit must be between 1 and "
                f"{_MAX_ADMIN_ENTITLEMENTS_PER_LICENSE}"
            )
        return value

    def list_published_releases(
        self,
        *,
        limit: int,
    ) -> tuple[ReleaseCatalogRecord, ...]:
        limit = self._limit(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM releases WHERE state = 'published'"
                ).fetchall()
                rows = sorted(
                    rows,
                    key=lambda row: SemanticVersion.parse(row["version"]),
                    reverse=True,
                )[:limit]
                records: list[ReleaseCatalogRecord] = []
                for row in rows:
                    artifact_rows = connection.execute(
                        "SELECT id, platform, architecture, artifact_kind, build, "
                        "byte_size, sha256, signature_verified_at, "
                        "verification_key_id FROM release_artifacts "
                        "WHERE release_id = ? ORDER BY platform, architecture, build",
                        (row["id"],),
                    ).fetchall()
                    artifacts = tuple(
                        ArtifactTargetSummary(
                            item["id"],
                            item["platform"],
                            item["architecture"],
                            item["artifact_kind"],
                            item["build"],
                            item["byte_size"],
                            item["sha256"],
                            item["signature_verified_at"],
                            item["verification_key_id"],
                        )
                        for item in artifact_rows
                    )
                    records.append(
                        ReleaseCatalogRecord(self._release(row), artifacts)
                    )
                return tuple(records)
        except (sqlite3.Error, ValueError) as exc:
            raise ProductReadNotAvailableError(
                "Published releases could not be read."
            ) from exc

    def get_release(self, release_id: str) -> Release | None:
        release_id = validate_opaque_identifier(release_id, field="release_id")
        return self._release_query("id", release_id)

    def get_release_by_version(self, version: str) -> Release | None:
        version = normalize_semver(version)
        return self._release_query("version", version)

    def _release_query(self, field: str, value: str) -> Release | None:
        if field not in {"id", "version"}:
            raise ProductReadNotAvailableError("Invalid release query.")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT * FROM releases WHERE {field} = ?", (value,)
                ).fetchone()
            return None if row is None else self._release(row)
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Release could not be read."
            ) from exc

    def list_release_artifacts(
        self,
        release_id: str,
    ) -> tuple[ReleaseArtifact, ...]:
        release_id = validate_opaque_identifier(release_id, field="release_id")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT a.*, r.version FROM release_artifacts a "
                    "JOIN releases r ON r.id = a.release_id "
                    "WHERE a.release_id = ? ORDER BY a.platform, a.architecture, "
                    "a.build",
                    (release_id,),
                ).fetchall()
                return tuple(
                    self._artifact(connection, row) for row in rows
                )
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Release artifacts could not be read."
            ) from exc

    def list_payments(
        self,
        *,
        state: PaymentState | None,
        limit: int,
    ) -> tuple[PaymentStatusRecord, ...]:
        limit = self._limit(limit)
        if state is not None and type(state) is not PaymentState:
            raise ValueError("state must be a PaymentState")
        where = "" if state is None else "WHERE p.state = ?"
        parameters: tuple[object, ...] = () if state is None else (state.value,)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT p.*, r.version FROM payment_submissions p "
                    "JOIN releases r ON r.id = p.release_id "
                    f"{where} ORDER BY p.submitted_at DESC, p.rowid DESC LIMIT ?",
                    (*parameters, limit),
                ).fetchall()
            return tuple(self._payment_record(row) for row in rows)
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Payments could not be read."
            ) from exc

    def get_payment(
        self,
        payment_id: str,
        *,
        license_id: str | None = None,
    ) -> PaymentStatusRecord | None:
        payment_id = validate_opaque_identifier(payment_id, field="payment_id")
        if license_id is not None:
            license_id = validate_opaque_identifier(
                license_id, field="license_id"
            )
        where = "p.id = ?" if license_id is None else "p.id = ? AND p.license_id = ?"
        params = (payment_id,) if license_id is None else (payment_id, license_id)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT p.*, r.version FROM payment_submissions p "
                    "JOIN releases r ON r.id = p.release_id WHERE " + where,
                    params,
                ).fetchone()
            return None if row is None else self._payment_record(row)
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Payment could not be read."
            ) from exc

    def get_latest_payment_for_release(
        self,
        license_id: str,
        release_id: str,
    ) -> PaymentStatusRecord | None:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        release_id = validate_opaque_identifier(release_id, field="release_id")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT p.*, r.version FROM payment_submissions p "
                    "JOIN releases r ON r.id = p.release_id "
                    "WHERE p.license_id = ? AND p.release_id = ? "
                    "ORDER BY p.submitted_at DESC, p.rowid DESC LIMIT 1",
                    (license_id, release_id),
                ).fetchone()
            return None if row is None else self._payment_record(row)
        except sqlite3.Error as exc:
            raise ProductReadNotAvailableError(
                "Payment status could not be read."
            ) from exc

    def find_update_candidate(
        self,
        *,
        platform: str,
        architecture: str,
        installed_version: str,
        installed_build: int,
    ) -> ReleaseArtifact | None:
        """Return the newest published compatible update for one exact target."""

        platform = normalize_target_platform(platform)
        architecture = normalize_target_architecture(architecture)
        installed_version = normalize_semver(installed_version)
        installed_build = validate_build(installed_build)
        installed = ProductVersion(
            SemanticVersion.parse(installed_version), installed_build
        )
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT a.*, r.version FROM release_artifacts a "
                    "JOIN releases r ON r.id = a.release_id "
                    "WHERE r.state = 'published' AND a.platform = ? "
                    "AND a.architecture = ? AND a.artifact_kind = ?",
                    (platform, architecture, ArtifactKind.UPDATE_PACKAGE.value),
                ).fetchall()
                compatible: list[tuple[ProductVersion, ReleaseArtifact]] = []
                for row in rows:
                    artifact = self._artifact(connection, row)
                    target = ProductVersion(
                        SemanticVersion.parse(artifact.identity.version),
                        artifact.identity.build,
                    )
                    if not target.is_newer_than(installed):
                        continue
                    if (
                        target.version != installed.version
                        and installed_version
                        not in artifact.compatible_source_versions
                    ):
                        continue
                    compatible.append((target, artifact))
                if not compatible:
                    return None
                compatible.sort(
                    key=lambda item: (item[0].version, item[0].build),
                    reverse=True,
                )
                return compatible[0][1]
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise ProductReadNotAvailableError(
                "Update candidate could not be read."
            ) from exc

    def list_admin_accounts(
        self,
        *,
        limit: int,
        offset: int,
    ) -> AdminAccountPage:
        """Return one deterministic, bounded account page for the admin UI."""

        limit, offset = self._pagination(limit, offset)
        try:
            with self._connect() as connection:
                total = int(
                    connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT a.*, "
                    "(SELECT COUNT(*) FROM licenses l "
                    " WHERE l.account_id = a.id) AS license_count, "
                    "(SELECT COUNT(*) FROM device_bindings d "
                    " JOIN licenses l ON l.id = d.license_id "
                    " WHERE l.account_id = a.id "
                    " AND d.deactivated_at IS NULL) AS active_device_count "
                    "FROM accounts a "
                    "ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            records = tuple(
                AdminAccountSummary(
                    self._account(row),
                    int(row["license_count"]),
                    int(row["active_device_count"]),
                )
                for row in rows
            )
            return AdminAccountPage(records, total, limit, offset)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise ProductReadNotAvailableError(
                "Admin accounts could not be read."
            ) from exc

    def list_admin_licenses(
        self,
        *,
        account_id: str | None,
        limit: int,
        offset: int,
        entitlements_limit: int,
    ) -> AdminLicensePage:
        """Return licenses with active-device and bounded entitlement summaries."""

        limit, offset = self._pagination(limit, offset)
        entitlements_limit = self._entitlements_limit(entitlements_limit)
        if account_id is not None:
            account_id = validate_opaque_identifier(
                account_id, field="account_id"
            )
        where = "" if account_id is None else "WHERE l.account_id = ?"
        parameters: tuple[object, ...] = () if account_id is None else (account_id,)
        try:
            with self._connect() as connection:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM licenses l " + where,
                        parameters,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT l.*, a.external_subject FROM licenses l "
                    "JOIN accounts a ON a.id = l.account_id "
                    f"{where} ORDER BY l.created_at DESC, l.id DESC "
                    "LIMIT ? OFFSET ?",
                    (*parameters, limit, offset),
                ).fetchall()
                if not rows:
                    return AdminLicensePage((), total, limit, offset)

                license_ids = tuple(str(row["id"]) for row in rows)
                placeholders = ",".join("?" for _ in license_ids)
                active_rows = connection.execute(
                    "SELECT * FROM device_bindings "
                    f"WHERE license_id IN ({placeholders}) "
                    "AND deactivated_at IS NULL",
                    license_ids,
                ).fetchall()
                active_devices = {
                    str(row["license_id"]): self._device(row)
                    for row in active_rows
                }

                entitlement_rows = connection.execute(
                    "SELECT * FROM ("
                    " SELECT e.*, r.version, "
                    " COUNT(*) OVER (PARTITION BY e.license_id) "
                    " AS entitlement_count, "
                    " ROW_NUMBER() OVER (PARTITION BY e.license_id "
                    " ORDER BY e.granted_at DESC, e.id DESC) "
                    " AS entitlement_position "
                    " FROM entitlements e "
                    " JOIN releases r ON r.id = e.release_id "
                    f" WHERE e.license_id IN ({placeholders})"
                    ") WHERE entitlement_position <= ? "
                    "ORDER BY license_id, entitlement_position",
                    (*license_ids, entitlements_limit),
                ).fetchall()

            entitlement_map: dict[str, list[Entitlement]] = {
                license_id: [] for license_id in license_ids
            }
            entitlement_counts: dict[str, int] = {
                license_id: 0 for license_id in license_ids
            }
            for row in entitlement_rows:
                license_id = str(row["license_id"])
                entitlement_map[license_id].append(self._entitlement(row))
                entitlement_counts[license_id] = int(row["entitlement_count"])

            records = tuple(
                AdminLicenseSummary(
                    self._license(row),
                    str(row["external_subject"]),
                    active_devices.get(str(row["id"])),
                    tuple(entitlement_map[str(row["id"])]),
                    entitlement_counts[str(row["id"])],
                )
                for row in rows
            )
            return AdminLicensePage(records, total, limit, offset)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise ProductReadNotAvailableError(
                "Admin licenses could not be read."
            ) from exc

    def list_admin_releases(
        self,
        *,
        limit: int,
        offset: int,
    ) -> AdminReleasePage:
        """Return drafts and published releases without private artifact data."""

        limit, offset = self._pagination(limit, offset)
        try:
            with self._connect() as connection:
                total = int(
                    connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT r.*, "
                    "(SELECT COUNT(*) FROM release_artifacts a "
                    " WHERE a.release_id = r.id) AS artifact_count "
                    "FROM releases r "
                    "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            records = tuple(
                AdminReleaseSummary(
                    self._release(row),
                    int(row["artifact_count"]),
                )
                for row in rows
            )
            return AdminReleasePage(records, total, limit, offset)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise ProductReadNotAvailableError(
                "Admin releases could not be read."
            ) from exc

    @staticmethod
    def _release(row: sqlite3.Row) -> Release:
        return Release(
            row["id"],
            row["version"],
            ReleaseState(row["state"]),
            row["price_minor"],
            row["currency"],
            row["created_at"],
            row["published_at"],
            row["features_en"],
            row["features_ru"],
            row["fixes_en"],
            row["fixes_ru"],
        )

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(row["id"], row["external_subject"], row["created_at"])

    @staticmethod
    def _license(row: sqlite3.Row) -> License:
        return License(
            row["id"],
            row["account_id"],
            row["plan_code"],
            row["created_at"],
        )

    @staticmethod
    def _device(row: sqlite3.Row) -> DeviceBinding:
        return DeviceBinding(
            row["id"],
            row["license_id"],
            row["device_key_fingerprint"],
            row["platform"],
            row["architecture"],
            row["device_label"],
            row["activated_at"],
            row["deactivated_at"],
            row["replaced_by_binding_id"],
            row["replacement_reason"],
        )

    @staticmethod
    def _entitlement(row: sqlite3.Row) -> Entitlement:
        return Entitlement(
            row["id"],
            row["license_id"],
            row["release_id"],
            row["version"],
            row["granted_by_payment_id"],
            row["granted_at"],
        )

    @staticmethod
    def _payment_record(row: sqlite3.Row) -> PaymentStatusRecord:
        payment = PaymentSubmission(
            row["id"],
            row["license_id"],
            row["release_id"],
            row["amount_minor"],
            row["currency"],
            row["screenshot_storage_key"],
            row["screenshot_sha256"],
            row["screenshot_byte_size"],
            row["screenshot_mime_type"],
            row["paid_at"],
            row["submitted_at"],
            PaymentState(row["state"]),
            row["review_started_at"],
            row["review_started_by"],
            row["decided_at"],
            row["decided_by"],
            row["rejection_reason"],
        )
        return PaymentStatusRecord(payment, row["version"])

    @staticmethod
    def _artifact(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ReleaseArtifact:
        source_rows = connection.execute(
            "SELECT source_version FROM artifact_compatible_sources "
            "WHERE artifact_id = ? ORDER BY source_version",
            (row["id"],),
        ).fetchall()
        return ReleaseArtifact(
            row["id"],
            row["release_id"],
            ArtifactIdentity(
                row["version"],
                row["platform"],
                row["architecture"],
                row["build"],
                ArtifactKind(row["artifact_kind"]),
            ),
            row["sha256"],
            row["byte_size"],
            row["storage_key"],
            row["signature"],
            row["signing_key_id"],
            row["signature_verified_at"],
            row["verification_key_id"],
            tuple(item["source_version"] for item in source_rows),
            row["created_at"],
        )


__all__ = ["ProductReadNotAvailableError", "SQLiteProductReadStore"]

"""SQLite adapter for the platform-neutral JARVIS commerce repository.

SQLite is the local/test persistence adapter.  Domain-facing methods and opaque
storage references are intentionally portable to a later PostgreSQL + private
object-storage implementation.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.product_state import PaymentState
from core.release_manifest import ArtifactKind
from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    ProductVersion,
    SemanticVersion,
    require_monotonic_upgrade,
)

from .models import (
    SINGLE_PAID_PLAN_CODE,
    Account,
    AdminDecisionAudit,
    AdminDecisionKind,
    ApprovalResult,
    ArtifactVerificationCandidate,
    ArtifactVerificationError,
    ArtifactVerificationReceipt,
    ArtifactVerifier,
    ArtifactIdentity,
    ConflictError,
    DeviceBinding,
    Entitlement,
    InstallAuthorization,
    InstallDecisionReason,
    InstallMode,
    InitialPurchaseResult,
    InvalidTransitionError,
    License,
    NotFoundError,
    PaymentSubmission,
    PersistenceInvariantError,
    Release,
    ReleaseArtifact,
    ReleaseState,
    ValidationError,
    VerifiedDevicePrincipal,
    format_utc_timestamp,
    normalize_semver,
    normalize_target_architecture,
    normalize_target_platform,
    normalize_utc_timestamp,
    sanitize_human_text,
    validate_build,
    validate_byte_size,
    validate_currency,
    validate_device_key_fingerprint,
    validate_minor_amount,
    validate_opaque_identifier,
    validate_payment_screenshot_mime_type,
    validate_payment_screenshot_size,
    validate_sha256,
    validate_signature,
    validate_storage_key,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    external_subject TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS licenses (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    plan_code TEXT NOT NULL CHECK (plan_code = 'jarvis_single_paid'),
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS device_bindings (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE RESTRICT,
    device_key_fingerprint TEXT NOT NULL CHECK (
        length(device_key_fingerprint) = 71
        AND substr(device_key_fingerprint, 1, 7) = 'sha256:'
        AND device_key_fingerprint = lower(device_key_fingerprint)
        AND substr(device_key_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    platform TEXT NOT NULL CHECK (platform IN ('macos', 'windows', 'linux')),
    architecture TEXT NOT NULL,
    device_label TEXT,
    activated_at TEXT NOT NULL CHECK (substr(activated_at, -1) = 'Z'),
    deactivated_at TEXT CHECK (
        deactivated_at IS NULL OR substr(deactivated_at, -1) = 'Z'
    ),
    replaced_by_binding_id TEXT REFERENCES device_bindings(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    replacement_reason TEXT,
    CHECK (
        (deactivated_at IS NULL AND replaced_by_binding_id IS NULL
            AND replacement_reason IS NULL)
        OR
        (deactivated_at IS NOT NULL AND replaced_by_binding_id IS NOT NULL
            AND replacement_reason IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_device_per_license
ON device_bindings(license_id)
WHERE deactivated_at IS NULL;

CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('draft', 'published')),
    price_minor INTEGER NOT NULL CHECK (price_minor > 0),
    currency TEXT NOT NULL CHECK (
        length(currency) = 3 AND currency = upper(currency)
    ),
    features_en TEXT NOT NULL DEFAULT '' CHECK (length(features_en) <= 4000),
    features_ru TEXT NOT NULL DEFAULT '' CHECK (length(features_ru) <= 4000),
    fixes_en TEXT NOT NULL DEFAULT '' CHECK (length(fixes_en) <= 4000),
    fixes_ru TEXT NOT NULL DEFAULT '' CHECK (length(fixes_ru) <= 4000),
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z'),
    published_at TEXT CHECK (
        published_at IS NULL OR substr(published_at, -1) = 'Z'
    ),
    CHECK (
        (state = 'draft' AND published_at IS NULL)
        OR (state = 'published' AND published_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS release_artifacts (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL CHECK (platform IN ('macos', 'windows', 'linux')),
    architecture TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (
        artifact_kind IN ('initial_installer', 'update_package')
    ),
    build INTEGER NOT NULL CHECK (build > 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    storage_key TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL CHECK (
        length(signature) = 86
        AND signature NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    signing_key_id TEXT NOT NULL,
    signature_verified_at TEXT NOT NULL CHECK (
        substr(signature_verified_at, -1) = 'Z'
    ),
    verification_key_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (substr(created_at, -1) = 'Z'),
    UNIQUE (release_id, platform, architecture, artifact_kind, build),
    UNIQUE (platform, architecture, artifact_kind, build),
    CHECK (verification_key_id = signing_key_id)
);

CREATE TABLE IF NOT EXISTS artifact_compatible_sources (
    artifact_id TEXT NOT NULL REFERENCES release_artifacts(id) ON DELETE CASCADE,
    source_version TEXT NOT NULL,
    PRIMARY KEY (artifact_id, source_version)
);

CREATE TABLE IF NOT EXISTS payment_submissions (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE RESTRICT,
    release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL CHECK (
        length(currency) = 3 AND currency = upper(currency)
    ),
    screenshot_storage_key TEXT NOT NULL UNIQUE CHECK (
        instr(screenshot_storage_key, '://') = 0
        AND substr(screenshot_storage_key, 1, 1) != '/'
    ),
    screenshot_sha256 TEXT NOT NULL CHECK (
        length(screenshot_sha256) = 64
        AND screenshot_sha256 = lower(screenshot_sha256)
        AND screenshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    screenshot_byte_size INTEGER NOT NULL CHECK (
        screenshot_byte_size > 0 AND screenshot_byte_size <= 10485760
    ),
    screenshot_mime_type TEXT NOT NULL CHECK (
        screenshot_mime_type IN ('image/png', 'image/jpeg', 'image/webp')
    ),
    paid_at TEXT NOT NULL CHECK (substr(paid_at, -1) = 'Z'),
    submitted_at TEXT NOT NULL CHECK (substr(submitted_at, -1) = 'Z'),
    client_submission_id TEXT NOT NULL CHECK (
        length(client_submission_id) BETWEEN 3 AND 128
    ),
    supersedes_payment_id TEXT REFERENCES payment_submissions(id)
        ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'under_review', 'approved', 'rejected')
    ),
    review_started_at TEXT,
    review_started_by TEXT,
    decided_at TEXT,
    decided_by TEXT,
    rejection_reason TEXT,
    UNIQUE (id, license_id, release_id),
    CHECK (
        supersedes_payment_id IS NULL OR supersedes_payment_id != id
    ),
    CHECK (
        (state = 'pending'
            AND review_started_at IS NULL AND review_started_by IS NULL
            AND decided_at IS NULL AND decided_by IS NULL
            AND rejection_reason IS NULL)
        OR
        (state = 'under_review'
            AND review_started_at IS NOT NULL AND review_started_by IS NOT NULL
            AND decided_at IS NULL AND decided_by IS NULL
            AND rejection_reason IS NULL)
        OR
        (state = 'approved'
            AND review_started_at IS NOT NULL AND review_started_by IS NOT NULL
            AND decided_at IS NOT NULL AND decided_by IS NOT NULL
            AND rejection_reason IS NULL)
        OR
        (state = 'rejected'
            AND review_started_at IS NOT NULL AND review_started_by IS NOT NULL
            AND decided_at IS NOT NULL AND decided_by IS NOT NULL
            AND rejection_reason IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_payment_per_license_release
ON payment_submissions(license_id, release_id)
WHERE state IN ('pending', 'under_review');

CREATE UNIQUE INDEX IF NOT EXISTS one_approved_payment_per_license_release
ON payment_submissions(license_id, release_id)
WHERE state = 'approved';

CREATE TABLE IF NOT EXISTS entitlements (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE RESTRICT,
    release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
    granted_by_payment_id TEXT NOT NULL UNIQUE,
    granted_at TEXT NOT NULL CHECK (substr(granted_at, -1) = 'Z'),
    UNIQUE (license_id, release_id),
    FOREIGN KEY (granted_by_payment_id, license_id, release_id)
        REFERENCES payment_submissions(id, license_id, release_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS entitlement_requires_approved_payment
BEFORE INSERT ON entitlements
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM payment_submissions p
    WHERE p.id = NEW.granted_by_payment_id
      AND p.license_id = NEW.license_id
      AND p.release_id = NEW.release_id
      AND p.state = 'approved'
)
BEGIN
    SELECT RAISE(ABORT, 'entitlement requires approved payment');
END;

CREATE TABLE IF NOT EXISTS admin_decision_audits (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL UNIQUE
        REFERENCES payment_submissions(id) ON DELETE RESTRICT,
    actor_admin_subject TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason TEXT,
    occurred_at TEXT NOT NULL CHECK (substr(occurred_at, -1) = 'Z'),
    CHECK (
        (decision = 'approved' AND reason IS NULL)
        OR (decision = 'rejected' AND reason IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS admin_decision_audits_are_append_only_update
BEFORE UPDATE ON admin_decision_audits
BEGIN
    SELECT RAISE(ABORT, 'admin decision audits are append-only');
END;

CREATE TRIGGER IF NOT EXISTS admin_decision_audits_are_append_only_delete
BEFORE DELETE ON admin_decision_audits
BEGIN
    SELECT RAISE(ABORT, 'admin decision audits are append-only');
END;

"""


class SQLiteCommerceRepository:
    """Transactional SQLite implementation of the commerce repository."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
        artifact_verifier: (
            ArtifactVerifier
            | Callable[[ArtifactVerificationCandidate], ArtifactVerificationReceipt]
            | None
        ) = None,
    ) -> None:
        self._database = str(database)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._artifact_verifier = artifact_verifier
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._database,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise PersistenceInvariantError("SQLite foreign keys are not enabled")
        self._connection.executescript(_SCHEMA)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply additive, fail-closed migrations through commerce schema v4."""

        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current > 4:
            raise PersistenceInvariantError("SQLite schema is newer than this runtime")
        release_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(releases)").fetchall()
        }
        release_additions = {
            "features_en": "TEXT NOT NULL DEFAULT '' CHECK (length(features_en) <= 4000)",
            "features_ru": "TEXT NOT NULL DEFAULT '' CHECK (length(features_ru) <= 4000)",
            "fixes_en": "TEXT NOT NULL DEFAULT '' CHECK (length(fixes_en) <= 4000)",
            "fixes_ru": "TEXT NOT NULL DEFAULT '' CHECK (length(fixes_ru) <= 4000)",
        }
        with self._transaction():
            for name, definition in release_additions.items():
                if name not in release_columns:
                    self._connection.execute(
                        f"ALTER TABLE releases ADD COLUMN {name} {definition}"
                    )

            payment_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(payment_submissions)"
                ).fetchall()
            }
            if "client_submission_id" not in payment_columns:
                # SQLite cannot add a non-constant per-row default. Backfill each
                # historical row with its already-unique opaque payment id, then
                # enforce non-null writes with triggers below.
                self._connection.execute(
                    "ALTER TABLE payment_submissions "
                    "ADD COLUMN client_submission_id TEXT"
                )
            self._connection.execute(
                "UPDATE payment_submissions SET client_submission_id = "
                "'legacy:' || id WHERE client_submission_id IS NULL"
            )
            if "supersedes_payment_id" not in payment_columns:
                self._connection.execute(
                    "ALTER TABLE payment_submissions ADD COLUMN "
                    "supersedes_payment_id TEXT REFERENCES payment_submissions(id) "
                    "ON DELETE RESTRICT CHECK ("
                    "supersedes_payment_id IS NULL OR supersedes_payment_id != id)"
                )

            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "payment_client_submission_identity "
                "ON payment_submissions(license_id, client_submission_id)"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "payment_single_successor "
                "ON payment_submissions(supersedes_payment_id) "
                "WHERE supersedes_payment_id IS NOT NULL"
            )
            triggers = (
                "CREATE TRIGGER IF NOT EXISTS "
                "payment_submission_identity_required_insert "
                "BEFORE INSERT ON payment_submissions "
                "WHEN NEW.client_submission_id IS NULL "
                "OR length(NEW.client_submission_id) NOT BETWEEN 3 AND 128 "
                "BEGIN SELECT RAISE(ABORT, "
                "'payment submission identity is required'); END",
                "CREATE TRIGGER IF NOT EXISTS "
                "payment_submission_identity_required_update "
                "BEFORE UPDATE OF client_submission_id ON payment_submissions "
                "WHEN NEW.client_submission_id IS NULL "
                "OR length(NEW.client_submission_id) NOT BETWEEN 3 AND 128 "
                "BEGIN SELECT RAISE(ABORT, "
                "'payment submission identity is required'); END",
                "CREATE TRIGGER IF NOT EXISTS payment_supersession_is_rejected_insert "
                "BEFORE INSERT ON payment_submissions "
                "WHEN NEW.supersedes_payment_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM payment_submissions previous "
                "WHERE previous.id = NEW.supersedes_payment_id "
                "AND previous.license_id = NEW.license_id "
                "AND previous.release_id = NEW.release_id "
                "AND previous.state = 'rejected') "
                "BEGIN SELECT RAISE(ABORT, "
                "'payment supersession requires rejected payment'); END",
                "CREATE TRIGGER IF NOT EXISTS payment_supersession_is_rejected_update "
                "BEFORE UPDATE OF supersedes_payment_id, license_id, release_id "
                "ON payment_submissions "
                "WHEN NEW.supersedes_payment_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM payment_submissions previous "
                "WHERE previous.id = NEW.supersedes_payment_id "
                "AND previous.license_id = NEW.license_id "
                "AND previous.release_id = NEW.release_id "
                "AND previous.state = 'rejected') "
                "BEGIN SELECT RAISE(ABORT, "
                "'payment supersession requires rejected payment'); END",
            )
            for statement in triggers:
                self._connection.execute(statement)
            self._connection.execute("PRAGMA user_version = 4")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteCommerceRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def _now(self) -> str:
        return format_utc_timestamp(self._clock())

    def create_account(self, external_subject: str) -> Account:
        subject = validate_opaque_identifier(
            external_subject, field="external_subject"
        )
        account_id = self._new_id("acct")
        created_at = self._now()
        try:
            with self._transaction():
                self._connection.execute(
                    "INSERT INTO accounts(id, external_subject, created_at) "
                    "VALUES (?, ?, ?)",
                    (account_id, subject, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("account subject already exists") from exc
        return Account(account_id, subject, created_at)

    def issue_license(self, account_id: str) -> License:
        account_id = validate_opaque_identifier(account_id, field="account_id")
        license_id = self._new_id("lic")
        created_at = self._now()
        try:
            with self._transaction():
                self._require_exists("accounts", account_id, "account")
                self._connection.execute(
                    "INSERT INTO licenses(id, account_id, plan_code, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        license_id,
                        account_id,
                        SINGLE_PAID_PLAN_CODE,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("license could not be issued") from exc
        return License(
            license_id,
            account_id,
            SINGLE_PAID_PLAN_CODE,
            created_at,
        )

    def activate_device(
        self,
        license_id: str,
        device_key_fingerprint: str,
        *,
        platform: str,
        architecture: str,
        device_label: str | None = None,
    ) -> DeviceBinding:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
        platform = normalize_target_platform(platform)
        architecture = normalize_target_architecture(architecture)
        label = self._optional_label(device_label)
        binding_id = self._new_id("dev")
        activated_at = self._now()
        try:
            with self._transaction():
                self._require_exists("licenses", license_id, "license")
                active = self._active_device_row(license_id)
                if active is not None:
                    if (
                        active["device_key_fingerprint"] == fingerprint
                        and active["platform"] == platform
                        and active["architecture"] == architecture
                    ):
                        return self._device_from_row(active)
                    raise ConflictError(
                        "license already has an active device; replace it explicitly"
                    )
                self._connection.execute(
                    "INSERT INTO device_bindings("
                    "id, license_id, device_key_fingerprint, platform, "
                    "architecture, device_label, activated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        binding_id,
                        license_id,
                        fingerprint,
                        platform,
                        architecture,
                        label,
                        activated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("device could not be activated") from exc
        return DeviceBinding(
            binding_id,
            license_id,
            fingerprint,
            platform,
            architecture,
            label,
            activated_at,
            None,
            None,
            None,
        )

    def replace_device(
        self,
        license_id: str,
        *,
        current_device_key_fingerprint: str,
        new_device_key_fingerprint: str,
        new_platform: str,
        new_architecture: str,
        replacement_reason: str,
        new_device_label: str | None = None,
    ) -> DeviceBinding:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        current_fingerprint = validate_device_key_fingerprint(
            current_device_key_fingerprint
        )
        new_fingerprint = validate_device_key_fingerprint(
            new_device_key_fingerprint
        )
        new_platform = normalize_target_platform(new_platform)
        new_architecture = normalize_target_architecture(new_architecture)
        if current_fingerprint == new_fingerprint:
            raise ValidationError("replacement requires a different device")
        reason = sanitize_human_text(
            replacement_reason,
            field="replacement_reason",
            max_length=240,
        )
        label = self._optional_label(new_device_label)
        binding_id = self._new_id("dev")
        changed_at = self._now()

        try:
            with self._transaction():
                self._require_exists("licenses", license_id, "license")
                active = self._active_device_row(license_id)
                if active is None:
                    raise InvalidTransitionError("license has no active device")
                if active["device_key_fingerprint"] != current_fingerprint:
                    raise InvalidTransitionError(
                        "current device does not match the active binding"
                    )
                self._connection.execute(
                    "UPDATE device_bindings SET deactivated_at = ?, "
                    "replaced_by_binding_id = ?, replacement_reason = ? "
                    "WHERE id = ? AND deactivated_at IS NULL",
                    (changed_at, binding_id, reason, active["id"]),
                )
                self._connection.execute(
                    "INSERT INTO device_bindings("
                    "id, license_id, device_key_fingerprint, platform, "
                    "architecture, device_label, activated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        binding_id,
                        license_id,
                        new_fingerprint,
                        new_platform,
                        new_architecture,
                        label,
                        changed_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("device replacement violated a constraint") from exc

        return DeviceBinding(
            binding_id,
            license_id,
            new_fingerprint,
            new_platform,
            new_architecture,
            label,
            changed_at,
            None,
            None,
            None,
        )

    def list_device_history(self, license_id: str) -> tuple[DeviceBinding, ...]:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        with self._lock:
            self._require_exists("licenses", license_id, "license")
            rows = self._connection.execute(
                "SELECT * FROM device_bindings WHERE license_id = ? "
                "ORDER BY activated_at, rowid",
                (license_id,),
            ).fetchall()
        return tuple(self._device_from_row(row) for row in rows)

    def get_active_device(self, license_id: str) -> DeviceBinding | None:
        """Return the current binding without revealing whether an account exists."""

        license_id = validate_opaque_identifier(license_id, field="license_id")
        with self._lock:
            row = self._active_device_row(license_id)
        return None if row is None else self._device_from_row(row)

    def create_release(
        self,
        version: str,
        *,
        price_minor: int,
        currency: str,
        features_en: str = "",
        features_ru: str = "",
        fixes_en: str = "",
        fixes_ru: str = "",
    ) -> Release:
        version = normalize_semver(version)
        price_minor = validate_minor_amount(price_minor)
        currency = validate_currency(currency)
        features_en = self._optional_release_text(features_en, field="features_en")
        features_ru = self._optional_release_text(features_ru, field="features_ru")
        fixes_en = self._optional_release_text(fixes_en, field="fixes_en")
        fixes_ru = self._optional_release_text(fixes_ru, field="fixes_ru")
        release_id = self._new_id("rel")
        created_at = self._now()
        try:
            with self._transaction():
                self._connection.execute(
                    "INSERT INTO releases("
                    "id, version, state, price_minor, currency, features_en, "
                    "features_ru, fixes_en, fixes_ru, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        release_id,
                        version,
                        ReleaseState.DRAFT.value,
                        price_minor,
                        currency,
                        features_en,
                        features_ru,
                        fixes_en,
                        fixes_ru,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("semantic version already exists") from exc
        return Release(
            release_id,
            version,
            ReleaseState.DRAFT,
            price_minor,
            currency,
            created_at,
            None,
            features_en,
            features_ru,
            fixes_en,
            fixes_ru,
        )

    def add_release_artifact(
        self,
        release_id: str,
        *,
        platform: str,
        architecture: str,
        artifact_kind: ArtifactKind = ArtifactKind.INITIAL_INSTALLER,
        build: int,
        sha256: str,
        byte_size: int,
        storage_key: str,
        signature: str,
        signing_key_id: str,
        compatible_source_versions: Sequence[str] = (),
    ) -> ReleaseArtifact:
        release_id = validate_opaque_identifier(release_id, field="release_id")
        platform = normalize_target_platform(platform)
        architecture = normalize_target_architecture(architecture)
        if type(artifact_kind) is not ArtifactKind:
            raise ValidationError("artifact_kind must be an ArtifactKind")
        build = validate_build(build)
        sha256 = validate_sha256(sha256)
        byte_size = validate_byte_size(byte_size)
        storage_key = validate_storage_key(storage_key, field="storage_key")
        signature = validate_signature(signature)
        signing_key_id = validate_opaque_identifier(
            signing_key_id, field="signing_key_id"
        )
        source_versions = self._normalize_source_versions(
            compatible_source_versions
        )
        with self._lock:
            release_preview = self._require_row(
                "releases", release_id, "release"
            )
            release_version = release_preview["version"]
        target_version = SemanticVersion.parse(release_version)
        if artifact_kind is ArtifactKind.INITIAL_INSTALLER and source_versions:
            raise ValidationError(
                "initial installer cannot declare compatible source versions"
            )
        if artifact_kind is ArtifactKind.UPDATE_PACKAGE and (
            not source_versions
            or any(
                SemanticVersion.parse(item) >= target_version
                for item in source_versions
            )
        ):
            raise ValidationError(
                "update source versions must be non-empty and older than the target"
            )
        candidate = ArtifactVerificationCandidate(
            product_id=PRODUCT_ID,
            bundle_id=BUNDLE_ID,
            release_version=release_version,
            platform=platform,
            architecture=architecture,
            artifact_kind=artifact_kind,
            build=build,
            sha256=sha256,
            byte_size=byte_size,
            storage_key=storage_key,
            signature=signature,
            signing_key_id=signing_key_id,
            compatible_source_versions=source_versions,
        )
        verification = self._verify_artifact_candidate(candidate)
        artifact_id = self._new_id("art")
        created_at = self._now()

        try:
            with self._transaction():
                release_row = self._require_row("releases", release_id, "release")
                if release_row["version"] != candidate.release_version:
                    raise PersistenceInvariantError(
                        "release version changed during artifact verification"
                    )
                candidate_product_version = ProductVersion.parse(
                    candidate.release_version, candidate.build
                )
                previous_rows = self._connection.execute(
                    "SELECT r.version, a.build FROM release_artifacts a "
                    "JOIN releases r ON r.id = a.release_id "
                    "WHERE a.platform = ? AND a.architecture = ? "
                    "AND a.artifact_kind = ?",
                    (platform, architecture, artifact_kind.value),
                ).fetchall()
                for previous_row in previous_rows:
                    try:
                        require_monotonic_upgrade(
                            ProductVersion.parse(
                                previous_row["version"], previous_row["build"]
                            ),
                            candidate_product_version,
                        )
                    except ValueError as exc:
                        raise InvalidTransitionError(
                            "artifact version/build is not globally monotonic "
                            "for this platform and architecture"
                        ) from exc
                self._connection.execute(
                    "INSERT INTO release_artifacts("
                    "id, release_id, platform, architecture, artifact_kind, "
                    "build, sha256, "
                    "byte_size, storage_key, signature, signing_key_id, "
                    "signature_verified_at, verification_key_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        release_id,
                        platform,
                        architecture,
                        artifact_kind.value,
                        build,
                        sha256,
                        byte_size,
                        storage_key,
                        signature,
                        signing_key_id,
                        verification.verified_at,
                        verification.verification_key_id,
                        created_at,
                    ),
                )
                self._connection.executemany(
                    "INSERT INTO artifact_compatible_sources("
                    "artifact_id, source_version) VALUES (?, ?)",
                    ((artifact_id, item) for item in source_versions),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("artifact identity or storage key already exists") from exc

        identity = ArtifactIdentity(
            release_row["version"],
            platform,
            architecture,
            build,
            artifact_kind,
        )
        return ReleaseArtifact(
            artifact_id,
            release_id,
            identity,
            sha256,
            byte_size,
            storage_key,
            signature,
            signing_key_id,
            verification.verified_at,
            verification.verification_key_id,
            source_versions,
            created_at,
        )

    def publish_release(self, release_id: str) -> Release:
        release_id = validate_opaque_identifier(release_id, field="release_id")
        published_at = self._now()
        with self._transaction():
            row = self._require_row("releases", release_id, "release")
            if row["state"] != ReleaseState.DRAFT.value:
                raise InvalidTransitionError("only a draft release can be published")
            artifact_counts = self._connection.execute(
                "SELECT count(*) AS total, "
                "sum(CASE WHEN signature_verified_at IS NOT NULL "
                "AND verification_key_id = signing_key_id THEN 1 ELSE 0 END) "
                "AS verified FROM release_artifacts WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if artifact_counts["total"] < 1:
                raise InvalidTransitionError(
                    "release cannot be published without an artifact"
                )
            if artifact_counts["verified"] != artifact_counts["total"]:
                raise InvalidTransitionError(
                    "release cannot be published with an unverified artifact"
                )
            self._connection.execute(
                "UPDATE releases SET state = ?, published_at = ? WHERE id = ?",
                (ReleaseState.PUBLISHED.value, published_at, release_id),
            )
            row = self._require_row("releases", release_id, "release")
        return self._release_from_row(row)

    def get_release(self, release_id: str) -> Release:
        release_id = validate_opaque_identifier(release_id, field="release_id")
        with self._lock:
            return self._release_from_row(
                self._require_row("releases", release_id, "release")
            )

    def submit_payment(
        self,
        license_id: str,
        release_id: str,
        *,
        screenshot_storage_key: str,
        screenshot_sha256: str,
        screenshot_byte_size: int,
        screenshot_mime_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
    ) -> PaymentSubmission:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        release_id = validate_opaque_identifier(release_id, field="release_id")
        client_submission_id = validate_opaque_identifier(
            client_submission_id, field="client_submission_id"
        )
        if supersedes_payment_id is not None:
            supersedes_payment_id = validate_opaque_identifier(
                supersedes_payment_id, field="supersedes_payment_id"
            )
        storage_key = validate_storage_key(
            screenshot_storage_key, field="screenshot_storage_key"
        )
        screenshot_sha256 = validate_sha256(screenshot_sha256)
        screenshot_byte_size = validate_payment_screenshot_size(
            screenshot_byte_size
        )
        screenshot_mime_type = validate_payment_screenshot_mime_type(
            screenshot_mime_type
        )
        paid_at = normalize_utc_timestamp(paid_at, field="paid_at")
        payment_id = self._new_id("pay")
        submitted_at = self._now()

        try:
            with self._transaction():
                self._require_exists("licenses", license_id, "license")
                release = self._require_row("releases", release_id, "release")
                row, _idempotent = self._submit_payment_locked(
                    payment_id=payment_id,
                    license_id=license_id,
                    release=release,
                    screenshot_storage_key=storage_key,
                    screenshot_sha256=screenshot_sha256,
                    screenshot_byte_size=screenshot_byte_size,
                    screenshot_mime_type=screenshot_mime_type,
                    paid_at=paid_at,
                    submitted_at=submitted_at,
                    client_submission_id=client_submission_id,
                    supersedes_payment_id=supersedes_payment_id,
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "an open payment or screenshot reference already exists"
            ) from exc
        return self._payment_from_row(row)

    def submit_initial_purchase(
        self,
        *,
        purchase_id: str,
        release_id: str,
        device_principal: VerifiedDevicePrincipal,
        screenshot_storage_key: str,
        screenshot_sha256: str,
        screenshot_byte_size: int,
        screenshot_mime_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
    ) -> InitialPurchaseResult:
        """Atomically enroll one proved device and submit non-entitled evidence.

        The caller must cryptographically prove the device before constructing
        ``device_principal``.  The opaque purchase identity is never stored raw;
        its digest deterministically reuses the same account/license on retries.
        No branch in this transaction creates an entitlement.
        """

        purchase_id = validate_opaque_identifier(purchase_id, field="purchase_id")
        release_id = validate_opaque_identifier(release_id, field="release_id")
        if (
            not isinstance(device_principal, VerifiedDevicePrincipal)
            or not device_principal.proof_verified
        ):
            raise ValidationError("verified device proof is required")
        client_submission_id = validate_opaque_identifier(
            client_submission_id, field="client_submission_id"
        )
        if supersedes_payment_id is not None:
            supersedes_payment_id = validate_opaque_identifier(
                supersedes_payment_id, field="supersedes_payment_id"
            )
        storage_key = validate_storage_key(
            screenshot_storage_key, field="screenshot_storage_key"
        )
        screenshot_sha256 = validate_sha256(screenshot_sha256)
        screenshot_byte_size = validate_payment_screenshot_size(
            screenshot_byte_size
        )
        screenshot_mime_type = validate_payment_screenshot_mime_type(
            screenshot_mime_type
        )
        paid_at = normalize_utc_timestamp(paid_at, field="paid_at")
        external_subject = "purchase:" + hashlib.sha256(
            purchase_id.encode("utf-8")
        ).hexdigest()
        account_id = self._new_id("acct")
        license_id = self._new_id("lic")
        binding_id = self._new_id("dev")
        payment_id = self._new_id("pay")
        created_at = self._now()

        try:
            with self._transaction():
                release = self._require_row("releases", release_id, "release")
                if release["state"] != ReleaseState.PUBLISHED.value:
                    raise InvalidTransitionError(
                        "initial purchase requires a published release"
                    )
                target_artifact = self._connection.execute(
                    "SELECT id FROM release_artifacts WHERE release_id = ? "
                    "AND platform = ? AND architecture = ? "
                    "AND artifact_kind = ? AND signature_verified_at IS NOT NULL "
                    "AND verification_key_id = signing_key_id LIMIT 1",
                    (
                        release_id,
                        device_principal.platform,
                        device_principal.architecture,
                        ArtifactKind.INITIAL_INSTALLER.value,
                    ),
                ).fetchone()
                if target_artifact is None:
                    raise InvalidTransitionError(
                        "initial purchase target is not available"
                    )

                account_row = self._connection.execute(
                    "SELECT * FROM accounts WHERE external_subject = ?",
                    (external_subject,),
                ).fetchone()
                if account_row is None:
                    self._connection.execute(
                        "INSERT INTO accounts(id, external_subject, created_at) "
                        "VALUES (?, ?, ?)",
                        (account_id, external_subject, created_at),
                    )
                    account_row = self._require_row(
                        "accounts", account_id, "purchase account"
                    )

                license_rows = self._connection.execute(
                    "SELECT * FROM licenses WHERE account_id = ? "
                    "ORDER BY created_at, rowid",
                    (account_row["id"],),
                ).fetchall()
                if len(license_rows) > 1:
                    raise PersistenceInvariantError(
                        "purchase account has multiple licenses"
                    )
                if license_rows:
                    license_row = license_rows[0]
                else:
                    self._connection.execute(
                        "INSERT INTO licenses(id, account_id, plan_code, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            license_id,
                            account_row["id"],
                            SINGLE_PAID_PLAN_CODE,
                            created_at,
                        ),
                    )
                    license_row = self._require_row(
                        "licenses", license_id, "purchase license"
                    )

                active = self._active_device_row(license_row["id"])
                if active is None:
                    self._connection.execute(
                        "INSERT INTO device_bindings("
                        "id, license_id, device_key_fingerprint, platform, "
                        "architecture, activated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            binding_id,
                            license_row["id"],
                            device_principal.device_key_fingerprint,
                            device_principal.platform,
                            device_principal.architecture,
                            created_at,
                        ),
                    )
                    active = self._connection.execute(
                        "SELECT * FROM device_bindings WHERE id = ?",
                        (binding_id,),
                    ).fetchone()
                elif (
                    active["device_key_fingerprint"]
                    != device_principal.device_key_fingerprint
                    or active["platform"] != device_principal.platform
                    or active["architecture"] != device_principal.architecture
                ):
                    raise ConflictError(
                        "purchase identity is bound to another device"
                    )
                if active is None:
                    raise PersistenceInvariantError(
                        "initial purchase device insert was not persisted"
                    )

                payment_row, idempotent = self._submit_payment_locked(
                    payment_id=payment_id,
                    license_id=license_row["id"],
                    release=release,
                    screenshot_storage_key=storage_key,
                    screenshot_sha256=screenshot_sha256,
                    screenshot_byte_size=screenshot_byte_size,
                    screenshot_mime_type=screenshot_mime_type,
                    paid_at=paid_at,
                    submitted_at=created_at,
                    client_submission_id=client_submission_id,
                    supersedes_payment_id=supersedes_payment_id,
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "initial purchase conflicts with current state"
            ) from exc

        return InitialPurchaseResult(
            self._account_from_row(account_row),
            self._license_from_row(license_row),
            self._device_from_row(active),
            self._payment_from_row(payment_row),
            idempotent,
        )

    def _submit_payment_locked(
        self,
        *,
        payment_id: str,
        license_id: str,
        release: sqlite3.Row,
        screenshot_storage_key: str,
        screenshot_sha256: str,
        screenshot_byte_size: int,
        screenshot_mime_type: str,
        paid_at: str,
        submitted_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None,
    ) -> tuple[sqlite3.Row, bool]:
        """Insert under an existing transaction or return an exact retry."""

        release_id = str(release["id"])
        existing = self._connection.execute(
            "SELECT * FROM payment_submissions WHERE license_id = ? "
            "AND client_submission_id = ?",
            (license_id, client_submission_id),
        ).fetchone()
        if existing is not None:
            same_request = (
                existing["release_id"] == release_id
                and existing["screenshot_sha256"] == screenshot_sha256
                and existing["screenshot_byte_size"] == screenshot_byte_size
                and existing["screenshot_mime_type"] == screenshot_mime_type
                and existing["paid_at"] == paid_at
                and existing["supersedes_payment_id"] == supersedes_payment_id
            )
            if not same_request:
                raise ConflictError(
                    "client submission identity was reused for another request"
                )
            return existing, True

        if release["state"] != ReleaseState.PUBLISHED.value:
            raise InvalidTransitionError(
                "payment can target only a published release"
            )
        if self._entitlement_row(license_id, release_id) is not None:
            raise ConflictError("license already owns this exact version")

        if supersedes_payment_id is None:
            rejected = self._connection.execute(
                "SELECT id FROM payment_submissions WHERE license_id = ? "
                "AND release_id = ? AND state = ? LIMIT 1",
                (license_id, release_id, PaymentState.REJECTED.value),
            ).fetchone()
            if rejected is not None:
                raise InvalidTransitionError(
                    "rejected payment must be resubmitted explicitly"
                )
        else:
            previous = self._require_row(
                "payment_submissions", supersedes_payment_id, "rejected payment"
            )
            if (
                previous["license_id"] != license_id
                or previous["release_id"] != release_id
                or previous["state"] != PaymentState.REJECTED.value
            ):
                raise InvalidTransitionError(
                    "resubmission must supersede a rejected payment for this purchase"
                )
            successor = self._connection.execute(
                "SELECT id FROM payment_submissions "
                "WHERE supersedes_payment_id = ? LIMIT 1",
                (supersedes_payment_id,),
            ).fetchone()
            if successor is not None:
                raise ConflictError("rejected payment was already resubmitted")

        self._connection.execute(
            "INSERT INTO payment_submissions("
            "id, license_id, release_id, amount_minor, currency, "
            "screenshot_storage_key, screenshot_sha256, "
            "screenshot_byte_size, screenshot_mime_type, paid_at, "
            "submitted_at, client_submission_id, supersedes_payment_id, state"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payment_id,
                license_id,
                release_id,
                release["price_minor"],
                release["currency"],
                screenshot_storage_key,
                screenshot_sha256,
                screenshot_byte_size,
                screenshot_mime_type,
                paid_at,
                submitted_at,
                client_submission_id,
                supersedes_payment_id,
                PaymentState.PENDING.value,
            ),
        )
        return self._require_row("payment_submissions", payment_id, "payment"), False

    def start_payment_review(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> PaymentSubmission:
        payment_id = validate_opaque_identifier(payment_id, field="payment_id")
        admin_subject = validate_opaque_identifier(
            admin_subject, field="admin_subject"
        )
        started_at = self._now()
        with self._transaction():
            row = self._require_row("payment_submissions", payment_id, "payment")
            if row["state"] == PaymentState.UNDER_REVIEW.value:
                if row["review_started_by"] == admin_subject:
                    return self._payment_from_row(row)
                raise ConflictError("payment is already under review by another admin")
            if row["state"] != PaymentState.PENDING.value:
                raise InvalidTransitionError(
                    "only a pending payment can enter review"
                )
            self._connection.execute(
                "UPDATE payment_submissions SET state = ?, "
                "review_started_at = ?, review_started_by = ? "
                "WHERE id = ? AND state = ?",
                (
                    PaymentState.UNDER_REVIEW.value,
                    started_at,
                    admin_subject,
                    payment_id,
                    PaymentState.PENDING.value,
                ),
            )
            row = self._require_row("payment_submissions", payment_id, "payment")
        return self._payment_from_row(row)

    def approve_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> ApprovalResult:
        """Atomically and idempotently approve and grant one exact version."""

        payment_id = validate_opaque_identifier(payment_id, field="payment_id")
        admin_subject = validate_opaque_identifier(
            admin_subject, field="admin_subject"
        )
        decided_at = self._now()
        try:
            with self._transaction():
                payment_row = self._require_row(
                    "payment_submissions", payment_id, "payment"
                )
                if payment_row["state"] == PaymentState.APPROVED.value:
                    entitlement_row = self._connection.execute(
                        "SELECT e.*, r.version FROM entitlements e "
                        "JOIN releases r ON r.id = e.release_id "
                        "WHERE e.granted_by_payment_id = ?",
                        (payment_id,),
                    ).fetchone()
                    audit_row = self._connection.execute(
                        "SELECT * FROM admin_decision_audits WHERE payment_id = ?",
                        (payment_id,),
                    ).fetchone()
                    if entitlement_row is None or audit_row is None:
                        raise PersistenceInvariantError(
                            "approved payment is missing entitlement or audit"
                        )
                    return ApprovalResult(
                        self._payment_from_row(payment_row),
                        self._entitlement_from_row(entitlement_row),
                        self._audit_from_row(audit_row),
                        True,
                    )
                if payment_row["state"] != PaymentState.UNDER_REVIEW.value:
                    raise InvalidTransitionError(
                        "only a payment under review can be approved"
                    )
                if payment_row["review_started_by"] != admin_subject:
                    raise ConflictError(
                        "only the admin who started review may approve payment"
                    )

                entitlement_id = self._new_id("ent")
                audit_id = self._new_id("audit")
                self._connection.execute(
                    "UPDATE payment_submissions SET state = ?, decided_at = ?, "
                    "decided_by = ? WHERE id = ? AND state = ?",
                    (
                        PaymentState.APPROVED.value,
                        decided_at,
                        admin_subject,
                        payment_id,
                        PaymentState.UNDER_REVIEW.value,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO entitlements("
                    "id, license_id, release_id, granted_by_payment_id, granted_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        entitlement_id,
                        payment_row["license_id"],
                        payment_row["release_id"],
                        payment_id,
                        decided_at,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO admin_decision_audits("
                    "id, payment_id, actor_admin_subject, decision, occurred_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        audit_id,
                        payment_id,
                        admin_subject,
                        AdminDecisionKind.APPROVED.value,
                        decided_at,
                    ),
                )
                payment_row = self._require_row(
                    "payment_submissions", payment_id, "payment"
                )
                entitlement_row = self._connection.execute(
                    "SELECT e.*, r.version FROM entitlements e "
                    "JOIN releases r ON r.id = e.release_id WHERE e.id = ?",
                    (entitlement_id,),
                ).fetchone()
                audit_row = self._require_row(
                    "admin_decision_audits", audit_id, "admin decision audit"
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("approval violated a commerce constraint") from exc

        return ApprovalResult(
            self._payment_from_row(payment_row),
            self._entitlement_from_row(entitlement_row),
            self._audit_from_row(audit_row),
            False,
        )

    def reject_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
        reason: str,
    ) -> PaymentSubmission:
        payment_id = validate_opaque_identifier(payment_id, field="payment_id")
        admin_subject = validate_opaque_identifier(
            admin_subject, field="admin_subject"
        )
        sanitized_reason = sanitize_human_text(
            reason, field="rejection_reason", max_length=500
        )
        decided_at = self._now()
        try:
            with self._transaction():
                payment_row = self._require_row(
                    "payment_submissions", payment_id, "payment"
                )
                if payment_row["state"] != PaymentState.UNDER_REVIEW.value:
                    raise InvalidTransitionError(
                        "only a payment under review can be rejected"
                    )
                if payment_row["review_started_by"] != admin_subject:
                    raise ConflictError(
                        "only the admin who started review may reject payment"
                    )
                audit_id = self._new_id("audit")
                self._connection.execute(
                    "UPDATE payment_submissions SET state = ?, decided_at = ?, "
                    "decided_by = ?, rejection_reason = ? "
                    "WHERE id = ? AND state = ?",
                    (
                        PaymentState.REJECTED.value,
                        decided_at,
                        admin_subject,
                        sanitized_reason,
                        payment_id,
                        PaymentState.UNDER_REVIEW.value,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO admin_decision_audits("
                    "id, payment_id, actor_admin_subject, decision, reason, "
                    "occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        audit_id,
                        payment_id,
                        admin_subject,
                        AdminDecisionKind.REJECTED.value,
                        sanitized_reason,
                        decided_at,
                    ),
                )
                payment_row = self._require_row(
                    "payment_submissions", payment_id, "payment"
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("rejection violated a commerce constraint") from exc
        return self._payment_from_row(payment_row)

    def get_entitlement(
        self,
        license_id: str,
        version: str,
    ) -> Entitlement | None:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        version = normalize_semver(version)
        with self._lock:
            row = self._connection.execute(
                "SELECT e.*, r.version FROM entitlements e "
                "JOIN releases r ON r.id = e.release_id "
                "WHERE e.license_id = ? AND r.version = ?",
                (license_id, version),
            ).fetchone()
        return None if row is None else self._entitlement_from_row(row)

    def authorize_install(
        self,
        license_id: str,
        *,
        device_principal: VerifiedDevicePrincipal,
        artifact_id: str,
        install_mode: InstallMode,
        source_version: str | None = None,
        source_build: int | None = None,
    ) -> InstallAuthorization:
        """Authorize after upstream challenge and install-context verification.

        The API layer, not this repository, verifies the device-key challenge and
        derives fresh/update mode plus the installed source identity.  This method
        then applies persistence-backed authorization to that verified context.
        """

        license_id = validate_opaque_identifier(license_id, field="license_id")
        if not isinstance(device_principal, VerifiedDevicePrincipal):
            raise ValidationError(
                "device_principal must be a VerifiedDevicePrincipal"
            )
        if type(install_mode) is not InstallMode:
            raise ValidationError("install_mode must be an InstallMode")
        artifact_id = validate_opaque_identifier(artifact_id, field="artifact_id")
        if install_mode is InstallMode.FRESH_INSTALL:
            if source_version is not None or source_build is not None:
                raise ValidationError(
                    "fresh install requires source_version and source_build to be None"
                )
            normalized_source = None
            normalized_source_build = None
        else:
            if source_version is None or source_build is None:
                raise ValidationError(
                    "update requires source_version and source_build"
                )
            normalized_source = normalize_semver(source_version)
            normalized_source_build = validate_build(source_build)

        if not device_principal.proof_verified:
            return InstallAuthorization(
                False, InstallDecisionReason.DEVICE_PROOF_REQUIRED
            )

        # BEGIN IMMEDIATE gives this multi-query decision one consistent snapshot
        # and prevents a concurrent connection from replacing the device midway.
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM licenses WHERE id = ?", (license_id,)
            ).fetchone() is None:
                return InstallAuthorization(
                    False, InstallDecisionReason.LICENSE_NOT_FOUND
                )
            active = self._active_device_row(license_id)
            if active is None:
                return InstallAuthorization(
                    False, InstallDecisionReason.DEVICE_NOT_BOUND
                )
            if (
                active["device_key_fingerprint"]
                != device_principal.device_key_fingerprint
            ):
                return InstallAuthorization(
                    False, InstallDecisionReason.DEVICE_NOT_ACTIVE
                )
            if (
                active["platform"] != device_principal.platform
                or active["architecture"] != device_principal.architecture
            ):
                return InstallAuthorization(
                    False, InstallDecisionReason.DEVICE_TARGET_MISMATCH
                )
            artifact = self._connection.execute(
                "SELECT a.*, r.version, r.state AS release_state "
                "FROM release_artifacts a "
                "JOIN releases r ON r.id = a.release_id WHERE a.id = ?",
                (artifact_id,),
            ).fetchone()
            if artifact is None:
                return InstallAuthorization(
                    False, InstallDecisionReason.ARTIFACT_NOT_FOUND
                )
            identity = ArtifactIdentity(
                artifact["version"],
                artifact["platform"],
                artifact["architecture"],
                artifact["build"],
                ArtifactKind(artifact["artifact_kind"]),
            )
            if (
                artifact["signature_verified_at"] is None
                or artifact["verification_key_id"] != artifact["signing_key_id"]
            ):
                return InstallAuthorization(
                    False,
                    InstallDecisionReason.ARTIFACT_NOT_VERIFIED,
                    identity,
                )
            if artifact["release_state"] != ReleaseState.PUBLISHED.value:
                return InstallAuthorization(
                    False,
                    InstallDecisionReason.RELEASE_NOT_PUBLISHED,
                    identity,
                )
            if self._entitlement_row(license_id, artifact["release_id"]) is None:
                return InstallAuthorization(
                    False,
                    InstallDecisionReason.ENTITLEMENT_REQUIRED,
                    identity,
                )
            if (
                identity.platform != device_principal.platform
                or identity.architecture != device_principal.architecture
            ):
                return InstallAuthorization(
                    False,
                    InstallDecisionReason.DEVICE_TARGET_MISMATCH,
                    identity,
                )
            expected_kind = (
                ArtifactKind.INITIAL_INSTALLER
                if install_mode is InstallMode.FRESH_INSTALL
                else ArtifactKind.UPDATE_PACKAGE
            )
            if identity.artifact_kind is not expected_kind:
                return InstallAuthorization(
                    False,
                    InstallDecisionReason.ARTIFACT_KIND_MISMATCH,
                    identity,
                )
            if install_mode is InstallMode.UPDATE:
                source_product_version = ProductVersion.parse(
                    normalized_source,
                    normalized_source_build,
                )
                candidate_product_version = ProductVersion.parse(
                    identity.version,
                    identity.build,
                )
                try:
                    require_monotonic_upgrade(
                        source_product_version,
                        candidate_product_version,
                    )
                except ValueError:
                    reason = (
                        InstallDecisionReason.SOURCE_BUILD_NOT_OLDER
                        if identity.build <= normalized_source_build
                        else InstallDecisionReason.SOURCE_VERSION_NOT_OLDER
                    )
                    return InstallAuthorization(
                        False,
                        reason,
                        identity,
                    )
                if normalized_source == identity.version:
                    return InstallAuthorization(
                        True, InstallDecisionReason.AUTHORIZED, identity
                    )
                compatible = self._connection.execute(
                    "SELECT 1 FROM artifact_compatible_sources "
                    "WHERE artifact_id = ? AND source_version = ?",
                    (artifact_id, normalized_source),
                ).fetchone()
                if compatible is None:
                    return InstallAuthorization(
                        False,
                        InstallDecisionReason.INCOMPATIBLE_SOURCE_VERSION,
                        identity,
                    )
            return InstallAuthorization(
                True, InstallDecisionReason.AUTHORIZED, identity
            )

    def list_admin_decisions(self) -> tuple[AdminDecisionAudit, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM admin_decision_audits "
                "ORDER BY occurred_at, rowid"
            ).fetchall()
        return tuple(self._audit_from_row(row) for row in rows)

    def _require_exists(self, table: str, record_id: str, label: str) -> None:
        self._require_row(table, record_id, label)

    def _require_row(
        self, table: str, record_id: str, label: str
    ) -> sqlite3.Row:
        allowed = {
            "accounts",
            "licenses",
            "releases",
            "release_artifacts",
            "payment_submissions",
            "admin_decision_audits",
        }
        if table not in allowed:
            raise PersistenceInvariantError("repository requested an invalid table")
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{label} not found")
        return row

    def _active_device_row(self, license_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM device_bindings "
            "WHERE license_id = ? AND deactivated_at IS NULL",
            (license_id,),
        ).fetchone()

    def _entitlement_row(
        self, license_id: str, release_id: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM entitlements WHERE license_id = ? AND release_id = ?",
            (license_id, release_id),
        ).fetchone()

    def _verify_artifact_candidate(
        self, candidate: ArtifactVerificationCandidate
    ) -> ArtifactVerificationReceipt:
        verifier = self._artifact_verifier
        if verifier is None:
            raise ArtifactVerificationError(
                "artifact verifier is not configured; artifact was not persisted"
            )
        try:
            if isinstance(verifier, ArtifactVerifier):
                result = verifier.verify(candidate)
            elif callable(verifier):
                result = verifier(candidate)
            else:
                raise ArtifactVerificationError(
                    "artifact verifier does not implement the verifier contract"
                )
        except ArtifactVerificationError:
            raise
        except Exception as exc:
            raise ArtifactVerificationError(
                "artifact verification failed; artifact was not persisted"
            ) from exc

        if not isinstance(result, ArtifactVerificationReceipt):
            raise ArtifactVerificationError(
                "artifact verifier returned an invalid receipt"
            )
        try:
            verified_at = normalize_utc_timestamp(
                result.verified_at, field="signature_verified_at"
            )
            verification_key_id = validate_opaque_identifier(
                result.verification_key_id, field="verification_key_id"
            )
        except ValidationError as exc:
            raise ArtifactVerificationError(
                "artifact verifier returned an invalid receipt"
            ) from exc
        if verification_key_id != candidate.signing_key_id:
            raise ArtifactVerificationError(
                "artifact verification key does not match signing metadata"
            )
        return ArtifactVerificationReceipt(verified_at, verification_key_id)

    @staticmethod
    def _optional_label(value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_human_text(value, field="device_label", max_length=120)

    @staticmethod
    def _normalize_source_versions(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise ValidationError("compatible_source_versions must be a sequence")
        normalized = tuple(normalize_semver(item) for item in values)
        if len(set(normalized)) != len(normalized):
            raise ValidationError("compatible_source_versions must be unique")
        ordered = tuple(sorted(normalized, key=SemanticVersion.parse))
        if normalized != ordered:
            raise ValidationError("compatible_source_versions must be sorted")
        return normalized

    @staticmethod
    def _optional_release_text(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise ValidationError(f"{field} must be text")
        if not value.strip():
            return ""
        return sanitize_human_text(value, field=field, max_length=4000)

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> Account:
        return Account(
            row["id"],
            row["external_subject"],
            row["created_at"],
        )

    @staticmethod
    def _license_from_row(row: sqlite3.Row) -> License:
        return License(
            row["id"],
            row["account_id"],
            row["plan_code"],
            row["created_at"],
        )

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> DeviceBinding:
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
    def _release_from_row(row: sqlite3.Row) -> Release:
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
    def _payment_from_row(row: sqlite3.Row) -> PaymentSubmission:
        return PaymentSubmission(
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
            row["client_submission_id"],
            row["supersedes_payment_id"],
        )

    @staticmethod
    def _entitlement_from_row(row: sqlite3.Row) -> Entitlement:
        return Entitlement(
            row["id"],
            row["license_id"],
            row["release_id"],
            row["version"],
            row["granted_by_payment_id"],
            row["granted_at"],
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> AdminDecisionAudit:
        return AdminDecisionAudit(
            row["id"],
            row["payment_id"],
            row["actor_admin_subject"],
            AdminDecisionKind(row["decision"]),
            row["reason"],
            row["occurred_at"],
        )

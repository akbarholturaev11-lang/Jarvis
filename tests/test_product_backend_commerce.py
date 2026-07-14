from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.product_state import PaymentState
from product_backend import (
    MAX_PAYMENT_SCREENSHOT_BYTES,
    SINGLE_PAID_PLAN_CODE,
    AdminDecisionKind,
    ArtifactKind,
    ArtifactVerificationCandidate,
    ArtifactVerificationError,
    ArtifactVerificationReceipt,
    CommerceRepository,
    ConflictError,
    InstallDecisionReason,
    InstallMode,
    InvalidTransitionError,
    ReleaseState,
    SQLiteCommerceRepository,
    ValidationError,
    VerifiedDevicePrincipal,
)


MAC_FINGERPRINT = "sha256:" + ("a" * 64)
WINDOWS_FINGERPRINT = "sha256:" + ("b" * 64)
OTHER_FINGERPRINT = "sha256:" + ("c" * 64)
SCREENSHOT_SHA256 = "d" * 64
ADMIN_ONE = "admin:operator-001"
ADMIN_TWO = "admin:operator-002"
DUMMY_SIGNATURE = "A" * 86


class IncrementingClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self._value
        self._value += timedelta(microseconds=1)
        return current


class RecordingArtifactVerifier:
    """Explicit test-only verifier; production must use a pinned public key."""

    def __init__(self, outcome: object = "receipt") -> None:
        self.outcome = outcome
        self.candidates: list[ArtifactVerificationCandidate] = []

    def verify(self, candidate: ArtifactVerificationCandidate):
        self.candidates.append(candidate)
        if self.outcome == "raise":
            raise RuntimeError("test verifier failure")
        if self.outcome == "receipt":
            return ArtifactVerificationReceipt(
                verified_at="2026-07-13T00:30:00Z",
                verification_key_id=candidate.signing_key_id,
            )
        return self.outcome


class CommerceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = RecordingArtifactVerifier()
        self.repo = SQLiteCommerceRepository(
            clock=IncrementingClock(),
            artifact_verifier=self.verifier,
        )
        self.assertIsInstance(self.repo, CommerceRepository)
        account = self.repo.create_account("buyer:account-001")
        self.license = self.repo.issue_license(account.id)
        self.device = self.repo.activate_device(
            self.license.id,
            MAC_FINGERPRINT,
            platform="macos",
            architecture="arm64",
            device_label="Akbar Mac",
        )
        self.mac_principal = VerifiedDevicePrincipal(
            MAC_FINGERPRINT,
            "macos",
            "arm64",
            True,
        )

    def tearDown(self) -> None:
        self.repo.close()

    def _create_release(
        self,
        version: str,
        *,
        build: int,
        price_minor: int = 125_000,
        platform: str = "macos",
        architecture: str = "arm64",
        storage_suffix: str | None = None,
        sources: tuple[str, ...] = (),
        artifact_kind: ArtifactKind | None = None,
    ):
        release = self.repo.create_release(
            version,
            price_minor=price_minor,
            currency="UZS",
        )
        suffix = storage_suffix or f"{version}-{platform}-{build}"
        selected_kind = artifact_kind or (
            ArtifactKind.UPDATE_PACKAGE
            if sources
            else ArtifactKind.INITIAL_INSTALLER
        )
        artifact = self.repo.add_release_artifact(
            release.id,
            platform=platform,
            architecture=architecture,
            artifact_kind=selected_kind,
            build=build,
            sha256=(f"{build:x}"[-1] * 64),
            byte_size=10_000 + build,
            storage_key=f"releases/{suffix}/Jarvis.package",
            signature=DUMMY_SIGNATURE,
            signing_key_id="signing-key-001",
            compatible_source_versions=sources,
        )
        published = self.repo.publish_release(release.id)
        return published, artifact

    def _submit(self, release_id: str, *, suffix: str):
        return self.repo.submit_payment(
            self.license.id,
            release_id,
            screenshot_storage_key=f"payments/{suffix}/screenshot.png",
            screenshot_sha256=SCREENSHOT_SHA256,
            screenshot_byte_size=2048,
            screenshot_mime_type="image/png",
            paid_at="2026-07-13T00:59:00Z",
            client_submission_id=f"submission:{suffix}",
        )

    def _approve(self, release_id: str, *, suffix: str):
        payment = self._submit(release_id, suffix=suffix)
        self.repo.start_payment_review(payment.id, admin_subject=ADMIN_ONE)
        return self.repo.approve_payment(payment.id, admin_subject=ADMIN_ONE)

    def _fresh_authorization(self, artifact_id: str, **overrides):
        values = {
            "device_principal": self.mac_principal,
            "artifact_id": artifact_id,
            "install_mode": InstallMode.FRESH_INSTALL,
        }
        values.update(overrides)
        return self.repo.authorize_install(self.license.id, **values)

    def test_pending_cannot_install_and_install_mode_is_explicit(self) -> None:
        release, artifact = self._create_release("1.0.0", build=10)
        payment = self._submit(release.id, suffix="pending-100")

        self.assertEqual(self.license.plan_code, SINGLE_PAID_PLAN_CODE)
        self.assertEqual(payment.state, PaymentState.PENDING)
        denied = self._fresh_authorization(artifact.id)
        self.assertEqual(denied.reason, InstallDecisionReason.ENTITLEMENT_REQUIRED)
        with self.assertRaises(ValidationError):
            self._fresh_authorization(
                artifact.id,
                source_version="0.9.0",
                source_build=9,
            )
        with self.assertRaises(ValidationError):
            self.repo.authorize_install(
                self.license.id,
                device_principal=self.mac_principal,
                artifact_id=artifact.id,
                install_mode=InstallMode.UPDATE,
            )
        with self.assertRaises(InvalidTransitionError):
            self.repo.approve_payment(payment.id, admin_subject=ADMIN_ONE)

    def test_approval_owner_exact_version_and_idempotent_actor(self) -> None:
        release_100, artifact_100 = self._create_release("1.0.0", build=10)
        release_110, artifact_110 = self._create_release(
            "1.1.0",
            build=11,
            sources=("1.0.0",),
        )
        payment = self._submit(release_100.id, suffix="approve-100")
        self.repo.start_payment_review(payment.id, admin_subject=ADMIN_ONE)

        with self.assertRaises(ConflictError):
            self.repo.approve_payment(payment.id, admin_subject=ADMIN_TWO)
        self.assertIsNone(self.repo.get_entitlement(self.license.id, "1.0.0"))
        self.assertEqual(self.repo.list_admin_decisions(), ())

        first = self.repo.approve_payment(payment.id, admin_subject=ADMIN_ONE)
        retry = self.repo.approve_payment(payment.id, admin_subject=ADMIN_TWO)
        self.assertFalse(first.idempotent)
        self.assertTrue(retry.idempotent)
        self.assertEqual(first.entitlement.id, retry.entitlement.id)
        self.assertEqual(first.audit.actor_admin_subject, ADMIN_ONE)
        self.assertEqual(retry.audit.actor_admin_subject, ADMIN_ONE)
        self.assertTrue(self._fresh_authorization(artifact_100.id).allowed)
        next_version = self._fresh_authorization(artifact_110.id)
        self.assertEqual(
            next_version.reason, InstallDecisionReason.ENTITLEMENT_REQUIRED
        )
        self.assertIsNone(self.repo.get_entitlement(self.license.id, "1.1.0"))
        self.assertEqual(release_110.version, "1.1.0")

    def test_rejection_requires_review_owner_and_grants_nothing(self) -> None:
        release, artifact = self._create_release("2.0.0", build=20)
        payment = self._submit(release.id, suffix="reject-200")
        self.repo.start_payment_review(payment.id, admin_subject=ADMIN_ONE)

        with self.assertRaises(ConflictError):
            self.repo.reject_payment(
                payment.id,
                admin_subject=ADMIN_TWO,
                reason="Wrong reviewer",
            )
        rejected = self.repo.reject_payment(
            payment.id,
            admin_subject=ADMIN_ONE,
            reason="<script>Mismatch</script>\n token=private-value",
        )
        self.assertEqual(rejected.state, PaymentState.REJECTED)
        self.assertNotIn("private-value", rejected.rejection_reason or "")
        self.assertIn("[redacted]", rejected.rejection_reason or "")
        self.assertIsNone(self.repo.get_entitlement(self.license.id, "2.0.0"))
        self.assertEqual(
            self._fresh_authorization(artifact.id).reason,
            InstallDecisionReason.ENTITLEMENT_REQUIRED,
        )
        audit = self.repo.list_admin_decisions()[0]
        self.assertEqual(audit.decision, AdminDecisionKind.REJECTED)
        self.assertEqual(audit.actor_admin_subject, ADMIN_ONE)
        with self.assertRaises(InvalidTransitionError):
            self.repo.approve_payment(payment.id, admin_subject=ADMIN_ONE)

    def test_same_semver_rebuild_requires_strictly_newer_build(self) -> None:
        release, first = self._create_release("3.0.0", build=30)
        self._approve(release.id, suffix="approve-300")
        rebuilt = self.repo.add_release_artifact(
            release.id,
            platform="macos",
            architecture="arm64",
            artifact_kind=ArtifactKind.UPDATE_PACKAGE,
            build=31,
            sha256="e" * 64,
            byte_size=20_000,
            storage_key="releases/3.0.0/build-31/Jarvis.dmg",
            signature=DUMMY_SIGNATURE,
            signing_key_id="signing-key-001",
            compatible_source_versions=("2.9.0",),
        )

        allowed = self.repo.authorize_install(
            self.license.id,
            device_principal=self.mac_principal,
            artifact_id=rebuilt.id,
            install_mode=InstallMode.UPDATE,
            source_version="3.0.0",
            source_build=first.identity.build,
        )
        equal_build = self.repo.authorize_install(
            self.license.id,
            device_principal=self.mac_principal,
            artifact_id=rebuilt.id,
            install_mode=InstallMode.UPDATE,
            source_version="3.0.0",
            source_build=rebuilt.identity.build,
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(
            equal_build.reason, InstallDecisionReason.SOURCE_BUILD_NOT_OLDER
        )
        with self.assertRaises(ValidationError):
            self.repo.authorize_install(
                self.license.id,
                device_principal=self.mac_principal,
                artifact_id=rebuilt.id,
                install_mode=InstallMode.UPDATE,
                source_version="3.0.0",
                source_build=0,
            )

    def test_cross_semver_update_cannot_bypass_compatibility(self) -> None:
        release, artifact = self._create_release(
            "5.0.0",
            build=50,
            sources=("4.2.0",),
        )
        self._approve(release.id, suffix="approve-500")
        compatible = self.repo.authorize_install(
            self.license.id,
            device_principal=self.mac_principal,
            artifact_id=artifact.id,
            install_mode=InstallMode.UPDATE,
            source_version="4.2.0",
            source_build=49,
        )
        incompatible = self.repo.authorize_install(
            self.license.id,
            device_principal=self.mac_principal,
            artifact_id=artifact.id,
            install_mode=InstallMode.UPDATE,
            source_version="4.1.0",
            source_build=48,
        )
        self.assertTrue(compatible.allowed)
        self.assertEqual(
            incompatible.reason,
            InstallDecisionReason.INCOMPATIBLE_SOURCE_VERSION,
        )
        signed_rollback = self.repo.authorize_install(
            self.license.id,
            device_principal=self.mac_principal,
            artifact_id=artifact.id,
            install_mode=InstallMode.UPDATE,
            source_version="6.0.0",
            source_build=49,
        )
        self.assertEqual(
            signed_rollback.reason,
            InstallDecisionReason.SOURCE_VERSION_NOT_OLDER,
        )
        with self.assertRaises(ValidationError):
            self.repo.authorize_install(
                self.license.id,
                device_principal=self.mac_principal,
                artifact_id=artifact.id,
                install_mode=InstallMode.UPDATE,
                source_version=None,
                source_build=None,
            )

    def test_verified_device_principal_target_and_replacement_history(self) -> None:
        release, mac_artifact = self._create_release("6.0.0", build=60)
        windows_artifact = self.repo.add_release_artifact(
            release.id,
            platform="windows",
            architecture="x86_64",
            build=1,
            sha256="f" * 64,
            byte_size=30_000,
            storage_key="releases/6.0.0/windows/Jarvis.exe",
            signature=DUMMY_SIGNATURE,
            signing_key_id="signing-key-001",
        )
        self._approve(release.id, suffix="approve-600")

        unproved = VerifiedDevicePrincipal(
            MAC_FINGERPRINT, "macos", "arm64", False
        )
        self.assertEqual(
            self._fresh_authorization(
                mac_artifact.id, device_principal=unproved
            ).reason,
            InstallDecisionReason.DEVICE_PROOF_REQUIRED,
        )
        wrong_key = VerifiedDevicePrincipal(
            OTHER_FINGERPRINT, "macos", "arm64", True
        )
        self.assertEqual(
            self._fresh_authorization(
                mac_artifact.id, device_principal=wrong_key
            ).reason,
            InstallDecisionReason.DEVICE_NOT_ACTIVE,
        )
        wrong_target = VerifiedDevicePrincipal(
            MAC_FINGERPRINT, "windows", "x86_64", True
        )
        self.assertEqual(
            self._fresh_authorization(
                mac_artifact.id, device_principal=wrong_target
            ).reason,
            InstallDecisionReason.DEVICE_TARGET_MISMATCH,
        )
        self.assertEqual(
            self._fresh_authorization(windows_artifact.id).reason,
            InstallDecisionReason.DEVICE_TARGET_MISMATCH,
        )

        replacement = self.repo.replace_device(
            self.license.id,
            current_device_key_fingerprint=MAC_FINGERPRINT,
            new_device_key_fingerprint=WINDOWS_FINGERPRINT,
            new_platform="windows",
            new_architecture="x86_64",
            replacement_reason="Owner moved to a new computer",
            new_device_label="New PC",
        )
        history = self.repo.list_device_history(self.license.id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].replaced_by_binding_id, replacement.id)
        self.assertFalse(history[0].is_active)
        self.assertTrue(history[1].is_active)
        self.assertEqual(self.repo.get_active_device(self.license.id), replacement)
        self.assertEqual(
            self._fresh_authorization(mac_artifact.id).reason,
            InstallDecisionReason.DEVICE_NOT_ACTIVE,
        )
        windows_principal = VerifiedDevicePrincipal(
            WINDOWS_FINGERPRINT, "windows", "x86_64", True
        )
        self.assertTrue(
            self._fresh_authorization(
                windows_artifact.id, device_principal=windows_principal
            ).allowed
        )
        with self.assertRaises(ValidationError):
            VerifiedDevicePrincipal(
                "sha256:" + ("A" * 64), "macos", "arm64", True
            )

    def test_global_product_version_monotonicity_is_per_target_stream(self) -> None:
        release_100, _ = self._create_release(
            "1.0.0", build=100, sources=("0.9.0",)
        )
        with self.assertRaises(InvalidTransitionError):
            self.repo.add_release_artifact(
                release_100.id,
                platform="macos",
                architecture="arm64",
                artifact_kind=ArtifactKind.UPDATE_PACKAGE,
                build=99,
                sha256="1" * 64,
                byte_size=999,
                storage_key="releases/1.0.0/build-99/Jarvis.dmg",
                signature=DUMMY_SIGNATURE,
                signing_key_id="signing-key-001",
                compatible_source_versions=("0.9.0",),
            )
        release_110 = self.repo.create_release(
            "1.1.0", price_minor=130_000, currency="UZS"
        )
        with self.assertRaises(InvalidTransitionError):
            self.repo.add_release_artifact(
                release_110.id,
                platform="macos",
                architecture="arm64",
                artifact_kind=ArtifactKind.UPDATE_PACKAGE,
                build=1,
                sha256="2" * 64,
                byte_size=1_001,
                storage_key="releases/1.1.0/build-1/Jarvis.dmg",
                signature=DUMMY_SIGNATURE,
                signing_key_id="signing-key-001",
                compatible_source_versions=("1.0.0",),
            )
        release_090 = self.repo.create_release(
            "0.9.0", price_minor=120_000, currency="UZS"
        )
        with self.assertRaises(InvalidTransitionError):
            self.repo.add_release_artifact(
                release_090.id,
                platform="macos",
                architecture="arm64",
                artifact_kind=ArtifactKind.UPDATE_PACKAGE,
                build=101,
                sha256="3" * 64,
                byte_size=1_002,
                storage_key="releases/0.9.0/build-101/Jarvis.dmg",
                signature=DUMMY_SIGNATURE,
                signing_key_id="signing-key-001",
                compatible_source_versions=("0.8.0",),
            )
        windows = self.repo.add_release_artifact(
            release_100.id,
            platform="windows",
            architecture="x86_64",
            build=1,
            sha256="4" * 64,
            byte_size=1_003,
            storage_key="releases/1.0.0/windows-build-1/Jarvis.exe",
            signature=DUMMY_SIGNATURE,
            signing_key_id="signing-key-001",
        )
        self.assertEqual(windows.identity.build, 1)

    def test_payment_screenshot_metadata_is_bounded_and_private(self) -> None:
        release, _ = self._create_release("8.0.0", build=80)
        invalid_cases = (
            {"screenshot_sha256": "A" * 64},
            {"screenshot_byte_size": 0},
            {"screenshot_byte_size": MAX_PAYMENT_SCREENSHOT_BYTES + 1},
            {"screenshot_mime_type": "image/gif"},
            {"screenshot_storage_key": "https://public.invalid/evidence.png"},
        )
        defaults = {
            "screenshot_storage_key": "payments/metadata/evidence.png",
            "screenshot_sha256": SCREENSHOT_SHA256,
            "screenshot_byte_size": 1024,
            "screenshot_mime_type": "image/png",
            "paid_at": "2026-07-13T00:00:00Z",
            "client_submission_id": "submission:metadata",
        }
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides), self.assertRaises(
                ValidationError
            ):
                self.repo.submit_payment(
                    self.license.id,
                    release.id,
                    **(defaults | overrides),
                )
        payment = self.repo.submit_payment(
            self.license.id,
            release.id,
            **defaults,
        )
        self.assertEqual(payment.screenshot_sha256, SCREENSHOT_SHA256)
        self.assertEqual(payment.screenshot_byte_size, 1024)
        self.assertEqual(payment.screenshot_mime_type, "image/png")


class ArtifactVerifierBoundaryTests(unittest.TestCase):
    def _draft_and_artifact_args(self, repo: SQLiteCommerceRepository):
        account = repo.create_account("buyer:verifier-001")
        repo.issue_license(account.id)
        release = repo.create_release(
            "1.0.0", price_minor=100_000, currency="UZS"
        )
        args = {
            "platform": "macos",
            "architecture": "arm64",
            "artifact_kind": ArtifactKind.INITIAL_INSTALLER,
            "build": 1,
            "sha256": "a" * 64,
            "byte_size": 1_000,
            "storage_key": "releases/verified/Jarvis.dmg",
            "signature": DUMMY_SIGNATURE,
            "signing_key_id": "signing-key-001",
            "compatible_source_versions": (),
        }
        return release, args

    def test_missing_rejecting_or_throwing_verifier_blocks_persistence(self) -> None:
        for outcome in (None, False, "raise"):
            verifier = None if outcome is None else RecordingArtifactVerifier(outcome)
            with self.subTest(outcome=outcome), SQLiteCommerceRepository(
                artifact_verifier=verifier
            ) as repo:
                release, args = self._draft_and_artifact_args(repo)
                with self.assertRaises(ArtifactVerificationError):
                    repo.add_release_artifact(release.id, **args)
                with self.assertRaises(InvalidTransitionError):
                    repo.publish_release(release.id)

    def test_verified_receipt_and_complete_immutable_candidate_are_persisted(self) -> None:
        verifier = RecordingArtifactVerifier()
        with SQLiteCommerceRepository(artifact_verifier=verifier) as repo:
            release, args = self._draft_and_artifact_args(repo)
            artifact = repo.add_release_artifact(release.id, **args)
            repo.publish_release(release.id)

        self.assertEqual(artifact.signature_verified_at, "2026-07-13T00:30:00.000000Z")
        self.assertEqual(artifact.verification_key_id, artifact.signing_key_id)
        candidate = verifier.candidates[0]
        self.assertEqual(candidate.product_id, "jarvis")
        self.assertEqual(candidate.bundle_id, "com.jarvis.assistant")
        self.assertEqual(candidate.release_version, "1.0.0")
        self.assertEqual(candidate.artifact_kind, ArtifactKind.INITIAL_INSTALLER)
        self.assertEqual(candidate.storage_key, args["storage_key"])
        self.assertEqual(candidate.compatible_source_versions, ())
        with self.assertRaises(FrozenInstanceError):
            candidate.build = 2

    def test_boolean_callback_is_not_a_cryptographic_verification_receipt(self) -> None:
        with SQLiteCommerceRepository(
            clock=IncrementingClock(), artifact_verifier=lambda _candidate: True
        ) as repo:
            release, args = self._draft_and_artifact_args(repo)
            with self.assertRaises(ArtifactVerificationError):
                repo.add_release_artifact(release.id, **args)


class CommercePersistenceTests(unittest.TestCase):
    def test_concurrent_approval_creates_one_entitlement_and_one_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "commerce.sqlite3"
            first = SQLiteCommerceRepository(
                database,
                clock=IncrementingClock(),
                artifact_verifier=RecordingArtifactVerifier(),
            )
            account = first.create_account("buyer:concurrency-001")
            license_record = first.issue_license(account.id)
            first.activate_device(
                license_record.id,
                MAC_FINGERPRINT,
                platform="macos",
                architecture="arm64",
            )
            release = first.create_release(
                "9.0.0", price_minor=300_000, currency="UZS"
            )
            first.add_release_artifact(
                release.id,
                platform="macos",
                architecture="arm64",
                build=90,
                sha256="9" * 64,
                byte_size=9_000,
                storage_key="releases/9.0.0/Jarvis.dmg",
                signature=DUMMY_SIGNATURE,
                signing_key_id="signing-key-001",
            )
            first.publish_release(release.id)
            payment = first.submit_payment(
                license_record.id,
                release.id,
                screenshot_storage_key="payments/concurrent/screenshot.png",
                screenshot_sha256=SCREENSHOT_SHA256,
                screenshot_byte_size=1024,
                screenshot_mime_type="image/png",
                paid_at="2026-07-13T00:00:00Z",
                client_submission_id="submission:concurrent",
            )
            first.start_payment_review(payment.id, admin_subject=ADMIN_ONE)
            second = SQLiteCommerceRepository(
                database,
                clock=IncrementingClock(),
                artifact_verifier=RecordingArtifactVerifier(),
            )
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            repository.approve_payment,
                            payment.id,
                            admin_subject=ADMIN_ONE,
                        )
                        for repository in (first, second)
                    ]
                    results = [future.result(timeout=5) for future in futures]
                self.assertEqual(
                    {result.idempotent for result in results}, {False, True}
                )
                self.assertEqual(results[0].entitlement.id, results[1].entitlement.id)
                self.assertEqual(len(first.list_admin_decisions()), 1)
            finally:
                second.close()
                first.close()

    def test_schema_has_constraints_and_no_bytes_secrets_or_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "commerce.sqlite3"
            repo = SQLiteCommerceRepository(database)
            repo.close()
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                payment_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(payment_submissions)"
                    )
                }
                entitlement_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(entitlements)")
                }
                artifact_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(release_artifacts)"
                    )
                }
                release_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(releases)")
                }
                self.assertTrue(
                    {
                        "screenshot_storage_key",
                        "screenshot_sha256",
                        "screenshot_byte_size",
                        "screenshot_mime_type",
                        "client_submission_id",
                        "supersedes_payment_id",
                    }.issubset(payment_columns)
                )
                self.assertNotIn("screenshot_bytes", payment_columns)
                self.assertNotIn("public_url", payment_columns)
                self.assertNotIn("secret", payment_columns)
                self.assertNotIn("expires_at", entitlement_columns)
                self.assertNotIn("revoked_at", entitlement_columns)
                self.assertIn("signature_verified_at", artifact_columns)
                self.assertIn("verification_key_id", artifact_columns)
                self.assertIn("artifact_kind", artifact_columns)
                self.assertTrue(
                    {"features_en", "features_ru", "fixes_en", "fixes_ru"}
                    .issubset(release_columns)
                )
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    4,
                )
                self.assertGreater(
                    len(
                        connection.execute(
                            "PRAGMA foreign_key_list(entitlements)"
                        ).fetchall()
                    ),
                    0,
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()

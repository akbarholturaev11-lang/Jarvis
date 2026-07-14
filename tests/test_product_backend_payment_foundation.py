from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from core.release_manifest import ArtifactKind
from product_backend.api_queries import SQLiteProductReadStore
from product_backend.api_service import (
    BackendServiceNotAvailableError,
    ProductBackendService,
)
from product_backend.models import (
    ArtifactVerificationCandidate,
    ArtifactVerificationReceipt,
    ConflictError,
    InvalidTransitionError,
    ValidationError,
    VerifiedDevicePrincipal,
)
from product_backend.private_storage import PrivateObjectMetadata
from product_backend.sqlite_repository import SQLiteCommerceRepository


_FINGERPRINT = "sha256:" + ("a" * 64)
_OTHER_FINGERPRINT = "sha256:" + ("b" * 64)
_SIGNATURE = "A" * 86
_SCREENSHOT_SHA256 = "c" * 64
_PAID_AT = "2026-07-14T01:00:00Z"


class _Verifier:
    def verify(
        self, candidate: ArtifactVerificationCandidate
    ) -> ArtifactVerificationReceipt:
        return ArtifactVerificationReceipt(
            "2026-07-14T00:00:00Z", candidate.signing_key_id
        )


class _UnusedChallenges:
    def issue(self, **_kwargs):  # pragma: no cover - structural port only
        raise AssertionError("challenge port is not used by these tests")

    def verify_and_consume(
        self, **_kwargs
    ):  # pragma: no cover - structural port only
        raise AssertionError("challenge port is not used by these tests")


class _MemoryEvidenceStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.discarded: list[str] = []
        self.fail_discard = False
        self._counter = 0

    def store_payment_screenshot(self, content, *, content_type, now=None):
        self._counter += 1
        storage_key = f"payments/test/evidence-{self._counter}.png"
        self.objects[storage_key] = bytes(content)
        timestamp = datetime.now(timezone.utc) if now is None else now
        return PrivateObjectMetadata(
            storage_key,
            hashlib.sha256(content).hexdigest(),
            len(content),
            content_type,
            timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

    def read_private_object(self, metadata, *, maximum_bytes):
        content = self.objects[metadata.storage_key]
        if len(content) > maximum_bytes:
            raise AssertionError("test evidence unexpectedly exceeds bound")
        return content

    def discard_payment_screenshot(self, metadata):
        if self.fail_discard:
            raise OSError("test-only discard failure")
        self.objects.pop(metadata.storage_key, None)
        self.discarded.append(metadata.storage_key)


class PaymentFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "commerce.sqlite3"
        self.repo = SQLiteCommerceRepository(
            self.database, artifact_verifier=_Verifier()
        )
        self.release, self.artifact = self._published_release(
            self.repo, "1.0.0", platform="macos", architecture="arm64"
        )
        self.principal = VerifiedDevicePrincipal(
            _FINGERPRINT, "macos", "arm64", True
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.temporary.cleanup()

    @staticmethod
    def _published_release(
        repo: SQLiteCommerceRepository,
        version: str,
        *,
        platform: str,
        architecture: str,
    ):
        release = repo.create_release(
            version, price_minor=149_000, currency="UZS"
        )
        artifact = repo.add_release_artifact(
            release.id,
            platform=platform,
            architecture=architecture,
            artifact_kind=ArtifactKind.INITIAL_INSTALLER,
            build=1,
            sha256="d" * 64,
            byte_size=4096,
            storage_key=f"releases/{version}/{platform}-{architecture}.dmg",
            signature=_SIGNATURE,
            signing_key_id="release-key-001",
            compatible_source_versions=(),
        )
        return repo.publish_release(release.id), artifact

    def _initial_kwargs(self, **overrides):
        values = {
            "purchase_id": "purchase-session-001",
            "release_id": self.release.id,
            "device_principal": self.principal,
            "screenshot_storage_key": "payments/initial/evidence-001.png",
            "screenshot_sha256": _SCREENSHOT_SHA256,
            "screenshot_byte_size": 2048,
            "screenshot_mime_type": "image/png",
            "paid_at": _PAID_AT,
            "client_submission_id": "submission:initial-001",
        }
        values.update(overrides)
        return values

    def _counts(self) -> dict[str, int]:
        with sqlite3.connect(self.database) as connection:
            return {
                table: int(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "accounts",
                    "licenses",
                    "device_bindings",
                    "payment_submissions",
                    "entitlements",
                )
            }

    def test_initial_purchase_is_atomic_private_and_non_entitling(self) -> None:
        raw_purchase_id = "purchase-session-private-001"
        result = self.repo.submit_initial_purchase(
            **self._initial_kwargs(purchase_id=raw_purchase_id)
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.license.account_id, result.account.id)
        self.assertEqual(result.device.license_id, result.license.id)
        self.assertEqual(result.payment.license_id, result.license.id)
        self.assertEqual(result.payment.amount_minor, 149_000)
        self.assertEqual(result.payment.currency, "UZS")
        self.assertEqual(result.payment.client_submission_id, "submission:initial-001")
        self.assertEqual(
            result.account.external_subject,
            "purchase:" + hashlib.sha256(raw_purchase_id.encode()).hexdigest(),
        )
        self.assertNotIn(raw_purchase_id, result.account.external_subject)
        self.assertIsNone(self.repo.get_entitlement(result.license.id, "1.0.0"))
        self.assertEqual(
            self._counts(),
            {
                "accounts": 1,
                "licenses": 1,
                "device_bindings": 1,
                "payment_submissions": 1,
                "entitlements": 0,
            },
        )
        database_bytes = self.database.read_bytes()
        self.assertNotIn(raw_purchase_id.encode(), database_bytes)

    def test_initial_purchase_exact_retry_reuses_every_record(self) -> None:
        first = self.repo.submit_initial_purchase(**self._initial_kwargs())
        second = self.repo.submit_initial_purchase(
            **self._initial_kwargs(
                screenshot_storage_key="payments/retry/evidence-002.png"
            )
        )

        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.account.id, second.account.id)
        self.assertEqual(first.license.id, second.license.id)
        self.assertEqual(first.device.id, second.device.id)
        self.assertEqual(first.payment.id, second.payment.id)
        self.assertEqual(
            second.payment.screenshot_storage_key,
            "payments/initial/evidence-001.png",
        )
        self.assertEqual(self._counts()["payment_submissions"], 1)

    def test_submission_identity_reuse_with_mutated_payload_is_rejected(self) -> None:
        self.repo.submit_initial_purchase(**self._initial_kwargs())
        mismatches = (
            {"screenshot_sha256": "e" * 64},
            {"screenshot_byte_size": 2049},
            {"screenshot_mime_type": "image/jpeg"},
            {"paid_at": "2026-07-14T01:00:01Z"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch), self.assertRaises(ConflictError):
                self.repo.submit_initial_purchase(
                    **self._initial_kwargs(
                        screenshot_storage_key="payments/mutated/evidence.png",
                        **mismatch,
                    )
                )
        self.assertEqual(self._counts()["payment_submissions"], 1)

    def test_unproved_or_wrong_target_purchase_has_no_side_effects(self) -> None:
        unproved = VerifiedDevicePrincipal(
            _FINGERPRINT, "macos", "arm64", False
        )
        with self.assertRaises(ValidationError):
            self.repo.submit_initial_purchase(
                **self._initial_kwargs(device_principal=unproved)
            )
        self.assertEqual(self._counts()["accounts"], 0)

        wrong_target = VerifiedDevicePrincipal(
            _FINGERPRINT, "windows", "x86_64", True
        )
        with self.assertRaises(InvalidTransitionError):
            self.repo.submit_initial_purchase(
                **self._initial_kwargs(device_principal=wrong_target)
            )
        self.assertEqual(self._counts()["accounts"], 0)

    def test_purchase_identity_cannot_rebind_to_another_device(self) -> None:
        first = self.repo.submit_initial_purchase(**self._initial_kwargs())
        other = VerifiedDevicePrincipal(
            _OTHER_FINGERPRINT, "macos", "arm64", True
        )
        with self.assertRaises(ConflictError):
            self.repo.submit_initial_purchase(
                **self._initial_kwargs(
                    device_principal=other,
                    client_submission_id="submission:other-device",
                    screenshot_storage_key="payments/other-device/evidence.png",
                )
            )
        self.assertEqual(self._counts()["accounts"], 1)
        self.assertEqual(
            self.repo.get_active_device(first.license.id).id,
            first.device.id,
        )

    def test_rejected_payment_requires_explicit_single_resubmission(self) -> None:
        purchase = self.repo.submit_initial_purchase(**self._initial_kwargs())
        self.repo.start_payment_review(
            purchase.payment.id, admin_subject="admin:reviewer-001"
        )
        rejected = self.repo.reject_payment(
            purchase.payment.id,
            admin_subject="admin:reviewer-001",
            reason="Receipt is unreadable / Чек нечитаемый",
        )

        with self.assertRaises(InvalidTransitionError):
            self.repo.submit_payment(
                purchase.license.id,
                self.release.id,
                screenshot_storage_key="payments/resubmit/missing-lineage.png",
                screenshot_sha256="f" * 64,
                screenshot_byte_size=2048,
                screenshot_mime_type="image/png",
                paid_at=_PAID_AT,
                client_submission_id="submission:resubmit-missing",
            )

        resubmitted = self.repo.submit_payment(
            purchase.license.id,
            self.release.id,
            screenshot_storage_key="payments/resubmit/evidence.png",
            screenshot_sha256="f" * 64,
            screenshot_byte_size=2048,
            screenshot_mime_type="image/png",
            paid_at=_PAID_AT,
            client_submission_id="submission:resubmit-001",
            supersedes_payment_id=rejected.id,
        )
        self.assertEqual(resubmitted.supersedes_payment_id, rejected.id)

        with self.assertRaises(ConflictError):
            self.repo.submit_payment(
                purchase.license.id,
                self.release.id,
                screenshot_storage_key="payments/resubmit/second.png",
                screenshot_sha256="0" * 64,
                screenshot_byte_size=2048,
                screenshot_mime_type="image/png",
                paid_at=_PAID_AT,
                client_submission_id="submission:resubmit-002",
                supersedes_payment_id=rejected.id,
            )

    def test_concurrent_exact_initial_retry_creates_one_purchase(self) -> None:
        second = SQLiteCommerceRepository(self.database, artifact_verifier=_Verifier())
        barrier = threading.Barrier(2)

        def submit(repository, storage_key):
            barrier.wait(timeout=5)
            return repository.submit_initial_purchase(
                **self._initial_kwargs(screenshot_storage_key=storage_key)
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(
                        submit, self.repo, "payments/concurrent/first.png"
                    ),
                    executor.submit(
                        submit, second, "payments/concurrent/second.png"
                    ),
                )
                results = [future.result(timeout=10) for future in futures]
        finally:
            second.close()

        self.assertEqual({result.idempotent for result in results}, {False, True})
        self.assertEqual(len({result.account.id for result in results}), 1)
        self.assertEqual(len({result.license.id for result in results}), 1)
        self.assertEqual(len({result.device.id for result in results}), 1)
        self.assertEqual(len({result.payment.id for result in results}), 1)
        self.assertEqual(self._counts()["accounts"], 1)
        self.assertEqual(self._counts()["payment_submissions"], 1)

    def test_service_discards_retry_object_and_failed_request_object(self) -> None:
        evidence = _MemoryEvidenceStore()
        service = ProductBackendService(
            self.repo,
            SQLiteProductReadStore(self.database),
            evidence,
            _UnusedChallenges(),
        )
        common = {
            "purchase_id": "purchase-service-001",
            "release_id": self.release.id,
            "device_principal": self.principal,
            "content": b"normalized-test-image",
            "content_type": "image/png",
            "paid_at": _PAID_AT,
            "client_submission_id": "submission:service-001",
        }
        first = service.submit_initial_purchase_evidence(**common)
        retry = service.submit_initial_purchase_evidence(**common)

        self.assertEqual(first.payment.id, retry.payment.id)
        self.assertTrue(retry.idempotent)
        self.assertEqual(
            set(evidence.objects), {first.payment.screenshot_storage_key}
        )
        self.assertEqual(evidence.discarded, ["payments/test/evidence-2.png"])

        with self.assertRaises(ConflictError):
            service.submit_initial_purchase_evidence(
                **(common | {"content": b"mutated-test-image"})
            )
        self.assertEqual(
            set(evidence.objects), {first.payment.screenshot_storage_key}
        )
        self.assertIn("payments/test/evidence-3.png", evidence.discarded)

    def test_service_reports_compensation_failure_without_false_success(self) -> None:
        evidence = _MemoryEvidenceStore()
        evidence.fail_discard = True
        service = ProductBackendService(
            self.repo,
            SQLiteProductReadStore(self.database),
            evidence,
            _UnusedChallenges(),
        )
        unproved = VerifiedDevicePrincipal(
            _FINGERPRINT, "macos", "arm64", False
        )
        with self.assertRaises(BackendServiceNotAvailableError):
            service.submit_initial_purchase_evidence(
                purchase_id="purchase-service-failure",
                release_id=self.release.id,
                device_principal=unproved,
                content=b"normalized-test-image",
                content_type="image/png",
                paid_at=_PAID_AT,
                client_submission_id="submission:service-failure",
            )
        self.assertEqual(self._counts()["accounts"], 0)


class PaymentSchemaMigrationTests(unittest.TestCase):
    def test_v3_rows_are_backfilled_and_v4_constraints_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "commerce.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    PRAGMA user_version = 3;
                    CREATE TABLE accounts (
                        id TEXT PRIMARY KEY,
                        external_subject TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE licenses (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL REFERENCES accounts(id),
                        plan_code TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE releases (
                        id TEXT PRIMARY KEY,
                        version TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL,
                        price_minor INTEGER NOT NULL,
                        currency TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        published_at TEXT
                    );
                    CREATE TABLE payment_submissions (
                        id TEXT PRIMARY KEY,
                        license_id TEXT NOT NULL REFERENCES licenses(id),
                        release_id TEXT NOT NULL REFERENCES releases(id),
                        amount_minor INTEGER NOT NULL,
                        currency TEXT NOT NULL,
                        screenshot_storage_key TEXT NOT NULL UNIQUE,
                        screenshot_sha256 TEXT NOT NULL,
                        screenshot_byte_size INTEGER NOT NULL,
                        screenshot_mime_type TEXT NOT NULL,
                        paid_at TEXT NOT NULL,
                        submitted_at TEXT NOT NULL,
                        state TEXT NOT NULL,
                        review_started_at TEXT,
                        review_started_by TEXT,
                        decided_at TEXT,
                        decided_by TEXT,
                        rejection_reason TEXT,
                        UNIQUE (id, license_id, release_id)
                    );
                    INSERT INTO accounts VALUES (
                        'acct_legacy', 'buyer:legacy', '2026-07-14T00:00:00Z'
                    );
                    INSERT INTO licenses VALUES (
                        'lic_legacy', 'acct_legacy', 'jarvis_single_paid',
                        '2026-07-14T00:00:00Z'
                    );
                    INSERT INTO releases VALUES (
                        'rel_legacy', '1.0.0', 'published', 149000, 'UZS',
                        '2026-07-14T00:00:00Z', '2026-07-14T00:01:00Z'
                    );
                    INSERT INTO payment_submissions VALUES (
                        'pay_legacy', 'lic_legacy', 'rel_legacy', 149000, 'UZS',
                        'payments/legacy/evidence.png',
                        'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                        2048, 'image/png', '2026-07-14T00:02:00Z',
                        '2026-07-14T00:03:00Z', 'pending',
                        NULL, NULL, NULL, NULL, NULL
                    );
                    """
                )

            repository = SQLiteCommerceRepository(database)
            repository.close()
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 4
                )
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(payment_submissions)"
                    )
                }
                self.assertTrue(
                    {"client_submission_id", "supersedes_payment_id"}.issubset(
                        columns
                    )
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT client_submission_id FROM payment_submissions "
                        "WHERE id = 'pay_legacy'"
                    ).fetchone()[0],
                    "legacy:pay_legacy",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE payment_submissions SET client_submission_id = NULL "
                        "WHERE id = 'pay_legacy'"
                    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from core.release_manifest import ArtifactKind
from product_backend.api_app import create_product_backend_app
from product_backend.api_auth import AdminAuthSettings, AdminPasswordCredential
from product_backend.api_ports import (
    ClientActivationPort,
    DeviceChallengePort,
    PrivatePaymentEvidenceStore,
    ProductReadStore,
    ReleaseArtifactStore,
)
from product_backend.api_queries import (
    ProductReadNotAvailableError,
    SQLiteProductReadStore,
)
from product_backend.models import (
    ArtifactVerificationCandidate,
    ArtifactVerificationReceipt,
    ReleaseState,
)
from product_backend.sqlite_repository import SQLiteCommerceRepository


NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-07-16T02:00:00.000000Z"
ADMIN_SUBJECT = "admin:mobile-test"
ADMIN_PASSWORD = "mobile-admin-test-password"


class _AcceptingArtifactVerifier:
    def verify(
        self,
        candidate: ArtifactVerificationCandidate,
    ) -> ArtifactVerificationReceipt:
        return ArtifactVerificationReceipt(NOW_TEXT, candidate.signing_key_id)


class AdminQueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "commerce.sqlite3"
        self.commerce: SQLiteCommerceRepository | None = SQLiteCommerceRepository(
            self.database,
            clock=lambda: NOW,
            artifact_verifier=_AcceptingArtifactVerifier(),
        )
        commerce = self._commerce()

        self.primary_account = commerce.create_account("buyer:mobile-primary")
        self.primary_license = commerce.issue_license(self.primary_account.id)
        self.primary_device = commerce.activate_device(
            self.primary_license.id,
            "sha256:" + ("a" * 64),
            platform="linux",
            architecture="x86_64",
            device_label="Primary Linux workstation",
        )
        self.secondary_account = commerce.create_account("buyer:mobile-secondary")
        self.secondary_license = commerce.issue_license(self.secondary_account.id)

        self.first_release = self._published_release(
            version="1.0.0",
            price_minor=125_000,
            build=1,
        )
        self.second_release = self._published_release(
            version="1.1.0",
            price_minor=150_000,
            build=2,
        )
        self.draft_release = commerce.create_release(
            "2.0.0",
            price_minor=175_000,
            currency="USD",
            features_en="Draft feature",
            features_ru="Черновая функция",
        )
        self._approve(self.first_release.id, suffix="one")
        self._approve(self.second_release.id, suffix="two")

    def tearDown(self) -> None:
        if self.commerce is not None:
            self.commerce.close()
        self.temporary.cleanup()

    def _commerce(self) -> SQLiteCommerceRepository:
        assert self.commerce is not None
        return self.commerce

    def _published_release(self, *, version: str, price_minor: int, build: int):
        commerce = self._commerce()
        release = commerce.create_release(
            version,
            price_minor=price_minor,
            currency="USD",
            features_en=f"Feature {version}",
            features_ru=f"Функция {version}",
        )
        commerce.add_release_artifact(
            release.id,
            platform="linux",
            architecture="x86_64",
            artifact_kind=ArtifactKind.INITIAL_INSTALLER,
            build=build,
            sha256=f"{build:x}" * 64,
            byte_size=100 + build,
            storage_key=f"releases/{version}/private.bin",
            signature="A" * 86,
            signing_key_id="test-key-001",
        )
        return commerce.publish_release(release.id)

    def _approve(self, release_id: str, *, suffix: str) -> None:
        commerce = self._commerce()
        payment = commerce.submit_payment(
            self.primary_license.id,
            release_id,
            screenshot_storage_key=f"payments/private-{suffix}.png",
            screenshot_sha256=("b" if suffix == "one" else "c") * 64,
            screenshot_byte_size=128,
            screenshot_mime_type="image/png",
            paid_at=NOW_TEXT,
            client_submission_id=f"submission-{suffix}",
        )
        commerce.start_payment_review(payment.id, admin_subject=ADMIN_SUBJECT)
        commerce.approve_payment(payment.id, admin_subject=ADMIN_SUBJECT)

    def _app(self, *, reads: ProductReadStore | None = None):
        credential = AdminPasswordCredential.derive_for_configuration(
            subject=ADMIN_SUBJECT,
            password=ADMIN_PASSWORD,
            salt=b"m" * 32,
        )
        settings = AdminAuthSettings(
            (credential,),
            b"mobile-admin-query-session-secret-32-bytes",
            ("testserver",),
            secure_cookie=False,
        )
        return create_product_backend_app(
            commerce=self._commerce(),
            reads=(
                SQLiteProductReadStore(self.database)
                if reads is None
                else reads
            ),
            evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
            challenges=Mock(spec=DeviceChallengePort),
            activation=Mock(spec=ClientActivationPort),
            release_artifact_store=Mock(spec=ReleaseArtifactStore),
            auth_settings=settings,
            allow_password_only_admin=True,
            clock=lambda: NOW,
        )


class SQLiteAdminProjectionTests(AdminQueryTestCase):
    def test_accounts_licenses_devices_entitlements_and_all_releases(self) -> None:
        reads = SQLiteProductReadStore(self.database)

        accounts = reads.list_admin_accounts(limit=100, offset=0)
        self.assertEqual(accounts.total, 2)
        account_records = {item.account.id: item for item in accounts.records}
        self.assertEqual(account_records[self.primary_account.id].license_count, 1)
        self.assertEqual(
            account_records[self.primary_account.id].active_device_count,
            1,
        )
        self.assertEqual(
            account_records[self.secondary_account.id].active_device_count,
            0,
        )

        licenses = reads.list_admin_licenses(
            account_id=self.primary_account.id,
            limit=10,
            offset=0,
            entitlements_limit=1,
        )
        self.assertEqual(licenses.total, 1)
        record = licenses.records[0]
        self.assertEqual(record.license.id, self.primary_license.id)
        self.assertEqual(
            record.account_external_subject,
            self.primary_account.external_subject,
        )
        self.assertEqual(record.active_device, self.primary_device)
        self.assertEqual(record.entitlement_count, 2)
        self.assertEqual(len(record.entitlements), 1)
        self.assertTrue(record.entitlements_truncated)

        releases = reads.list_admin_releases(limit=100, offset=0)
        self.assertEqual(releases.total, 3)
        by_version = {item.release.version: item for item in releases.records}
        self.assertEqual(by_version["1.0.0"].release.state, ReleaseState.PUBLISHED)
        self.assertEqual(by_version["1.0.0"].release.price_minor, 125_000)
        self.assertEqual(by_version["1.0.0"].artifact_count, 1)
        self.assertEqual(by_version["2.0.0"].release.state, ReleaseState.DRAFT)
        self.assertEqual(by_version["2.0.0"].artifact_count, 0)

    def test_pages_are_deterministic_bounded_and_filterable(self) -> None:
        reads = SQLiteProductReadStore(self.database)
        first = reads.list_admin_accounts(limit=1, offset=0)
        second = reads.list_admin_accounts(limit=1, offset=1)

        self.assertEqual(first.total, 2)
        self.assertEqual(second.total, 2)
        self.assertNotEqual(first.records[0].account.id, second.records[0].account.id)
        filtered = reads.list_admin_licenses(
            account_id=self.secondary_account.id,
            limit=10,
            offset=0,
            entitlements_limit=25,
        )
        self.assertEqual(filtered.total, 1)
        self.assertEqual(filtered.records[0].license.id, self.secondary_license.id)
        self.assertIsNone(filtered.records[0].active_device)
        self.assertEqual(filtered.records[0].entitlements, ())

        invalid_calls = (
            lambda: reads.list_admin_accounts(limit=True, offset=0),
            lambda: reads.list_admin_accounts(limit=101, offset=0),
            lambda: reads.list_admin_accounts(limit=1, offset=100_001),
            lambda: reads.list_admin_licenses(
                account_id=None,
                limit=1,
                offset=0,
                entitlements_limit=0,
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_projection_survives_repository_restart(self) -> None:
        self._commerce().close()
        self.commerce = None

        reopened = SQLiteCommerceRepository(
            self.database,
            clock=lambda: NOW,
            artifact_verifier=_AcceptingArtifactVerifier(),
        )
        reopened.close()
        reads = SQLiteProductReadStore(self.database)

        self.assertEqual(reads.list_admin_accounts(limit=10, offset=0).total, 2)
        licenses = reads.list_admin_licenses(
            account_id=self.primary_account.id,
            limit=10,
            offset=0,
            entitlements_limit=10,
        )
        self.assertEqual(licenses.records[0].entitlement_count, 2)
        self.assertEqual(reads.list_admin_releases(limit=10, offset=0).total, 3)


class AdminProjectionHttpTests(AdminQueryTestCase):
    def test_non_admin_is_denied_and_authenticated_admin_gets_private_views(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            for path in (
                "/api/admin/accounts",
                "/api/admin/licenses",
                "/api/admin/releases",
            ):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 401)

            login = client.post(
                "/api/admin/session",
                json={"subject": ADMIN_SUBJECT, "password": ADMIN_PASSWORD},
            )
            self.assertEqual(login.status_code, 200, login.text)

            accounts = client.get("/api/admin/accounts?limit=1&offset=0")
            self.assertEqual(accounts.status_code, 200, accounts.text)
            self.assertEqual(accounts.json()["pagination"]["total"], 2)
            self.assertTrue(accounts.json()["pagination"]["has_more"])

            licenses = client.get(
                "/api/admin/licenses",
                params={
                    "account_id": self.primary_account.id,
                    "entitlements_limit": 1,
                },
            )
            self.assertEqual(licenses.status_code, 200, licenses.text)
            license_payload = licenses.json()["licenses"][0]
            self.assertEqual(license_payload["entitlement_count"], 2)
            self.assertTrue(license_payload["entitlements_truncated"])
            self.assertEqual(
                license_payload["active_device"]["platform"],
                "linux",
            )

            releases = client.get("/api/admin/releases")
            self.assertEqual(releases.status_code, 200, releases.text)
            release_payloads = {
                item["version"]: item for item in releases.json()["releases"]
            }
            self.assertEqual(release_payloads["2.0.0"]["state"], "draft")
            self.assertEqual(release_payloads["2.0.0"]["price_minor"], 175_000)

            combined = accounts.text + licenses.text + releases.text
            self.assertNotIn("storage_key", combined)
            self.assertNotIn("signing_key_id", combined)
            self.assertNotIn("granted_by_payment_id", combined)
            self.assertNotIn("private.bin", combined)

    def test_http_bounds_and_invalid_filter_fail_closed(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            login = client.post(
                "/api/admin/session",
                json={"subject": ADMIN_SUBJECT, "password": ADMIN_PASSWORD},
            )
            self.assertEqual(login.status_code, 200, login.text)

            for path in (
                "/api/admin/accounts?limit=0",
                "/api/admin/accounts?limit=101",
                "/api/admin/accounts?offset=100001",
                "/api/admin/licenses?entitlements_limit=0",
                "/api/admin/licenses?entitlements_limit=101",
            ):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 422)

            invalid_filter = client.get(
                "/api/admin/licenses",
                params={"account_id": "invalid account id"},
            )
            self.assertEqual(invalid_filter.status_code, 400)
            self.assertEqual(invalid_filter.json(), {"detail": "request is invalid"})

    def test_read_dependency_failure_is_sanitized_and_honest(self) -> None:
        unavailable_reads = Mock(spec=ProductReadStore)
        unavailable_reads.list_admin_accounts.side_effect = (
            ProductReadNotAvailableError("sensitive database detail")
        )
        app = self._app(reads=unavailable_reads)
        with TestClient(app) as client:
            login = client.post(
                "/api/admin/session",
                json={"subject": ADMIN_SUBJECT, "password": ADMIN_PASSWORD},
            )
            self.assertEqual(login.status_code, 200, login.text)

            response = client.get("/api/admin/accounts")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "backend dependency is not available"},
        )
        self.assertNotIn("sensitive database detail", response.text)


if __name__ == "__main__":
    unittest.main()

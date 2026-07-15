from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.device_identity import DeviceIdentityManager
from core.entitlement_cache import SignedEntitlementCache
from core.product_api_client import ProductApiClient
from core.product_purchase import (
    STATUS_ENTITLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PURCHASE_REQUIRED,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    ProductPurchaseService,
)
from core.product_version import PRODUCT_ID
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)
from product_backend.api_activation import SQLiteClientActivationService
from product_backend.api_app import create_product_backend_app
from product_backend.api_artifact_storage import LocalReadOnlyReleaseArtifactStore
from product_backend.api_auth import AdminAuthSettings, AdminPasswordCredential
from product_backend.api_queries import SQLiteProductReadStore
from product_backend.api_signing import InjectedEd25519EntitlementSigner
from product_backend.device_challenges import SQLiteDeviceChallengeService
from product_backend.models import ArtifactVerificationReceipt
from product_backend.payment_instructions import (
    PAYMENT_INSTRUCTIONS_SCHEMA,
    load_payment_instructions,
)
from product_backend.private_storage import PrivateObjectMetadata
from product_backend.sqlite_repository import SQLiteCommerceRepository


NOW = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
ENTITLEMENT_KEY_ID = "entitlement-initial-e2e"
PURCHASE_ID = "purchase_" + ("1" * 32)
FIRST_SUBMISSION_ID = "purchase_" + ("2" * 32)
SECOND_SUBMISSION_ID = "purchase_" + ("3" * 32)
PAID_AT = "2026-07-14T03:59:00Z"
EVIDENCE = b"sanitized-private-test-image"


class _MemorySecureStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service: str, account: str) -> SecureStoreResult:
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND, message="not found")
        return SecureStoreResult(STORE_SUCCESS, value=value, message="loaded")

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS, message="stored")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS, message="deleted")


class _ReceiptVerifier:
    def verify(self, candidate):
        return ArtifactVerificationReceipt(
            "2026-07-14T04:00:00Z",
            candidate.signing_key_id,
        )


class _MemoryEvidenceStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store_payment_screenshot(self, content, *, content_type, now=None):
        key = f"payments/private/evidence-{len(self.objects) + 1}.png"
        self.objects[key] = content
        return PrivateObjectMetadata(
            key,
            hashlib.sha256(content).hexdigest(),
            len(content),
            content_type,
            "2026-07-14T04:00:00Z",
        )

    def read_private_object(self, metadata, *, maximum_bytes):
        return self.objects[metadata.storage_key]

    def discard_payment_screenshot(self, metadata):
        self.objects.pop(metadata.storage_key, None)


class _ResponseAdapter:
    def __init__(self, response) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self._content = response.content
        self._offset = 0
        self._url = str(response.url)

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self._content) - self._offset
        result = self._content[self._offset : self._offset + amount]
        self._offset += len(result)
        return result

    def close(self):
        return None

    def geturl(self):
        return self._url


class _ASGITransport:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def open(self, *, method, url, headers, body, timeout_seconds):
        response = self.client.request(
            method,
            url,
            headers=headers,
            content=body,
        )
        return _ResponseAdapter(response)


class ProductInitialPurchaseE2ETests(unittest.TestCase):
    def test_fresh_purchase_reject_resubmit_approve_and_signed_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            commerce = SQLiteCommerceRepository(
                root / "commerce.sqlite3",
                artifact_verifier=_ReceiptVerifier(),
                clock=lambda: NOW,
            )
            release = commerce.create_release(
                "1.0.0",
                price_minor=125_000,
                currency="UZS",
                features_en="Fresh release",
                features_ru="Новый выпуск",
                fixes_en="Verified fixes",
                fixes_ru="Проверенные исправления",
            )
            commerce.add_release_artifact(
                release.id,
                platform="macos",
                architecture="arm64",
                build=1,
                sha256="a" * 64,
                byte_size=1024,
                storage_key="releases/1.0.0/Jarvis.dmg",
                signature="A" * 86,
                signing_key_id="release-initial-e2e",
            )
            commerce.publish_release(release.id)
            challenges = SQLiteDeviceChallengeService(
                commerce,
                root / "challenges.sqlite3",
                clock=lambda: NOW,
            )
            entitlement_key = Ed25519PrivateKey.generate()
            activation = SQLiteClientActivationService(
                commerce,
                InjectedEd25519EntitlementSigner(
                    entitlement_key,
                    key_id=ENTITLEMENT_KEY_ID,
                ),
                b"initial-e2e-activation-pepper-32b",
                root / "activation.sqlite3",
                clock=lambda: NOW,
            )
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            evidence_store = _MemoryEvidenceStore()
            payment_path = root / "payment-instructions.json"
            payment_path.write_text(
                json.dumps(
                    {
                        "schema": PAYMENT_INSTRUCTIONS_SCHEMA,
                        "recipient": "TEST-DESTINATION-NOT-REAL",
                        "method": {
                            "en": "Test transfer",
                            "ru": "Тестовый перевод",
                        },
                        "instructions": {
                            "en": "Use only the local fixture.",
                            "ru": "Используйте только локальный тест.",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            payment_path.chmod(0o600)
            credential = AdminPasswordCredential.derive_for_configuration(
                subject="admin:initial-e2e",
                password="strong-initial-e2e-password",
                salt=b"s" * 32,
            )
            settings = AdminAuthSettings(
                (credential,),
                b"initial-e2e-session-secret-32bytes",
                ("testserver",),
            )
            app = create_product_backend_app(
                commerce=commerce,
                reads=SQLiteProductReadStore(root / "commerce.sqlite3"),
                evidence_store=evidence_store,
                challenges=challenges,
                activation=activation,
                release_artifact_store=LocalReadOnlyReleaseArtifactStore(
                    artifact_root,
                    maximum_artifact_bytes=2048,
                ),
                auth_settings=settings,
                payment_instructions=load_payment_instructions(payment_path),
                clock=lambda: NOW,
            )
            identity_manager = DeviceIdentityManager(
                _MemorySecureStore(),
                creation_lock_path=str(root / "identity.lock"),
            )
            public_key = entitlement_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            cache = SignedEntitlementCache(
                root / "entitlements",
                trusted_public_keys={ENTITLEMENT_KEY_ID: public_key},
            )
            try:
                with TestClient(app, base_url="https://testserver") as client:
                    api = ProductApiClient(
                        "https://testserver",
                        transport=_ASGITransport(client),
                    )
                    purchase = ProductPurchaseService(
                        api,
                        identity_manager,
                        cache,
                    )
                    prepared = purchase.prepare_initial_purchase(
                        purchase_id=PURCHASE_ID,
                        version="1.0.0",
                        platform="macos",
                        architecture="arm64",
                    )
                    self.assertEqual(prepared.status, STATUS_PURCHASE_REQUIRED)
                    self.assertTrue(prepared.ready)
                    assert prepared.offer is not None
                    self.assertEqual(prepared.offer.price_minor, 125_000)
                    self.assertEqual(prepared.offer.currency, "UZS")
                    self.assertEqual(
                        prepared.offer.recipient,
                        "TEST-DESTINATION-NOT-REAL",
                    )

                    # MIME rejection must release, not consume, the reserved grant.
                    invalid = client.post(
                        f"/api/purchases/{PURCHASE_ID}/releases/"
                        f"{release.id}/payments",
                        headers={
                            "X-Purchase-Grant": prepared.offer.purchase_grant
                        },
                        data={
                            "paid_at": PAID_AT,
                            "client_submission_id": FIRST_SUBMISSION_ID,
                        },
                        files={"file": ("receipt.txt", b"invalid", "text/plain")},
                    )
                    self.assertEqual(invalid.status_code, 400, invalid.text)

                    submitted = purchase.submit_initial_payment(
                        prepared.offer,
                        paid_at=PAID_AT,
                        screenshot=EVIDENCE,
                        content_type="image/png",
                        submission_id=FIRST_SUBMISSION_ID,
                    )
                    self.assertEqual(submitted.status, STATUS_SUBMITTED)
                    self.assertIsNotNone(submitted.license_id)
                    self.assertIsNotNone(submitted.payment_id)
                    assert submitted.license_id is not None
                    assert submitted.payment_id is not None

                    replay = purchase.submit_initial_payment(
                        prepared.offer,
                        paid_at=PAID_AT,
                        screenshot=EVIDENCE,
                        content_type="image/png",
                        submission_id=FIRST_SUBMISSION_ID,
                    )
                    self.assertEqual(replay.status, STATUS_FAILED)

                    # A new proof grant recovers a lost response idempotently.
                    retry_offer = purchase.prepare_initial_purchase(
                        purchase_id=PURCHASE_ID,
                        version="1.0.0",
                        platform="macos",
                        architecture="arm64",
                    )
                    assert retry_offer.offer is not None
                    retried = purchase.submit_initial_payment(
                        retry_offer.offer,
                        paid_at=PAID_AT,
                        screenshot=EVIDENCE,
                        content_type="image/png",
                        submission_id=FIRST_SUBMISSION_ID,
                    )
                    self.assertEqual(retried.status, STATUS_SUBMITTED)
                    self.assertEqual(retried.payment_id, submitted.payment_id)
                    self.assertEqual(retried.license_id, submitted.license_id)
                    self.assertEqual(len(evidence_store.objects), 1)

                    pending = purchase.poll_status(
                        license_id=submitted.license_id,
                        version="1.0.0",
                    )
                    self.assertEqual(pending.status, STATUS_PENDING)
                    unauthenticated_evidence = client.get(
                        f"/api/admin/payments/{submitted.payment_id}/evidence"
                    )
                    self.assertEqual(unauthenticated_evidence.status_code, 401)

                    login = client.post(
                        "/api/admin/session",
                        json={
                            "subject": "admin:initial-e2e",
                            "password": "strong-initial-e2e-password",
                        },
                    )
                    self.assertEqual(login.status_code, 200, login.text)
                    admin_headers = {
                        "X-CSRF-Token": login.json()["csrf_token"]
                    }
                    queue = client.get("/api/admin/payments")
                    self.assertEqual(queue.status_code, 200, queue.text)
                    self.assertEqual(
                        queue.json()["payments"][0]["id"],
                        submitted.payment_id,
                    )
                    self.assertNotIn(
                        "storage_key",
                        queue.text,
                    )
                    reviewed = client.post(
                        f"/api/admin/payments/{submitted.payment_id}/review",
                        headers=admin_headers,
                    )
                    self.assertEqual(reviewed.status_code, 200, reviewed.text)
                    rejected = client.post(
                        f"/api/admin/payments/{submitted.payment_id}/reject",
                        headers=admin_headers,
                        json={"reason": "Receipt is unreadable"},
                    )
                    self.assertEqual(rejected.status_code, 200, rejected.text)
                    rejected_status = purchase.poll_status(
                        license_id=submitted.license_id,
                        version="1.0.0",
                    )
                    self.assertEqual(rejected_status.status, STATUS_REJECTED)
                    self.assertEqual(
                        rejected_status.rejection_reason,
                        "Receipt is unreadable",
                    )

                    resubmit_offer = purchase.prepare_initial_purchase(
                        purchase_id=PURCHASE_ID,
                        version="1.0.0",
                        platform="macos",
                        architecture="arm64",
                    )
                    assert resubmit_offer.offer is not None
                    resubmitted = purchase.submit_initial_payment(
                        resubmit_offer.offer,
                        paid_at=PAID_AT,
                        screenshot=EVIDENCE,
                        content_type="image/png",
                        submission_id=SECOND_SUBMISSION_ID,
                        supersedes_payment_id=submitted.payment_id,
                    )
                    self.assertEqual(resubmitted.status, STATUS_SUBMITTED)
                    self.assertNotEqual(resubmitted.payment_id, submitted.payment_id)
                    assert resubmitted.payment_id is not None
                    self.assertEqual(
                        client.post(
                            f"/api/admin/payments/{resubmitted.payment_id}/review",
                            headers=admin_headers,
                        ).status_code,
                        200,
                    )
                    approved = client.post(
                        f"/api/admin/payments/{resubmitted.payment_id}/approve",
                        headers=admin_headers,
                    )
                    self.assertEqual(approved.status_code, 200, approved.text)
                    duplicate_approve = client.post(
                        f"/api/admin/payments/{resubmitted.payment_id}/approve",
                        headers=admin_headers,
                    )
                    self.assertEqual(
                        duplicate_approve.status_code,
                        200,
                        duplicate_approve.text,
                    )
                    self.assertTrue(duplicate_approve.json()["idempotent"])

                    entitled = purchase.poll_status(
                        license_id=submitted.license_id,
                        version="1.0.0",
                    )
                    self.assertEqual(entitled.status, STATUS_ENTITLED)
                    self.assertTrue(entitled.entitled)
                    self.assertTrue(
                        cache.load_verified(
                            license_id=submitted.license_id,
                            device_fingerprint=(
                                identity_manager.load().identity.fingerprint
                            ),
                            version="1.0.0",
                        ).ok
                    )

                    wrong_version = purchase.prepare_initial_purchase(
                        purchase_id="purchase_" + ("4" * 32),
                        version="1.1.0",
                        platform="macos",
                        architecture="arm64",
                    )
                    self.assertNotEqual(
                        wrong_version.status,
                        STATUS_PURCHASE_REQUIRED,
                    )

                    # Random purchase identifiers must not create a fresh
                    # rate-limit bucket for every request from one client.
                    fingerprint = identity_manager.load().identity.fingerprint
                    abuse_statuses = []
                    for index in range(24):
                        abuse_statuses.append(
                            client.post(
                                "/api/purchases/challenges",
                                json={
                                    "product_id": PRODUCT_ID,
                                    "purchase_id": f"purchase_{index:032x}",
                                    "version": "1.0.0",
                                    "device_key_fingerprint": fingerprint,
                                    "platform": "macos",
                                    "architecture": "arm64",
                                },
                            ).status_code
                        )
                    self.assertEqual(abuse_statuses[-1], 429)
                    self.assertIn(201, abuse_statuses)
            finally:
                activation.close()
                challenges.close()
                commerce.close()


if __name__ == "__main__":
    unittest.main()

"""API-level regression for the initial-purchase grant-expiry race (P1-1).

A slow or large upload can push the wall clock past the one-time upload grant's
TTL between the moment the request reserves the grant and the moment the handler
commits it.  The payment row is already durably persisted by then, so the commit
must stay deterministic; it must never convert an accepted, stored payment into a
spurious 503.  This test drives the real ASGI middleware and handler with a
shared mutable clock that jumps forward while the private evidence is being
written -- exactly between ``reserve_grant`` and ``commit_grant``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.device_identity import DeviceIdentityManager
from core.entitlement_cache import SignedEntitlementCache
from core.product_api_client import ProductApiClient
from core.product_purchase import STATUS_SUBMITTED, ProductPurchaseService
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
from product_backend.initial_purchase import DEFAULT_GRANT_TTL_SECONDS
from product_backend.models import ArtifactVerificationReceipt
from product_backend.payment_instructions import (
    PAYMENT_INSTRUCTIONS_SCHEMA,
    load_payment_instructions,
)
from product_backend.private_storage import PrivateObjectMetadata
from product_backend.sqlite_repository import SQLiteCommerceRepository


PURCHASE_ID = "purchase_" + ("5" * 32)
SUBMISSION_ID = "purchase_" + ("6" * 32)
PAID_AT = "2026-07-14T03:59:00Z"
EVIDENCE = b"sanitized-private-test-image"


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class _ReceiptVerifier:
    def verify(self, candidate):
        return ArtifactVerificationReceipt(
            "2026-07-14T04:00:00Z",
            candidate.signing_key_id,
        )


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


class _SlowUploadEvidenceStore:
    """Evidence store that simulates a slow upload by advancing the clock.

    The advance happens inside ``store_payment_screenshot`` -- after the request
    reserved its grant, and before the handler commits it -- which is the exact
    window in which the grant expires under the P1 race.
    """

    def __init__(self, clock: _MutableClock) -> None:
        self._clock = clock
        self.objects: dict[str, bytes] = {}

    def store_payment_screenshot(self, content, *, content_type, now=None):
        self._clock.value += timedelta(seconds=DEFAULT_GRANT_TTL_SECONDS + 60)
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
        response = self.client.request(method, url, headers=headers, content=body)
        return _ResponseAdapter(response)


class InitialPurchaseGrantRaceTests(unittest.TestCase):
    def test_slow_upload_grant_expiry_does_not_spuriously_fail_persisted_payment(
        self,
    ) -> None:
        clock = _MutableClock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            commerce = SQLiteCommerceRepository(
                root / "commerce.sqlite3",
                artifact_verifier=_ReceiptVerifier(),
                clock=clock,
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
                signing_key_id="release-grant-race",
            )
            commerce.publish_release(release.id)
            challenges = SQLiteDeviceChallengeService(
                commerce,
                root / "challenges.sqlite3",
                clock=clock,
            )
            entitlement_key = Ed25519PrivateKey.generate()
            activation = SQLiteClientActivationService(
                commerce,
                InjectedEd25519EntitlementSigner(
                    entitlement_key,
                    key_id="entitlement-grant-race",
                ),
                b"grant-race-activation-pepper-32by",
                root / "activation.sqlite3",
                clock=clock,
            )
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            evidence_store = _SlowUploadEvidenceStore(clock)
            payment_path = root / "payment-instructions.json"
            payment_path.write_text(
                json.dumps(
                    {
                        "schema": PAYMENT_INSTRUCTIONS_SCHEMA,
                        "recipient": "TEST-DESTINATION-NOT-REAL",
                        "method": {"en": "Test transfer", "ru": "Тестовый перевод"},
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
                subject="admin:grant-race",
                password="strong-grant-race-password",
                salt=b"s" * 32,
            )
            settings = AdminAuthSettings(
                (credential,),
                b"grant-race-session-secret-32bytesx",
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
                allow_password_only_admin=True,
                payment_instructions=load_payment_instructions(payment_path),
                clock=clock,
            )
            cache = SignedEntitlementCache(
                root / "entitlements",
                trusted_public_keys={},
            )
            try:
                with TestClient(app, base_url="https://testserver") as client:
                    api = ProductApiClient(
                        "https://testserver",
                        transport=_ASGITransport(client),
                    )
                    purchase = ProductPurchaseService(
                        api,
                        DeviceIdentityManager(
                            _MemorySecureStore(),
                            creation_lock_path=str(root / "identity.lock"),
                        ),
                        cache,
                    )
                    prepared = purchase.prepare_initial_purchase(
                        purchase_id=PURCHASE_ID,
                        version="1.0.0",
                        platform="macos",
                        architecture="arm64",
                    )
                    assert prepared.offer is not None

                    # The upload advances the clock past the grant TTL while the
                    # evidence is written; the payment is persisted, so the
                    # response must be a real success, never a 503.
                    submitted = purchase.submit_initial_payment(
                        prepared.offer,
                        paid_at=PAID_AT,
                        screenshot=EVIDENCE,
                        content_type="image/png",
                        submission_id=SUBMISSION_ID,
                    )
                    self.assertEqual(submitted.status, STATUS_SUBMITTED, submitted.message)
                    self.assertIsNotNone(submitted.payment_id)
                    self.assertIsNotNone(submitted.license_id)

                    # A lost-response retry with a fresh grant and the same
                    # idempotency key resolves to the same payment -- no
                    # duplicate is created despite the earlier clock jumps.
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
                        submission_id=SUBMISSION_ID,
                    )
                    self.assertEqual(retried.status, STATUS_SUBMITTED, retried.message)
                    self.assertEqual(retried.payment_id, submitted.payment_id)
                    self.assertEqual(retried.license_id, submitted.license_id)
                    self.assertEqual(len(evidence_store.objects), 1)
            finally:
                activation.close()
                challenges.close()
                commerce.close()


if __name__ == "__main__":
    unittest.main()

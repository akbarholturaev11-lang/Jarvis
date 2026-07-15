from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentityManager
from core.product_api_client import ProductApiClient, ProductApiError
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)
from product_backend.api_app import create_product_backend_app
from product_backend.api_activation import SQLiteClientActivationService
from product_backend.api_artifact_storage import LocalReadOnlyReleaseArtifactStore
from product_backend.api_auth import AdminAuthSettings, AdminPasswordCredential
from product_backend.api_queries import SQLiteProductReadStore
from product_backend.api_signing import InjectedEd25519EntitlementSigner
from product_backend.device_challenges import SQLiteDeviceChallengeService
from product_backend.models import ArtifactVerificationReceipt
from product_backend.private_storage import PrivateObjectMetadata
from product_backend.sqlite_repository import SQLiteCommerceRepository


class MemorySecureStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service, account):
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND, message="not found")
        return SecureStoreResult(STORE_SUCCESS, value=value, message="loaded")

    def _set(self, service, account, secret):
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS, message="stored")

    def _delete(self, service, account):
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS, message="deleted")


class ReceiptVerifier:
    def verify(self, candidate):
        return ArtifactVerificationReceipt(
            "2026-07-13T03:00:00Z",
            candidate.signing_key_id,
        )


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self.objects = {}

    def store_payment_screenshot(self, content, *, content_type, now=None):
        key = "payments/test/evidence.png"
        self.objects[key] = content
        return PrivateObjectMetadata(
            key,
            hashlib.sha256(content).hexdigest(),
            len(content),
            content_type,
            "2026-07-13T03:00:00Z",
        )

    def read_private_object(self, metadata, *, maximum_bytes):
        return self.objects[metadata.storage_key]

    def discard_payment_screenshot(self, metadata):
        self.objects.pop(metadata.storage_key, None)


class ASGIResponseAdapter:
    def __init__(self, response) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self._raw = response.content
        self._offset = 0
        self._url = str(response.url)

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self._raw) - self._offset
        chunk = self._raw[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass

    def geturl(self):
        return self._url


class ASGIClientTransport:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def open(self, *, method, url, headers, body, timeout_seconds):
        response = self.client.request(
            method,
            url,
            headers=headers,
            content=body,
        )
        return ASGIResponseAdapter(response)


class ProductClientBackendContractTests(unittest.TestCase):
    def test_real_backend_challenge_proof_and_single_use_grant_match_client(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            commerce = SQLiteCommerceRepository(
                root / "commerce.sqlite3",
                artifact_verifier=ReceiptVerifier(),
            )
            identity_manager = DeviceIdentityManager(
                MemorySecureStore(),
                creation_lock_path=str(root / "identity.lock"),
            )
            identity = identity_manager.get_or_create().identity
            account = commerce.create_account("buyer:contract-001")
            license_record = commerce.issue_license(account.id)
            commerce.activate_device(
                license_record.id,
                identity.fingerprint,
                platform="macos",
                architecture="arm64",
            )
            release = commerce.create_release(
                "1.0.0",
                price_minor=100_000,
                currency="UZS",
            )
            commerce.add_release_artifact(
                release.id,
                platform="macos",
                architecture="arm64",
                build=1,
                sha256="a" * 64,
                byte_size=1000,
                storage_key="releases/1.0.0/Jarvis.dmg",
                signature="A" * 86,
                signing_key_id="release-key-001",
            )
            commerce.publish_release(release.id)
            challenges = SQLiteDeviceChallengeService(
                commerce,
                root / "challenges.sqlite3",
                clock=lambda: datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc),
            )
            entitlement_key = Ed25519PrivateKey.generate()
            activation = SQLiteClientActivationService(
                commerce,
                InjectedEd25519EntitlementSigner(
                    entitlement_key,
                    key_id="entitlement-key-001",
                ),
                b"activation-pepper-for-tests-32-bytes-long",
                root / "activation.sqlite3",
                clock=lambda: datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc),
            )
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            reads = SQLiteProductReadStore(root / "commerce.sqlite3")
            credential = AdminPasswordCredential.derive_for_configuration(
                subject="admin:contract-001",
                password="test-password-strong",
                salt=b"s" * 32,
            )
            settings = AdminAuthSettings(
                (credential,),
                b"session-secret-for-tests-only-32b",
                ("testserver",),
            )
            app = create_product_backend_app(
                commerce=commerce,
                reads=reads,
                evidence_store=MemoryEvidenceStore(),
                challenges=challenges,
                activation=activation,
                release_artifact_store=LocalReadOnlyReleaseArtifactStore(
                    artifact_root,
                    maximum_artifact_bytes=1024,
                ),
                auth_settings=settings,
                allow_password_only_admin=True,
                clock=lambda: datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc),
            )
            try:
                with TestClient(app, base_url="https://testserver") as test_client:
                    api = ProductApiClient(
                        "https://testserver",
                        transport=ASGIClientTransport(test_client),
                    )
                    issued = api.request_json(
                        "POST",
                        "/api/device-challenges",
                        payload={
                            "license_id": license_record.id,
                            "device_key_fingerprint": identity.fingerprint,
                            "action": "fetch_entitlement",
                            "resource_id": "1.0.0",
                        },
                    )
                    signature = identity.sign_challenge(issued["challenge_nonce"])
                    grant = api.request_json(
                        "POST",
                        f"/api/device-challenges/{issued['challenge_id']}/verify",
                        payload={
                            "challenge_nonce": issued["challenge_nonce"],
                            "public_key_base64": identity.public_key_base64,
                            "signature_base64": signature,
                        },
                    )
                    status = api.request_json(
                        "GET",
                        f"/api/customer/licenses/{license_record.id}/versions/1.0.0/status",
                        headers={"X-Device-Grant": grant["device_grant"]},
                    )
                    with self.assertRaises(ProductApiError):
                        api.request_json(
                            "GET",
                            f"/api/customer/licenses/{license_record.id}/versions/1.0.0/status",
                            headers={"X-Device-Grant": grant["device_grant"]},
                        )

                self.assertEqual(grant["action"], "fetch_entitlement")
                self.assertEqual(grant["resource_id"], "1.0.0")
                self.assertEqual(status["version"], "1.0.0")
                self.assertFalse(status["entitled"])
                self.assertNotIn(grant["device_grant"], repr(api))
            finally:
                activation.close()
                challenges.close()
                commerce.close()


if __name__ == "__main__":
    unittest.main()

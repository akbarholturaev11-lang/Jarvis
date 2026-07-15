from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.device_identity import DeviceIdentity
from core.entitlement_certificate import (
    STATUS_SUCCESS as CERTIFICATE_SUCCESS,
    verify_entitlement_certificate,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.release_manifest import (
    MANIFEST_SCHEMA,
    SCHEMA_VERSION,
    STATUS_SUCCESS as MANIFEST_SUCCESS,
    ArtifactKind,
    verify_release_manifest,
)
from product_backend.api_activation import SQLiteClientActivationService
from product_backend.api_app import create_product_backend_app
from product_backend.api_artifact_storage import (
    LocalReadOnlyReleaseArtifactStore,
)
from product_backend.api_auth import AdminAuthSettings, AdminPasswordCredential
from product_backend.api_queries import SQLiteProductReadStore
from product_backend.api_signing import InjectedEd25519EntitlementSigner
from product_backend.device_challenges import SQLiteDeviceChallengeService
from product_backend.models import ArtifactVerificationCandidate
from product_backend.payment_instructions import (
    PAYMENT_INSTRUCTIONS_SCHEMA,
    load_payment_instructions,
)
from product_backend.private_storage import PrivateObjectMetadata
from product_backend.release_verifier import PinnedReleaseArtifactVerifier
from product_backend.sqlite_repository import SQLiteCommerceRepository


NOW = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)
RELEASE_KEY_ID = "release-key-001"
ENTITLEMENT_KEY_ID = "entitlement-key-001"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _artifact_signature(
    private_key: Ed25519PrivateKey,
    *,
    version: str,
    build: int,
    kind: ArtifactKind,
    sha256: str,
    byte_size: int,
    storage_key: str,
    sources: tuple[str, ...],
) -> str:
    candidate = ArtifactVerificationCandidate(
        PRODUCT_ID,
        BUNDLE_ID,
        version,
        "macos",
        "arm64",
        kind,
        build,
        sha256,
        byte_size,
        storage_key,
        "A" * 86,
        RELEASE_KEY_ID,
        sources,
    )
    payload = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": candidate.product_id,
        "bundle_id": candidate.bundle_id,
        "version": candidate.release_version,
        "build": candidate.build,
        "platform": candidate.platform,
        "architecture": candidate.architecture,
        "artifact_kind": candidate.artifact_kind.value,
        "sha256": candidate.sha256,
        "byte_size": candidate.byte_size,
        "storage_key": candidate.storage_key,
        "signing_key_id": candidate.signing_key_id,
        "compatible_source_versions": list(
            candidate.compatible_source_versions
        ),
    }
    return _b64url(private_key.sign(_canonical(payload)))


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store_payment_screenshot(self, content, *, content_type, now=None):
        key = f"payments/evidence-{len(self.objects) + 1}.png"
        self.objects[key] = content
        timestamp = NOW if now is None else now
        return PrivateObjectMetadata(
            key,
            hashlib.sha256(content).hexdigest(),
            len(content),
            content_type,
            timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

    def read_private_object(self, metadata, *, maximum_bytes):
        return self.objects[metadata.storage_key]

    def discard_payment_screenshot(self, metadata):
        self.objects.pop(metadata.storage_key, None)


class ProductBackendMvpTests(unittest.TestCase):
    def test_manual_sale_activation_paid_update_and_single_use_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            (artifact_root / "releases").mkdir(mode=0o700)

            initial_bytes = b"jarvis-initial-package"
            update_bytes = b"jarvis-update-package"
            initial_path = artifact_root / "releases" / "1.0.0.dmg"
            update_path = artifact_root / "releases" / "1.1.0.update"
            initial_path.write_bytes(initial_bytes)
            update_path.write_bytes(update_bytes)
            initial_path.chmod(0o600)
            update_path.chmod(0o600)

            release_key = Ed25519PrivateKey.generate()
            entitlement_key = Ed25519PrivateKey.generate()
            release_verifier = PinnedReleaseArtifactVerifier(
                {RELEASE_KEY_ID: release_key.public_key()},
                clock=lambda: NOW,
            )
            commerce = SQLiteCommerceRepository(
                root / "commerce.sqlite3",
                artifact_verifier=release_verifier,
                clock=lambda: NOW,
            )
            challenges = SQLiteDeviceChallengeService(
                commerce,
                root / "device-challenges.sqlite3",
                clock=lambda: NOW,
            )
            signer = InjectedEd25519EntitlementSigner(
                entitlement_key,
                key_id=ENTITLEMENT_KEY_ID,
            )
            activation = SQLiteClientActivationService(
                commerce,
                signer,
                b"activation-pepper-for-tests-32-bytes-long",
                root / "activation.sqlite3",
                clock=lambda: NOW,
            )
            reads = SQLiteProductReadStore(root / "commerce.sqlite3")
            credential = AdminPasswordCredential.derive_for_configuration(
                subject="admin:test",
                password="a-strong-test-password",
                salt=b"s" * 32,
            )
            settings = AdminAuthSettings(
                (credential,),
                b"session-secret-for-tests-32-bytes",
                ("testserver",),
                secure_cookie=False,
            )
            payment_config_path = root / "payment-instructions.json"
            payment_config_path.write_text(
                json.dumps(
                    {
                        "schema": PAYMENT_INSTRUCTIONS_SCHEMA,
                        "recipient": "TEST-RECIPIENT-NOT-REAL",
                        "method": {
                            "en": "Test payment method",
                            "ru": "Тестовый способ оплаты",
                        },
                        "instructions": {
                            "en": "Use only this non-real test destination.",
                            "ru": "Используйте только эти нереальные тестовые реквизиты.",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            payment_config_path.chmod(0o600)
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
                payment_instructions=load_payment_instructions(
                    payment_config_path
                ),
                clock=lambda: NOW,
            )

            device = DeviceIdentity(Ed25519PrivateKey.generate())
            try:
                with TestClient(app) as client:
                    login = client.post(
                        "/api/admin/session",
                        json={
                            "subject": "admin:test",
                            "password": "a-strong-test-password",
                        },
                    )
                    self.assertEqual(login.status_code, 200)
                    csrf = login.json()["csrf_token"]
                    admin_headers = {"X-CSRF-Token": csrf}

                    account_response = client.post(
                        "/api/admin/accounts",
                        json={"external_subject": "buyer:test-001"},
                        headers=admin_headers,
                    )
                    self.assertEqual(
                        account_response.status_code, 201, account_response.text
                    )
                    account = account_response.json()
                    license_record = client.post(
                        f"/api/admin/accounts/{account['account_id']}/licenses",
                        headers=admin_headers,
                    ).json()
                    license_id = license_record["license_id"]
                    bound = client.post(
                        f"/api/admin/licenses/{license_id}/devices",
                        json={
                            "device_key_fingerprint": device.fingerprint,
                            "platform": "macos",
                            "architecture": "arm64",
                            "device_label": "Test Mac",
                        },
                        headers=admin_headers,
                    )
                    self.assertEqual(bound.status_code, 201)

                    initial_release = self._publish_release(
                        client,
                        admin_headers,
                        release_key,
                        version="1.0.0",
                        price_minor=100_000,
                        build=1,
                        kind=ArtifactKind.INITIAL_INSTALLER,
                        content=initial_bytes,
                        storage_key="releases/1.0.0.dmg",
                        sources=(),
                    )
                    payment = self._submit_payment(
                        client,
                        device,
                        license_id,
                        initial_release,
                    )
                    self._approve_payment(
                        client, admin_headers, payment["id"]
                    )

                    issued_key = client.post(
                        f"/api/admin/licenses/{license_id}/versions/1.0.0/"
                        "activation-credentials",
                        headers=admin_headers,
                    ).json()
                    activation_db = (root / "activation.sqlite3").read_bytes()
                    self.assertNotIn(
                        issued_key["license_key"].encode("utf-8"), activation_db
                    )
                    wrong_device = DeviceIdentity(Ed25519PrivateKey.generate())
                    mismatch_challenge = client.post(
                        "/v1/client/activation/challenge",
                        json={
                            "product_id": PRODUCT_ID,
                            "license_key": issued_key["license_key"],
                            "device_key_fingerprint": wrong_device.fingerprint,
                            "device_public_key": wrong_device.public_key_base64,
                            "version": "1.0.0",
                            "platform": "macos",
                            "architecture": "arm64",
                        },
                    )
                    self.assertEqual(mismatch_challenge.status_code, 200)
                    mismatch = mismatch_challenge.json()
                    mismatch_complete = client.post(
                        "/v1/client/activation/complete",
                        json={
                            "product_id": PRODUCT_ID,
                            "challenge_id": mismatch["challenge_id"],
                            "challenge_nonce": mismatch["challenge_nonce"],
                            "device_key_fingerprint": wrong_device.fingerprint,
                            "device_public_key": wrong_device.public_key_base64,
                            "challenge_signature": wrong_device.sign_challenge(
                                mismatch["challenge_nonce"]
                            ),
                            "version": "1.0.0",
                            "platform": "macos",
                            "architecture": "arm64",
                        },
                    )
                    self.assertEqual(mismatch_complete.status_code, 409)
                    self.assertEqual(
                        mismatch_complete.headers["X-Jarvis-Error-Code"],
                        "device_mismatch",
                    )
                    self.assertEqual(
                        mismatch_complete.json(),
                        {"detail": "activation conflicts with the active device"},
                    )
                    self.assertNotIn(
                        wrong_device.fingerprint,
                        mismatch_complete.text,
                    )

                    # The same key remains usable after admin-controlled device
                    # reconciliation; mismatch must not consume it.
                    challenge = client.post(
                        "/v1/client/activation/challenge",
                        json={
                            "product_id": PRODUCT_ID,
                            "license_key": issued_key["license_key"],
                            "device_key_fingerprint": device.fingerprint,
                            "device_public_key": device.public_key_base64,
                            "version": "1.0.0",
                            "platform": "macos",
                            "architecture": "arm64",
                        },
                    ).json()
                    completed = client.post(
                        "/v1/client/activation/complete",
                        json={
                            "product_id": PRODUCT_ID,
                            "challenge_id": challenge["challenge_id"],
                            "challenge_nonce": challenge["challenge_nonce"],
                            "device_key_fingerprint": device.fingerprint,
                            "device_public_key": device.public_key_base64,
                            "challenge_signature": device.sign_challenge(
                                challenge["challenge_nonce"]
                            ),
                            "version": "1.0.0",
                            "platform": "macos",
                            "architecture": "arm64",
                        },
                    )
                    self.assertEqual(completed.status_code, 200)
                    self._assert_certificate(
                        completed.json()["entitlement_certificate"],
                        entitlement_key,
                        license_id,
                        device.fingerprint,
                        "1.0.0",
                    )
                    replay = client.post(
                        "/v1/client/activation/complete",
                        json={
                            "product_id": PRODUCT_ID,
                            "challenge_id": challenge["challenge_id"],
                            "challenge_nonce": challenge["challenge_nonce"],
                            "device_key_fingerprint": device.fingerprint,
                            "device_public_key": device.public_key_base64,
                            "challenge_signature": device.sign_challenge(
                                challenge["challenge_nonce"]
                            ),
                            "version": "1.0.0",
                            "platform": "macos",
                            "architecture": "arm64",
                        },
                    )
                    self.assertEqual(replay.status_code, 401)

                    update_release = self._publish_release(
                        client,
                        admin_headers,
                        release_key,
                        version="1.1.0",
                        price_minor=50_000,
                        build=2,
                        kind=ArtifactKind.UPDATE_PACKAGE,
                        content=update_bytes,
                        storage_key="releases/1.1.0.update",
                        sources=("1.0.0",),
                    )
                    update_request = {
                        "product_id": PRODUCT_ID,
                        "license_id": license_id,
                        "device_key_fingerprint": device.fingerprint,
                        "installed_version": "1.0.0",
                        "installed_build": 1,
                        "platform": "macos",
                        "architecture": "arm64",
                    }
                    offered = client.post(
                        "/v1/client/updates/check", json=update_request
                    ).json()
                    self.assertEqual(
                        frozenset(offered),
                        {
                            "state",
                            "manifest",
                            "artifact_id",
                            "release_id",
                            "release_info",
                        },
                    )
                    self.assertEqual(offered["state"], "purchase_required")
                    self.assertNotIn("payment_instructions", offered)
                    self.assertNotIn("TEST-RECIPIENT-NOT-REAL", offered.__repr__())
                    self.assertEqual(offered["release_info"]["price_minor"], 50_000)
                    self.assertEqual(
                        offered["release_info"]["supported_platforms"],
                        ["macos"],
                    )
                    manifest = verify_release_manifest(
                        offered["manifest"],
                        trusted_public_keys={
                            RELEASE_KEY_ID: release_key.public_key()
                        },
                    )
                    self.assertEqual(manifest.status, MANIFEST_SUCCESS)

                    purchase_grant = self._device_grant(
                        client,
                        device,
                        license_id,
                        "authorize_install",
                        offered["artifact_id"],
                    )
                    purchase_offer = client.post(
                        "/v1/client/updates/check",
                        json=update_request,
                        headers={"X-Device-Grant": purchase_grant},
                    ).json()
                    self.assertEqual(purchase_offer["state"], "purchase_required")
                    self.assertEqual(
                        purchase_offer["payment_instructions"]["status"],
                        "configured",
                    )
                    self.assertEqual(
                        purchase_offer["payment_instructions"]["recipient"],
                        "TEST-RECIPIENT-NOT-REAL",
                    )

                    update_payment = self._submit_payment(
                        client,
                        device,
                        license_id,
                        update_release,
                    )
                    pending_status = self._version_status(
                        client, device, license_id, "1.1.0"
                    )
                    self.assertFalse(pending_status["entitled"])
                    self.assertIsNone(
                        pending_status["entitlement_certificate"]
                    )
                    self._approve_payment(
                        client, admin_headers, update_payment["id"]
                    )
                    approved_status = self._version_status(
                        client, device, license_id, "1.1.0"
                    )
                    self.assertTrue(approved_status["entitled"])
                    self._assert_certificate(
                        approved_status["entitlement_certificate"],
                        entitlement_key,
                        license_id,
                        device.fingerprint,
                        "1.1.0",
                    )

                    install_grant = self._device_grant(
                        client,
                        device,
                        license_id,
                        "authorize_install",
                        offered["artifact_id"],
                    )
                    entitled = client.post(
                        "/v1/client/updates/check",
                        json=update_request,
                        headers={"X-Device-Grant": install_grant},
                    ).json()
                    self.assertEqual(entitled["state"], "entitled")
                    self.assertEqual(entitled["manifest"], offered["manifest"])
                    self.assertEqual(
                        entitled["artifact_id"], offered["artifact_id"]
                    )
                    self.assertEqual(
                        entitled["release_id"], offered["release_id"]
                    )
                    self.assertEqual(
                        entitled["release_info"], offered["release_info"]
                    )
                    downloaded = client.get(entitled["download_path"])
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(
                        downloaded.headers["content-length"],
                        str(len(update_bytes)),
                    )
                    self.assertEqual(downloaded.content, update_bytes)
                    self.assertEqual(
                        client.get(entitled["download_path"]).status_code,
                        401,
                    )
            finally:
                activation.close()
                challenges.close()
                commerce.close()

    def _publish_release(
        self,
        client,
        admin_headers,
        release_key,
        *,
        version,
        price_minor,
        build,
        kind,
        content,
        storage_key,
        sources,
    ):
        release = client.post(
            "/api/admin/releases",
            json={
                "version": version,
                "price_minor": price_minor,
                "currency": "UZS",
                "features_en": f"New in {version}",
                "features_ru": f"Новое в {version}",
                "fixes_en": f"Fixes in {version}",
                "fixes_ru": f"Исправления в {version}",
            },
            headers=admin_headers,
        ).json()
        digest = hashlib.sha256(content).hexdigest()
        artifact = client.post(
            f"/api/admin/releases/{release['id']}/artifacts",
            json={
                "platform": "macos",
                "architecture": "arm64",
                "artifact_kind": kind.value,
                "build": build,
                "sha256": digest,
                "byte_size": len(content),
                "storage_key": storage_key,
                "signature": _artifact_signature(
                    release_key,
                    version=version,
                    build=build,
                    kind=kind,
                    sha256=digest,
                    byte_size=len(content),
                    storage_key=storage_key,
                    sources=sources,
                ),
                "signing_key_id": RELEASE_KEY_ID,
                "compatible_source_versions": list(sources),
            },
            headers=admin_headers,
        )
        self.assertEqual(artifact.status_code, 201, artifact.text)
        published = client.post(
            f"/api/admin/releases/{release['id']}/publish",
            headers=admin_headers,
        )
        self.assertEqual(published.status_code, 200, published.text)
        return release["id"]

    def _device_grant(
        self, client, device, license_id, action, resource_id
    ):
        challenge = client.post(
            "/api/device-challenges",
            json={
                "license_id": license_id,
                "device_key_fingerprint": device.fingerprint,
                "action": action,
                "resource_id": resource_id,
            },
        )
        self.assertEqual(challenge.status_code, 201, challenge.text)
        issued = challenge.json()
        verified = client.post(
            f"/api/device-challenges/{issued['challenge_id']}/verify",
            json={
                "challenge_nonce": issued["challenge_nonce"],
                "public_key_base64": device.public_key_base64,
                "signature_base64": device.sign_challenge(
                    issued["challenge_nonce"]
                ),
            },
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        return verified.json()["device_grant"]

    def _submit_payment(
        self, client, device, license_id, release_id
    ):
        grant = self._device_grant(
            client,
            device,
            license_id,
            "submit_payment",
            release_id,
        )
        response = client.post(
            f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
            headers={"X-Device-Grant": grant},
            data={
                "paid_at": "2026-07-13T03:00:00Z",
                "client_submission_id": f"submission:{release_id}",
            },
            files={"file": ("payment.png", b"private-proof", "image/png")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _approve_payment(self, client, headers, payment_id):
        reviewed = client.post(
            f"/api/admin/payments/{payment_id}/review", headers=headers
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        approved = client.post(
            f"/api/admin/payments/{payment_id}/approve", headers=headers
        )
        self.assertEqual(approved.status_code, 200, approved.text)

    def _version_status(self, client, device, license_id, version):
        grant = self._device_grant(
            client,
            device,
            license_id,
            "fetch_entitlement",
            version,
        )
        response = client.get(
            f"/api/customer/licenses/{license_id}/versions/{version}/status",
            headers={"X-Device-Grant": grant},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _assert_certificate(
        self,
        certificate,
        private_key,
        license_id,
        fingerprint,
        version,
    ):
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        verified = verify_entitlement_certificate(
            certificate,
            trusted_public_keys={ENTITLEMENT_KEY_ID: public_key},
            expected_license_id=license_id,
            expected_device_fingerprint=fingerprint,
            expected_version=version,
        )
        self.assertEqual(verified.status, CERTIFICATE_SUCCESS)


if __name__ == "__main__":
    unittest.main()

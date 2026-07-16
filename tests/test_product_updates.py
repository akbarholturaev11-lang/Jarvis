from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentityManager, verify_device_challenge
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA as ENTITLEMENT_ENVELOPE_SCHEMA,
    SCHEMA_VERSION as ENTITLEMENT_SCHEMA_VERSION,
)
from core.product_api_client import ProductApiClient
from core.product_updates import (
    STATUS_CURRENT,
    STATUS_ENTITLED,
    STATUS_ENTITLEMENT_REQUIRED,
    STATUS_INVALID,
    STATUS_PURCHASE_REQUIRED,
    STATUS_SUCCESS,
    ProductUpdateService,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion
from core.release_manifest import (
    ENVELOPE_SCHEMA as RELEASE_ENVELOPE_SCHEMA,
    MANIFEST_SCHEMA,
    SCHEMA_VERSION as RELEASE_SCHEMA_VERSION,
    ArtifactKind,
)
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


RELEASE_KEY_ID = "release-key-001"
ENTITLEMENT_KEY_ID = "entitlement-key-001"
LICENSE_ID = "lic_update_001"
SOURCE = ProductVersion.parse("1.0.0", 10)
TARGET_VERSION = "1.1.0"
TARGET_BUILD = 11
ARTIFACT = b"verified update package" * 200


def _release_info(version: str = TARGET_VERSION) -> dict[str, object]:
    return {
        "version": version,
        "price_minor": 249_000,
        "currency": "UZS",
        "supported_platforms": ["macos"],
        "features": {"en": "New release tools", "ru": "Новые инструменты релиза"},
        "fixes": {"en": "Safer updates", "ru": "Более безопасные обновления"},
    }


def _payment_instructions() -> dict[str, object]:
    return {
        "status": "configured",
        "method": {"en": "Test method", "ru": "Тестовый способ"},
        "recipient": "TEST-RECIPIENT",
        "instructions": {
            "en": "Use only the configured test destination.",
            "ru": "Используйте только тестовые реквизиты.",
        },
    }


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _release_manifest(
    private_key: Ed25519PrivateKey,
    *,
    version: str = TARGET_VERSION,
    build: int = TARGET_BUILD,
    sources: list[str] | None = None,
    sha256: str | None = None,
    byte_size: int | None = None,
) -> str:
    payload = _canonical(
        {
            "schema": MANIFEST_SCHEMA,
            "schema_version": RELEASE_SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "version": version,
            "build": build,
            "platform": "macos",
            "architecture": "arm64",
            "artifact_kind": ArtifactKind.UPDATE_PACKAGE.value,
            "sha256": sha256 or hashlib.sha256(ARTIFACT).hexdigest(),
            "byte_size": len(ARTIFACT) if byte_size is None else byte_size,
            "storage_key": f"releases/{version}/macos/arm64/Jarvis.update",
            "signing_key_id": RELEASE_KEY_ID,
            "compatible_source_versions": sources or ["1.0.0"],
        }
    )
    return json.dumps(
        {
            "schema": RELEASE_ENVELOPE_SCHEMA,
            "schema_version": RELEASE_SCHEMA_VERSION,
            "payload": _b64url(payload),
            "signature": _b64url(private_key.sign(payload)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _entitlement(
    private_key: Ed25519PrivateKey,
    version: str,
    fingerprint: str,
) -> str:
    payload = _canonical(
        {
            "schema": CERTIFICATE_SCHEMA,
            "schema_version": ENTITLEMENT_SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "license_id": LICENSE_ID,
            "device_key_fingerprint": fingerprint,
            "version": version,
            "issued_at": "2026-07-13T03:00:00Z",
            "key_id": ENTITLEMENT_KEY_ID,
        }
    )
    return json.dumps(
        {
            "schema": ENTITLEMENT_ENVELOPE_SCHEMA,
            "schema_version": ENTITLEMENT_SCHEMA_VERSION,
            "payload": _b64url(payload),
            "signature": _b64url(private_key.sign(payload)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class FakeResponse:
    def __init__(
        self,
        raw: bytes,
        url: str,
        *,
        content_type: str | None = None,
    ) -> None:
        self.status = 200
        self.headers = {"Content-Length": str(len(raw))}
        if content_type:
            self.headers["Content-Type"] = content_type
        self.raw = raw
        self.offset = 0
        self.url = url

    @classmethod
    def json(cls, document: dict[str, object]):
        return cls(
            _canonical(document),
            "https://api.example.test/v1/client/updates/check",
            content_type="application/json",
        )

    @classmethod
    def artifact(cls, raw: bytes):
        return cls(raw, "https://api.example.test/v1/client/updates/download")

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self.raw) - self.offset
        chunk = self.raw[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass

    def geturl(self):
        return self.url


class FakeTransport:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def open(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


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


class ProductUpdateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.release_key = Ed25519PrivateKey.generate()
        self.entitlement_key = Ed25519PrivateKey.generate()
        self.cache = SignedEntitlementCache(
            root / "entitlements",
            trusted_public_keys={
                ENTITLEMENT_KEY_ID: self.entitlement_key.public_key()
            },
        )
        self.staging = root / "staging"
        self.identity_manager = DeviceIdentityManager(
            MemorySecureStore(),
            creation_lock_path=str(root / "identity.lock"),
        )
        identity = self.identity_manager.get_or_create()
        self.assertTrue(identity.ok)
        self.fingerprint = identity.identity.fingerprint

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self, responses) -> tuple[ProductUpdateService, FakeTransport]:
        transport = FakeTransport(responses)
        api = ProductApiClient(
            "https://api.example.test",
            transport=transport,
        )
        service = ProductUpdateService(
            api,
            self.cache,
            self.identity_manager,
            self.staging,
            trusted_release_public_keys={
                RELEASE_KEY_ID: self.release_key.public_key()
            },
        )
        return service, transport

    def _proof_responses(
        self,
        manifest: str,
        *,
        entitled: bool,
        certificate_key: Ed25519PrivateKey | None = None,
    ) -> list[FakeResponse]:
        artifact_id = "art_update_001"
        available = {
            "state": "purchase_required",
            "manifest": manifest,
            "artifact_id": artifact_id,
            "release_id": "rel_update_001",
            "release_info": _release_info(),
        }
        challenge = {
            "challenge_id": "chl_update_001",
            "challenge_nonce": _b64url(b"n" * 32),
            "action": "authorize_install",
            "resource_id": artifact_id,
            "issued_at": "2026-07-13T03:00:00Z",
            "expires_at": "2026-07-13T03:02:00Z",
        }
        grant = {
            "device_grant": "test-device-grant-001",
            "action": "authorize_install",
            "resource_id": artifact_id,
            "expires_at": "2026-07-13T03:02:00Z",
        }
        if entitled:
            authorized = {
                "state": "entitled",
                "manifest": manifest,
                "artifact_id": artifact_id,
                "release_id": "rel_update_001",
                "release_info": _release_info(),
                "download_path": "/v1/client/updates/download",
                "download_grant": "artifact-download-grant-test-001",
                "entitlement_certificate": _entitlement(
                    certificate_key or self.entitlement_key,
                    TARGET_VERSION,
                    self.fingerprint,
                ),
            }
        else:
            authorized = {
                **available,
                "payment_instructions": _payment_instructions(),
            }
        return [
            FakeResponse.json(available),
            FakeResponse.json(challenge),
            FakeResponse.json(grant),
            FakeResponse.json(authorized),
        ]

    def _check(self, service: ProductUpdateService):
        return service.check(
            license_id=LICENSE_ID,
            device_fingerprint=self.fingerprint,
            installed=SOURCE,
            platform="macos",
            architecture="arm64",
        )

    def test_current_and_purchase_required_never_grant_download(self):
        current_service, _ = self._service([FakeResponse.json({"state": "current"})])
        current = self._check(current_service)
        self.assertEqual(current.status, STATUS_CURRENT)

        manifest = _release_manifest(self.release_key)
        purchase_service, transport = self._service(
            self._proof_responses(manifest, entitled=False)
        )
        purchase = self._check(purchase_service)
        denied = purchase_service.download(
            purchase.candidate,
            license_id=LICENSE_ID,
            device_fingerprint=self.fingerprint,
            platform="macos",
            architecture="arm64",
        )

        self.assertEqual(purchase.status, STATUS_PURCHASE_REQUIRED)
        self.assertEqual(purchase.candidate.release_info.price_minor, 249_000)
        self.assertTrue(purchase.candidate.payment_instructions.configured)
        self.assertNotIn("TEST-RECIPIENT", repr(purchase.candidate))
        self.assertEqual(denied.status, STATUS_ENTITLEMENT_REQUIRED)
        grant_header = transport.requests[3]["headers"]["X-Device-Grant"]
        self.assertEqual(grant_header, "test-device-grant-001")
        challenge_body = json.loads(transport.requests[1]["body"])
        self.assertEqual(challenge_body["resource_id"], "art_update_001")
        self.assertEqual(challenge_body["action"], "authorize_install")

    def test_maximum_payment_instruction_length_matches_backend_contract(self):
        manifest = _release_manifest(self.release_key)
        responses = self._proof_responses(manifest, entitled=False)
        authorized = json.loads(responses[-1].raw.decode("utf-8"))
        authorized["payment_instructions"]["instructions"] = {
            "en": "i" * 2000,
            "ru": "и" * 2000,
        }
        responses[-1] = FakeResponse.json(authorized)
        service, _ = self._service(responses)

        checked = self._check(service)

        self.assertEqual(checked.status, STATUS_PURCHASE_REQUIRED)
        self.assertEqual(
            len(checked.candidate.payment_instructions.instructions_en),
            2000,
        )

    def test_entitled_update_is_reverified_downloaded_and_privately_staged(self):
        manifest = _release_manifest(self.release_key)
        service, transport = self._service(
            self._proof_responses(manifest, entitled=True)
            + [FakeResponse.artifact(ARTIFACT)]
        )

        checked = self._check(service)
        downloaded = service.download(
            checked.candidate,
            license_id=LICENSE_ID,
            device_fingerprint=self.fingerprint,
            platform="macos",
            architecture="arm64",
        )

        self.assertEqual(checked.status, STATUS_ENTITLED)
        self.assertEqual(downloaded.status, STATUS_SUCCESS)
        proof_body = json.loads(transport.requests[2]["body"])
        proof = verify_device_challenge(
            public_key_base64=proof_body["public_key_base64"],
            device_key_fingerprint=self.fingerprint,
            challenge_nonce=proof_body["challenge_nonce"],
            signature_base64=proof_body["signature_base64"],
        )
        self.assertTrue(proof.ok)
        self.assertEqual(downloaded.staged.path.read_bytes(), ARTIFACT)
        self.assertEqual(downloaded.staged.source, SOURCE)
        self.assertEqual(
            downloaded.staged.target,
            ProductVersion.parse(TARGET_VERSION, TARGET_BUILD),
        )
        if os.name != "nt":
            self.assertEqual(self.staging.stat().st_mode & 0o777, 0o700)
            self.assertEqual(downloaded.staged.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.staging), repr(downloaded))
        self.assertFalse(any(path.suffix == ".part" for path in self.staging.iterdir()))
        self.assertEqual(
            transport.requests[4]["headers"]["X-Artifact-Grant"],
            "artifact-download-grant-test-001",
        )
        self.assertNotIn(
            "artifact-download-grant-test-001",
            transport.requests[4]["url"],
        )

    def test_signed_rollback_incompatible_source_and_tamper_are_rejected(self):
        valid = _release_manifest(self.release_key)
        envelope = json.loads(valid)
        signature = envelope["signature"]
        envelope["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        invalid_manifests = (
            _release_manifest(
                self.release_key,
                version="0.9.0",
                build=11,
                sources=["0.8.0"],
            ),
            _release_manifest(
                self.release_key,
                version="2.0.0",
                build=20,
                sources=["0.9.0"],
            ),
            _release_manifest(
                self.release_key,
                version="1.0.0",
                build=10,
                sources=["0.9.0"],
            ),
            tampered,
        )
        for manifest in invalid_manifests:
            with self.subTest(manifest=manifest[:20]):
                service, _ = self._service(
                    [
                        FakeResponse.json(
                            {
                                "state": "purchase_required",
                                "manifest": manifest,
                                "artifact_id": "art_update_001",
                                "release_id": "rel_update_001",
                                "release_info": _release_info(),
                            }
                        )
                    ]
                )
                checked = self._check(service)
                self.assertEqual(checked.status, STATUS_INVALID)

    def test_wrong_digest_or_incomplete_download_is_deleted(self):
        manifest = _release_manifest(self.release_key)
        for artifact in (b"x" * len(ARTIFACT), ARTIFACT[:-1]):
            with self.subTest(size=len(artifact)):
                service, _ = self._service(
                    self._proof_responses(manifest, entitled=True)
                    + [FakeResponse.artifact(artifact)]
                )
                checked = self._check(service)
                downloaded = service.download(
                    checked.candidate,
                    license_id=LICENSE_ID,
                    device_fingerprint=self.fingerprint,
                    platform="macos",
                    architecture="arm64",
                )
                self.assertEqual(downloaded.status, STATUS_INVALID)
                self.assertFalse(
                    any(path.name.startswith("verified-") for path in self.staging.iterdir())
                )

    def test_entitled_server_claim_with_wrong_certificate_is_not_authority(self):
        wrong_key = Ed25519PrivateKey.generate()
        manifest = _release_manifest(self.release_key)
        service, _ = self._service(
            self._proof_responses(
                manifest,
                entitled=True,
                certificate_key=wrong_key,
            )
        )

        checked = self._check(service)

        self.assertEqual(checked.status, STATUS_ENTITLEMENT_REQUIRED)
        self.assertIsNone(checked.candidate)

    def test_entitled_response_requires_a_bounded_header_grant(self):
        manifest = _release_manifest(self.release_key)
        for grant in (None, "short", "bad grant with spaces", "x" * 129):
            with self.subTest(grant=grant):
                responses = self._proof_responses(manifest, entitled=True)
                authorized = json.loads(responses[-1].raw.decode("utf-8"))
                if grant is None:
                    authorized.pop("download_grant")
                else:
                    authorized["download_grant"] = grant
                responses[-1] = FakeResponse.json(authorized)
                service, _ = self._service(responses)
                checked = self._check(service)
                self.assertEqual(checked.status, STATUS_INVALID)
                self.assertIsNone(checked.candidate)


if __name__ == "__main__":
    unittest.main()

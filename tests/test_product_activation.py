from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentityManager, verify_device_challenge
from core.entitlement_cache import STATUS_SUCCESS as CACHE_SUCCESS
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA,
    SCHEMA_VERSION,
)
from core.product_activation import (
    STATUS_DEVICE_MISMATCH,
    STATUS_INVALID,
    STATUS_OFFLINE,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    ProductActivationService,
)
from core.product_api_client import ProductApiClient
from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


KEY_ID = "entitlement-key-001"
LICENSE_ID = "lic_activation_001"
LICENSE_KEY = "JARVIS-TEST-LICENSE-PRIVATE"
VERSION = "1.0.0"


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _certificate(private_key, fingerprint: str) -> str:
    payload = _canonical(
        {
            "schema": CERTIFICATE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "license_id": LICENSE_ID,
            "device_key_fingerprint": fingerprint,
            "version": VERSION,
            "issued_at": "2026-07-13T03:00:00Z",
            "key_id": KEY_ID,
        }
    )
    return json.dumps(
        {
            "schema": ENVELOPE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "payload": _b64url(payload),
            "signature": _b64url(private_key.sign(payload)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class MemorySecureStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service, account):
        key = (service, account)
        if key not in self.values:
            return SecureStoreResult(STATUS_NOT_FOUND, message="not found")
        return SecureStoreResult(
            STORE_SUCCESS,
            value=self.values[key],
            message="loaded",
        )

    def _set(self, service, account, secret):
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS, message="stored")

    def _delete(self, service, account):
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS, message="deleted")


class FakeResponse:
    def __init__(
        self,
        document: dict[str, object],
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = {
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.raw = _canonical(document)
        self.offset = 0
        self.url = url

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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ProductActivationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.identity_manager = DeviceIdentityManager(
            MemorySecureStore(),
            creation_lock_path=str(root / "identity.lock"),
        )
        identity_result = self.identity_manager.get_or_create()
        self.assertTrue(identity_result.ok)
        self.identity = identity_result.identity
        self.signing_key = Ed25519PrivateKey.generate()
        self.cache = SignedEntitlementCache(
            root / "entitlements",
            trusted_public_keys={KEY_ID: self.signing_key.public_key()},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self, transport: FakeTransport) -> ProductActivationService:
        api = ProductApiClient(
            "https://api.example.test",
            transport=transport,
        )
        return ProductActivationService(
            api,
            self.identity_manager,
            self.cache,
        )

    def test_real_device_proof_and_signed_certificate_complete_activation(self):
        nonce = _b64url(b"n" * 32)
        transport = FakeTransport(
            [
                FakeResponse(
                    {
                        "challenge_id": "chl_activation_001",
                        "challenge_nonce": nonce,
                    },
                    "https://api.example.test/v1/client/activation/challenge",
                ),
                FakeResponse(
                    {
                        "license_id": LICENSE_ID,
                        "entitlement_certificate": _certificate(
                            self.signing_key,
                            self.identity.fingerprint,
                        ),
                    },
                    "https://api.example.test/v1/client/activation/complete",
                ),
            ]
        )
        service = self._service(transport)

        result = service.activate(
            LICENSE_KEY,
            version=VERSION,
            platform="macos",
            architecture="arm64",
        )

        self.assertEqual(result.status, STATUS_SUCCESS)
        challenge_request = json.loads(transport.requests[0]["body"])
        proof_request = json.loads(transport.requests[1]["body"])
        proof = verify_device_challenge(
            public_key_base64=proof_request["device_public_key"],
            device_key_fingerprint=proof_request["device_key_fingerprint"],
            challenge_nonce=proof_request["challenge_nonce"],
            signature_base64=proof_request["challenge_signature"],
        )
        self.assertTrue(proof.ok)
        self.assertEqual(challenge_request["license_key"], LICENSE_KEY)
        self.assertNotIn("license_key", proof_request)
        self.assertNotIn(LICENSE_KEY, repr(service))
        self.assertNotIn(LICENSE_KEY, repr(result))
        cached = self.cache.load_verified(
            license_id=LICENSE_ID,
            device_fingerprint=self.identity.fingerprint,
            version=VERSION,
        )
        self.assertEqual(cached.status, CACHE_SUCCESS)

    def test_invalid_challenge_or_untrusted_certificate_never_succeeds(self):
        cases = (
            [
                FakeResponse(
                    {"challenge_id": "chl_activation_001", "challenge_nonce": "bad"},
                    "https://api.example.test/v1/client/activation/challenge",
                )
            ],
            [
                FakeResponse(
                    {
                        "challenge_id": "chl_activation_001",
                        "challenge_nonce": _b64url(b"n" * 32),
                    },
                    "https://api.example.test/v1/client/activation/challenge",
                ),
                FakeResponse(
                    {
                        "license_id": LICENSE_ID,
                        "entitlement_certificate": _certificate(
                            Ed25519PrivateKey.generate(),
                            self.identity.fingerprint,
                        ),
                    },
                    "https://api.example.test/v1/client/activation/complete",
                ),
            ],
        )
        for responses in cases:
            with self.subTest(count=len(responses)):
                result = self._service(FakeTransport(responses)).activate(
                    LICENSE_KEY,
                    version=VERSION,
                    platform="macos",
                    architecture="arm64",
                )
                self.assertEqual(result.status, STATUS_INVALID)

    def test_network_error_is_reported_as_offline_without_exception_details(self):
        transport = FakeTransport([URLError("private-host-and-token")])
        result = self._service(transport).activate(
            LICENSE_KEY,
            version=VERSION,
            platform="macos",
            architecture="arm64",
        )

        self.assertEqual(result.status, STATUS_OFFLINE)
        self.assertNotIn("private-host", result.message)
        self.assertNotIn(LICENSE_KEY, repr(result))

    def test_http_conflict_is_a_distinct_device_mismatch(self):
        response = FakeResponse(
            {"detail": "sanitized conflict"},
            "https://api.example.test/v1/client/activation/challenge",
            status=409,
            headers={"X-Jarvis-Error-Code": "device_mismatch"},
        )
        result = self._service(FakeTransport([response])).activate(
            LICENSE_KEY,
            version=VERSION,
            platform="macos",
            architecture="arm64",
        )
        self.assertEqual(result.status, STATUS_DEVICE_MISMATCH)
        self.assertNotIn(LICENSE_KEY, repr(result))

    def test_generic_http_conflict_is_rejected_without_device_claim(self):
        response = FakeResponse(
            {"detail": "sanitized conflict"},
            "https://api.example.test/v1/client/activation/challenge",
            status=409,
        )
        result = self._service(FakeTransport([response])).activate(
            LICENSE_KEY,
            version=VERSION,
            platform="macos",
            architecture="arm64",
        )
        self.assertEqual(result.status, STATUS_REJECTED)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentityManager, verify_device_challenge
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA,
    SCHEMA_VERSION,
)
from core.product_api_client import ProductApiClient
from core.product_purchase import (
    STATUS_ENTITLED,
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_SUBMITTED,
    ProductPurchaseService,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


LICENSE_ID = "lic_purchase_001"
RELEASE_ID = "rel_purchase_001"
VERSION = "1.1.0"
KEY_ID = "entitlement-key-001"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class MemoryStore(SecureStore):
    def __init__(self):
        self.values = {}

    def _get(self, service, account):
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND)
        return SecureStoreResult(STATUS_SUCCESS, value=value)

    def _set(self, service, account, secret):
        self.values[(service, account)] = secret
        return SecureStoreResult(STATUS_SUCCESS)

    def _delete(self, service, account):
        self.values.pop((service, account), None)
        return SecureStoreResult(STATUS_SUCCESS)


class Response:
    def __init__(self, document, url):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.raw = _canonical(document)
        self.offset = 0
        self.url = url

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self.raw) - self.offset
        value = self.raw[self.offset : self.offset + amount]
        self.offset += len(value)
        return value

    def close(self):
        pass

    def geturl(self):
        return self.url


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class ProductPurchaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.manager = DeviceIdentityManager(
            MemoryStore(), creation_lock_path=str(root / "identity.lock")
        )
        self.identity = self.manager.get_or_create().identity
        self.key = Ed25519PrivateKey.generate()
        self.cache = SignedEntitlementCache(
            root / "entitlements",
            trusted_public_keys={KEY_ID: self.key.public_key()},
        )

    def tearDown(self):
        self.temp.cleanup()

    def _challenge(self, action, resource):
        return [
            Response(
                {
                    "challenge_id": "chl_purchase_001",
                    "challenge_nonce": _b64(b"n" * 32),
                    "action": action,
                    "resource_id": resource,
                    "issued_at": "2026-07-13T03:00:00Z",
                    "expires_at": "2026-07-13T03:02:00Z",
                },
                "https://api.example.test/api/device-challenges",
            ),
            Response(
                {
                    "device_grant": "device-grant-purchase-001",
                    "action": action,
                    "resource_id": resource,
                    "expires_at": "2026-07-13T03:02:00Z",
                },
                "https://api.example.test/api/device-challenges/chl_purchase_001/verify",
            ),
        ]

    def _service(self, responses):
        transport = Transport(responses)
        return (
            ProductPurchaseService(
                ProductApiClient(
                    "https://api.example.test", transport=transport
                ),
                self.manager,
                self.cache,
            ),
            transport,
        )

    def _certificate(self, key=None):
        signer = self.key if key is None else key
        payload = _canonical(
            {
                "schema": CERTIFICATE_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "product_id": PRODUCT_ID,
                "bundle_id": BUNDLE_ID,
                "license_id": LICENSE_ID,
                "device_key_fingerprint": self.identity.fingerprint,
                "version": VERSION,
                "issued_at": "2026-07-13T03:00:00Z",
                "key_id": KEY_ID,
            }
        )
        return json.dumps(
            {
                "schema": ENVELOPE_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "payload": _b64(payload),
                "signature": _b64(signer.sign(payload)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _status(self, *, entitled=False, certificate=None, state="pending"):
        return {
            "version": VERSION,
            "release_id": RELEASE_ID,
            "release_state": "published",
            "price_minor": 125000,
            "currency": "UZS",
            "entitled": entitled,
            "entitlement_granted_at": "2026-07-13T03:00:00Z" if entitled else None,
            "payment_id": "pay_purchase_001",
            "payment_state": "approved" if entitled else state,
            "rejection_reason": None,
            "active_device_bound": True,
            "entitlement_certificate": certificate,
        }

    def test_payment_evidence_uses_device_grant_and_bounded_multipart(self):
        screenshot = b"private-png-evidence"
        payment = {
            "id": "pay_purchase_001",
            "release_id": RELEASE_ID,
            "version": None,
            "amount_minor": 125000,
            "currency": "UZS",
            "paid_at": "2026-07-13T02:50:00Z",
            "submitted_at": "2026-07-13T03:00:00Z",
            "state": "pending",
            "rejection_reason": None,
        }
        responses = self._challenge("submit_payment", RELEASE_ID) + [
            Response(
                payment,
                f"https://api.example.test/api/customer/licenses/{LICENSE_ID}/releases/{RELEASE_ID}/payments",
            )
        ]
        service, transport = self._service(responses)

        result = service.submit_payment(
            license_id=LICENSE_ID,
            release_id=RELEASE_ID,
            paid_at="2026-07-13T02:50:00Z",
            screenshot=screenshot,
            content_type="image/png",
        )

        self.assertEqual(result.status, STATUS_SUBMITTED)
        proof_body = json.loads(transport.requests[1]["body"])
        self.assertTrue(
            verify_device_challenge(
                public_key_base64=proof_body["public_key_base64"],
                device_key_fingerprint=self.identity.fingerprint,
                challenge_nonce=proof_body["challenge_nonce"],
                signature_base64=proof_body["signature_base64"],
            ).ok
        )
        self.assertIn(screenshot, transport.requests[2]["body"])
        self.assertEqual(
            transport.requests[2]["headers"]["X-Device-Grant"],
            "device-grant-purchase-001",
        )
        self.assertNotIn(screenshot.decode(), repr(result))

    def test_poll_pending_has_no_authority_but_signed_approved_status_does(self):
        pending_service, _ = self._service(
            self._challenge("fetch_entitlement", VERSION)
            + [
                Response(
                    self._status(),
                    f"https://api.example.test/api/customer/licenses/{LICENSE_ID}/versions/{VERSION}/status",
                )
            ]
        )
        pending = pending_service.poll_status(license_id=LICENSE_ID, version=VERSION)
        self.assertEqual(pending.status, STATUS_PENDING)
        self.assertFalse(pending.entitled)

        entitled_service, _ = self._service(
            self._challenge("fetch_entitlement", VERSION)
            + [
                Response(
                    self._status(entitled=True, certificate=self._certificate()),
                    f"https://api.example.test/api/customer/licenses/{LICENSE_ID}/versions/{VERSION}/status",
                )
            ]
        )
        entitled = entitled_service.poll_status(
            license_id=LICENSE_ID, version=VERSION
        )
        self.assertEqual(entitled.status, STATUS_ENTITLED)
        self.assertTrue(entitled.entitled)
        self.assertTrue(
            self.cache.load_verified(
                license_id=LICENSE_ID,
                device_fingerprint=self.identity.fingerprint,
                version=VERSION,
            ).ok
        )

    def test_entitled_metadata_with_untrusted_certificate_is_invalid(self):
        service, _ = self._service(
            self._challenge("fetch_entitlement", VERSION)
            + [
                Response(
                    self._status(
                        entitled=True,
                        certificate=self._certificate(Ed25519PrivateKey.generate()),
                    ),
                    f"https://api.example.test/api/customer/licenses/{LICENSE_ID}/versions/{VERSION}/status",
                )
            ]
        )

        result = service.poll_status(license_id=LICENSE_ID, version=VERSION)

        self.assertEqual(result.status, STATUS_INVALID)
        self.assertFalse(result.entitled)


if __name__ == "__main__":
    unittest.main()

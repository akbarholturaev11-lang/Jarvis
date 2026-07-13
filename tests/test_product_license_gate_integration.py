from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.app_paths import resolve_app_paths
from core.entitlement_certificate import CERTIFICATE_SCHEMA, ENVELOPE_SCHEMA, SCHEMA_VERSION
from core.product_activation import ProductActivationService
from core.product_api_client import ProductApiClient
from core.product_config import ProductClientConfig, ProductConfigResult, STATUS_SUCCESS
from core.product_gate import STATUS_ACTIVATION_REQUIRED, ProductLicenseGate
from core.product_runtime import ProductRuntimeService
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion
from core.runtime_product import RuntimeProductIdentity
from core.secure_store import (
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


KEY_ID = "entitlement-key-gate-001"
LICENSE_ID = "license_gate_integration_001"
LICENSE_KEY = "test-license-key-not-real"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _certificate(private_key, fingerprint: str, version: str) -> str:
    payload = _canonical(
        {
            "schema": CERTIFICATE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "license_id": LICENSE_ID,
            "device_key_fingerprint": fingerprint,
            "version": version,
            "issued_at": "2026-07-14T00:00:00Z",
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


class MemoryStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service, account):
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND)
        return SecureStoreResult(STORE_SUCCESS, value=value)

    def _set(self, service, account, secret):
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS)

    def _delete(self, service, account):
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS)


class FakeResponse:
    def __init__(self, document: dict[str, object], url: str) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.url = url
        self.raw = _canonical(document)
        self.offset = 0

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


class ProductLicenseGateIntegrationTests(unittest.TestCase):
    def test_activation_restart_offline_and_new_paid_version_boundary(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        config = ProductConfigResult(
            STATUS_SUCCESS,
            ProductClientConfig(
                "https://product.example.test",
                False,
                {KEY_ID: public_key},
                {"release-key-gate-001": b"r" * 32},
            ),
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = resolve_app_paths(
                platform_name="macos",
                home=root,
                environ={},
                resource_root=root / "resources",
            )
            old_identity = RuntimeProductIdentity(
                ProductVersion.parse("1.0.0", 1), "macos", "arm64", True
            )
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=old_identity,
            )
            gate = ProductLicenseGate(service)
            self.assertEqual(gate.evaluate().status, STATUS_ACTIVATION_REQUIRED)
            fingerprint = service.device_fingerprint()
            nonce = _b64url(b"n" * 32)
            transport = FakeTransport(
                [
                    FakeResponse(
                        {
                            "challenge_id": "challenge_gate_001",
                            "challenge_nonce": nonce,
                        },
                        "https://product.example.test/v1/client/activation/challenge",
                    ),
                    FakeResponse(
                        {
                            "license_id": LICENSE_ID,
                            "entitlement_certificate": _certificate(
                                signing_key,
                                fingerprint,
                                "1.0.0",
                            ),
                        },
                        "https://product.example.test/v1/client/activation/complete",
                    ),
                ]
            )
            service._activation = ProductActivationService(
                ProductApiClient(
                    "https://product.example.test",
                    transport=transport,
                ),
                service._identity,
                service._cache,
            )
            activated = gate.activate(LICENSE_KEY)
            self.assertTrue(activated.allowed)
            self.assertNotIn(LICENSE_KEY, repr(gate))
            first_body = json.loads(transport.requests[0]["body"])
            second_body = json.loads(transport.requests[1]["body"])
            self.assertEqual(first_body["license_key"], LICENSE_KEY)
            self.assertNotIn("license_key", second_body)

            restarted_offline = ProductLicenseGate(
                ProductRuntimeService(
                    app_paths=paths,
                    secure_store=store,
                    config_result=config,
                    runtime_identity=old_identity,
                )
            ).evaluate()
            self.assertTrue(restarted_offline.allowed)

            new_paid_version = ProductLicenseGate(
                ProductRuntimeService(
                    app_paths=paths,
                    secure_store=store,
                    config_result=config,
                    runtime_identity=RuntimeProductIdentity(
                        ProductVersion.parse("1.1.0", 2),
                        "macos",
                        "arm64",
                        True,
                    ),
                )
            ).evaluate()
            self.assertFalse(new_paid_version.allowed)
            self.assertTrue(
                ProductLicenseGate(
                    ProductRuntimeService(
                        app_paths=paths,
                        secure_store=store,
                        config_result=config,
                        runtime_identity=old_identity,
                    )
                ).evaluate().allowed
            )


if __name__ == "__main__":
    unittest.main()

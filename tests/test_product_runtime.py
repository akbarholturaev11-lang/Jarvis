from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import core.product_runtime as product_runtime_module
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.app_paths import resolve_app_paths
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import CERTIFICATE_SCHEMA, ENVELOPE_SCHEMA, SCHEMA_VERSION
from core.product_config import (
    STATUS_NOT_CONFIGURED as CONFIG_NOT_CONFIGURED,
    STATUS_SUCCESS as CONFIG_SUCCESS,
    ProductClientConfig,
    ProductConfigResult,
)
from core.product_runtime import (
    LICENSE_ID_ACCOUNT,
    LICENSE_STATE_SERVICE,
    STATUS_ENTITLED,
    STATUS_INVALID,
    STATUS_NOT_ACTIVATED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_CONFIGURED,
    ProductRuntimeService,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion
from core.runtime_product import RuntimeProductIdentity
from core.secure_store import (
    STATUS_NOT_AVAILABLE as STORE_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


KEY_ID = "entitlement-key-runtime-001"
LICENSE_ID = "license_runtime_001"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _certificate(
    private_key: Ed25519PrivateKey,
    *,
    fingerprint: str,
    version: str,
) -> str:
    payload = json.dumps(
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
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
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


class MissingStore(SecureStore):
    def _get(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)


class MemoryStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service: str, account: str) -> SecureStoreResult:
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND)
        return SecureStoreResult(STORE_SUCCESS, value=value)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS)


class ReadbackUnavailableStore(MemoryStore):
    def _get(self, service: str, account: str) -> SecureStoreResult:
        if (service, account) == (LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT):
            return SecureStoreResult(STORE_NOT_AVAILABLE)
        return super()._get(service, account)


class ProductRuntimeTests(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_app_paths(
            platform_name="macos",
            home=root,
            environ={},
            resource_root=root / "resources",
        )

    def _runtime(
        self,
        version: str = "0.3.1",
        *,
        packaged: bool = False,
    ) -> RuntimeProductIdentity:
        return RuntimeProductIdentity(
            ProductVersion.parse(version, 1), "macos", "arm64", packaged
        )

    def _config(self, public_key: bytes) -> ProductConfigResult:
        return ProductConfigResult(
            CONFIG_SUCCESS,
            ProductClientConfig(
                "https://product.example.com",
                False,
                {KEY_ID: public_key},
                {"release-key-runtime-001": b"r" * 32},
            ),
        )

    def test_missing_product_configuration_is_honest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_NOT_CONFIGURED),
                runtime_identity=self._runtime(),
            )
            state = service.local_state()
        self.assertEqual(state.status, STATUS_NOT_CONFIGURED)
        self.assertIn("<redacted>", repr(service))

    def test_configured_service_without_license_is_not_activated(self) -> None:
        config = ProductClientConfig(
            "https://product.example.com",
            False,
            {"ent-key-001": b"e" * 32},
            {"rel-key-001": b"r" * 32},
        )
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_SUCCESS, config),
                runtime_identity=self._runtime(),
            )
            state = service.local_state()
        self.assertEqual(state.status, STATUS_NOT_ACTIVATED)
        self.assertIsNone(service.check_updates())

    def test_invalid_frozen_build_identity_remains_packaged_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            product_runtime_module.sys,
            "frozen",
            True,
            create=True,
        ), mock.patch(
            "core.product_runtime.load_runtime_product_identity",
            side_effect=RuntimeError("invalid metadata"),
        ):
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_NOT_CONFIGURED),
            )
            state = service.local_state()
        self.assertTrue(service.packaged_runtime_expected)
        self.assertEqual(state.status, "invalid")

    def test_exact_version_entitlement_survives_offline_restart_but_not_paid_upgrade(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            config = self._config(public_key)
            old_runtime = self._runtime("1.0.0", packaged=True)
            first = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=old_runtime,
            )
            fingerprint = first.device_fingerprint()
            self.assertIsNotNone(fingerprint)
            cache = SignedEntitlementCache(
                paths.data_dir / "entitlements",
                trusted_public_keys={KEY_ID: public_key},
            )
            stored = cache.store_verified(
                _certificate(
                    signing_key,
                    fingerprint=fingerprint,
                    version="1.0.0",
                ),
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            self.assertTrue(stored.ok)
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)

            self.assertEqual(first.local_state().status, STATUS_ENTITLED)
            restarted_offline = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=old_runtime,
            )
            self.assertEqual(
                restarted_offline.local_state().status,
                STATUS_ENTITLED,
            )
            paid_new_release = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=self._runtime("1.1.0", packaged=True),
            )
            self.assertEqual(
                paid_new_release.local_state().status,
                STATUS_NOT_ACTIVATED,
            )
            self.assertEqual(first.local_state().status, STATUS_ENTITLED)

            certificate_path = cache.certificate_path(
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            certificate_path.write_text("corrupted", encoding="utf-8")
            self.assertEqual(first.local_state().status, STATUS_INVALID)

    def test_cache_dependency_failures_are_not_mislabeled_as_invalid(self) -> None:
        store = MemoryStore()
        public_key = b"e" * 32
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=self._runtime("1.0.0", packaged=True),
            )
            self.assertIsNotNone(service.device_fingerprint())
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)
            for cache_status, expected in (
                ("not_available", STATUS_NOT_AVAILABLE),
                ("failed", "failed"),
            ):
                with self.subTest(cache_status=cache_status):
                    service._cache = SimpleNamespace(
                        load_verified=lambda **_: SimpleNamespace(
                            status=cache_status,
                            ok=False,
                        )
                    )
                    self.assertEqual(service.local_state().status, expected)

    def test_activation_claim_is_not_success_until_local_readback_verifies(self) -> None:
        store = ReadbackUnavailableStore()
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=store,
                config_result=self._config(b"e" * 32),
                runtime_identity=self._runtime("1.0.0", packaged=True),
            )
            service._activation = SimpleNamespace(
                activate=lambda *args, **kwargs: SimpleNamespace(
                    ok=True,
                    license_id=LICENSE_ID,
                )
            )
            outcome = service.activate("test-activation-key")
        self.assertEqual(outcome.status, STATUS_NOT_AVAILABLE)
        self.assertFalse(outcome.ok)


if __name__ == "__main__":
    unittest.main()

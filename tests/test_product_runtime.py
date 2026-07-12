from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.product_runtime as product_runtime_module

from core.app_paths import resolve_app_paths
from core.product_config import (
    STATUS_NOT_CONFIGURED as CONFIG_NOT_CONFIGURED,
    STATUS_SUCCESS as CONFIG_SUCCESS,
    ProductClientConfig,
    ProductConfigResult,
)
from core.product_runtime import (
    STATUS_NOT_ACTIVATED,
    STATUS_NOT_CONFIGURED,
    ProductRuntimeService,
)
from core.product_version import ProductVersion
from core.runtime_product import RuntimeProductIdentity
from core.secure_store import STATUS_NOT_FOUND, SecureStore, SecureStoreResult


class MissingStore(SecureStore):
    def _get(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)


class ProductRuntimeTests(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_app_paths(
            platform_name="macos",
            home=root,
            environ={},
            resource_root=root / "resources",
        )

    def _runtime(self) -> RuntimeProductIdentity:
        return RuntimeProductIdentity(
            ProductVersion.parse("0.3.1", 1), "macos", "arm64", False
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


if __name__ == "__main__":
    unittest.main()

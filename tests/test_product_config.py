from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from core.app_paths import resolve_app_paths
from core.product_config import (
    STATUS_INVALID,
    STATUS_NOT_CONFIGURED,
    STATUS_SUCCESS,
    load_product_client_config,
)


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).rstrip(b"=").decode("ascii")


class ProductConfigTests(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_app_paths(
            platform_name="linux",
            home=root,
            environ={},
            resource_root=root / "resources",
        )

    def test_missing_config_is_honestly_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = load_product_client_config(app_paths=self._paths(Path(temp)))
        self.assertEqual(result.status, STATUS_NOT_CONFIGURED)

    def test_strict_https_config_loads_and_repr_redacts_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._paths(root)
            paths.config_dir.mkdir(parents=True)
            (paths.config_dir / "product.json").write_text(
                json.dumps(
                    {
                        "schema": "jarvis.product-client.v1",
                        "api_base_url": "https://product.example.com",
                        "allow_insecure_localhost": False,
                        "entitlement_public_keys": {"ent-key-001": _key(1)},
                        "release_public_keys": {"rel-key-001": _key(2)},
                    }
                ),
                encoding="utf-8",
            )
            result = load_product_client_config(app_paths=paths)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertNotIn("product.example.com", repr(result.config))

    def test_nonlocal_http_placeholder_and_bad_key_fail_closed(self) -> None:
        for url, key in (
            ("http://product.example.com", _key(1)),
            ("https://product.example.com", "A" * 42),
        ):
            with self.subTest(url=url, key_length=len(key)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = self._paths(root)
                paths.config_dir.mkdir(parents=True)
                (paths.config_dir / "product.json").write_text(
                    json.dumps(
                        {
                            "schema": "jarvis.product-client.v1",
                            "api_base_url": url,
                            "allow_insecure_localhost": False,
                            "entitlement_public_keys": {"ent-key-001": key},
                            "release_public_keys": {"rel-key-001": _key(2)},
                        }
                    ),
                    encoding="utf-8",
                )
                result = load_product_client_config(app_paths=paths)
            self.assertEqual(result.status, STATUS_INVALID)

    def test_packaged_runtime_ignores_user_writable_trust_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._paths(root)
            paths.config_dir.mkdir(parents=True)
            bundled = paths.resource_root / "config" / "product.json"
            bundled.parent.mkdir(parents=True)
            safe = {
                "schema": "jarvis.product-client.v1",
                "api_base_url": "https://product.example.com",
                "allow_insecure_localhost": False,
                "entitlement_public_keys": {"ent-key-001": _key(1)},
                "release_public_keys": {"rel-key-001": _key(2)},
            }
            bundled.write_text(json.dumps(safe), encoding="utf-8")
            attacker = dict(safe)
            attacker["api_base_url"] = "https://attacker.example.com"
            (paths.config_dir / "product.json").write_text(
                json.dumps(attacker), encoding="utf-8"
            )
            result = load_product_client_config(
                app_paths=paths,
                packaged=True,
            )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.config.api_base_url, "https://product.example.com")


if __name__ == "__main__":
    unittest.main()

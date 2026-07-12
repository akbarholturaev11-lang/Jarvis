from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.runtime_product import (
    SOURCE_PRODUCT_VERSION,
    load_runtime_product_identity,
)


class RuntimeProductTests(unittest.TestCase):
    def test_source_runtime_uses_declared_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity = load_runtime_product_identity(
                resource_root=Path(temp),
                frozen=False,
                system="Darwin",
                machine="arm64",
            )
        self.assertEqual(identity.product_version, SOURCE_PRODUCT_VERSION)
        self.assertFalse(identity.packaged)

    def test_packaged_runtime_requires_and_validates_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(RuntimeError):
                load_runtime_product_identity(
                    resource_root=root,
                    frozen=True,
                    system="Linux",
                    machine="x86_64",
                )
            (root / "product_build.json").write_text(
                json.dumps(
                    {
                        "product_id": PRODUCT_ID,
                        "bundle_id": BUNDLE_ID,
                        "version": "2.4.0",
                        "build": 19,
                    }
                ),
                encoding="utf-8",
            )
            identity = load_runtime_product_identity(
                resource_root=root,
                frozen=True,
                system="Linux",
                machine="x86_64",
            )
        self.assertEqual(str(identity.product_version.version), "2.4.0")
        self.assertEqual(identity.product_version.build, 19)

    def test_tampered_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "product_build.json").write_text(
                json.dumps(
                    {
                        "product_id": "other",
                        "bundle_id": BUNDLE_ID,
                        "version": "1.0.0",
                        "build": 1,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_runtime_product_identity(
                    resource_root=root,
                    frozen=True,
                    system="Darwin",
                    machine="arm64",
                )


if __name__ == "__main__":
    unittest.main()

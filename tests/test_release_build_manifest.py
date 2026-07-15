from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core.product_version import BUNDLE_ID, PRODUCT_ID, PRODUCT_NAME
from core.release_build_manifest import (
    BUILD_MANIFEST_SCHEMA,
    build_artifact_manifest,
    load_build_manifest_document,
    sha256_file,
    write_build_manifest,
)


class BuildManifestTests(unittest.TestCase):
    def _artifact(self, root: Path, content: bytes = b"jarvis dmg bytes") -> Path:
        path = root / "JARVIS-1.0.0-build1-macos-arm64.dmg"
        path.write_bytes(content)
        return path

    def test_manifest_uses_shared_identity_and_real_hash(self):
        content = b"jarvis dmg bytes"
        with tempfile.TemporaryDirectory() as temp:
            artifact = self._artifact(Path(temp), content)
            manifest = build_artifact_manifest(
                artifact_path=artifact,
                version="1.0.0",
                build=1,
                platform="Darwin",
                architecture="aarch64",
            )
        self.assertEqual(manifest.product_id, PRODUCT_ID)
        self.assertEqual(manifest.product_name, PRODUCT_NAME)
        self.assertEqual(manifest.bundle_id, BUNDLE_ID)
        self.assertEqual(manifest.platform, "macos")
        self.assertEqual(manifest.architecture, "arm64")
        self.assertEqual(manifest.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(manifest.byte_size, len(content))
        self.assertFalse(manifest.signed)
        self.assertFalse(manifest.notarized)

    def test_document_records_no_distribution_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = self._artifact(Path(temp))
            manifest = build_artifact_manifest(
                artifact_path=artifact,
                version="2.3.4",
                build=7,
                platform="macos",
                architecture="arm64",
            )
        document = manifest.to_document()
        self.assertEqual(document["schema"], BUILD_MANIFEST_SCHEMA)
        self.assertIs(document["distribution_ready"], False)
        self.assertEqual(document["version"], "2.3.4")
        self.assertEqual(document["build"], 7)

    def test_roundtrip_and_deterministic_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = self._artifact(root)
            manifest = build_artifact_manifest(
                artifact_path=artifact,
                version="1.0.0",
                build=1,
                platform="macos",
                architecture="arm64",
            )
            destination = root / "out" / "build_manifest.json"
            written = write_build_manifest(manifest, destination)
            self.assertTrue(written.is_file())
            loaded = load_build_manifest_document(written)
        self.assertEqual(loaded["sha256"], manifest.sha256)
        self.assertEqual(manifest.to_json(), manifest.to_json())

    def test_notarized_requires_signed(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = self._artifact(Path(temp))
            with self.assertRaises(ValueError):
                build_artifact_manifest(
                    artifact_path=artifact,
                    version="1.0.0",
                    build=1,
                    platform="macos",
                    architecture="arm64",
                    signed=False,
                    notarized=True,
                )

    def test_missing_or_empty_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                build_artifact_manifest(
                    artifact_path=root / "absent.dmg",
                    version="1.0.0",
                    build=1,
                    platform="macos",
                    architecture="arm64",
                )
            empty = root / "empty.dmg"
            empty.write_bytes(b"")
            with self.assertRaises(ValueError):
                build_artifact_manifest(
                    artifact_path=empty,
                    version="1.0.0",
                    build=1,
                    platform="macos",
                    architecture="arm64",
                )

    def test_sha256_file_streams_correctly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blob"
            payload = b"x" * (3 * 1024 * 1024 + 17)
            path.write_bytes(payload)
            self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()

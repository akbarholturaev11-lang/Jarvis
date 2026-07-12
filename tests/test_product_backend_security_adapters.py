from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.entitlement_certificate import STATUS_SUCCESS, verify_entitlement_certificate
from product_backend.api_artifact_storage import (
    ARTIFACT_STREAM_MAX_READ_BYTES,
    ARTIFACT_VERIFY_CHUNK_BYTES,
    LocalReadOnlyReleaseArtifactStore,
    ReleaseArtifactStorageIntegrityError,
)
from product_backend.api_signing import InjectedEd25519EntitlementSigner


@unittest.skipUnless(os.name == "posix", "secure local artifact adapter is POSIX-only")
class ReleaseArtifactStoreTests(unittest.TestCase):
    def test_exact_private_artifact_is_verified_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "artifacts"
            nested = root / "releases" / "1.0.0"
            nested.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            (root / "releases").chmod(0o700)
            nested.chmod(0o700)
            content = b"verified-release-package"
            artifact = nested / "Jarvis.package"
            artifact.write_bytes(content)
            artifact.chmod(0o600)
            store = LocalReadOnlyReleaseArtifactStore(root)
            stream = store.open_verified_release_artifact(
                storage_key="releases/1.0.0/Jarvis.package",
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_byte_size=len(content),
            )
            loaded = bytearray()
            with stream:
                while True:
                    chunk = stream.read(5)
                    if not chunk:
                        break
                    self.assertLessEqual(len(chunk), 5)
                    loaded.extend(chunk)
            self.assertTrue(stream.closed)
        self.assertEqual(bytes(loaded), content)

    def test_world_writable_intermediate_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "artifacts"
            nested = root / "releases"
            nested.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            nested.chmod(0o777)
            artifact = nested / "Jarvis.package"
            artifact.write_bytes(b"x")
            artifact.chmod(0o600)
            store = LocalReadOnlyReleaseArtifactStore(root)
            with self.assertRaises(ReleaseArtifactStorageIntegrityError):
                store.open_verified_release_artifact(
                    storage_key="releases/Jarvis.package",
                    expected_sha256=hashlib.sha256(b"x").hexdigest(),
                    expected_byte_size=1,
                )

    def test_verified_stream_is_bounded_and_detects_post_verify_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "artifacts"
            root.mkdir(mode=0o700)
            content = b"a" * ((2 * 1024 * 1024) + 17)
            artifact = root / "Jarvis.package"
            artifact.write_bytes(content)
            artifact.chmod(0o600)
            store = LocalReadOnlyReleaseArtifactStore(root)
            requested_sizes: list[int] = []
            real_read = os.read

            def recording_read(descriptor: int, amount: int) -> bytes:
                requested_sizes.append(amount)
                return real_read(descriptor, amount)

            with patch("product_backend.api_artifact_storage.os.read", recording_read):
                stream = store.open_verified_release_artifact(
                    storage_key="Jarvis.package",
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    expected_byte_size=len(content),
                )
                artifact.write_bytes(b"b" * len(content))
                artifact.chmod(0o600)
                with self.assertRaises(ReleaseArtifactStorageIntegrityError):
                    while stream.read(64 * 1024):
                        pass
            self.assertTrue(stream.closed)
            self.assertLessEqual(
                max(requested_sizes),
                max(
                    ARTIFACT_VERIFY_CHUNK_BYTES,
                    ARTIFACT_STREAM_MAX_READ_BYTES,
                ),
            )

    def test_path_replacement_after_verification_cannot_change_streamed_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "artifacts"
            root.mkdir(mode=0o700)
            content = b"trusted-package"
            artifact = root / "Jarvis.package"
            artifact.write_bytes(content)
            artifact.chmod(0o600)
            store = LocalReadOnlyReleaseArtifactStore(root)
            stream = store.open_verified_release_artifact(
                storage_key="Jarvis.package",
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_byte_size=len(content),
            )
            artifact.unlink()
            artifact.write_bytes(b"malicious-bytes")
            artifact.chmod(0o600)
            with self.assertRaises(ReleaseArtifactStorageIntegrityError):
                stream.read(len(content))
            self.assertTrue(stream.closed)


class EntitlementSignerTests(unittest.TestCase):
    def test_real_signer_matches_offline_verifier_and_redacts_key(self) -> None:
        private = Ed25519PrivateKey.generate()
        signer = InjectedEd25519EntitlementSigner(
            private, key_id="entitlement-key-001"
        )
        certificate = signer.sign_entitlement_certificate(
            license_id="lic_0123456789abcdef",
            device_key_fingerprint="sha256:" + "a" * 64,
            version="1.2.3",
            issued_at="2026-07-13T03:00:00Z",
        )
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        verified = verify_entitlement_certificate(
            certificate,
            trusted_public_keys={"entitlement-key-001": public},
            expected_license_id="lic_0123456789abcdef",
            expected_device_fingerprint="sha256:" + "a" * 64,
            expected_version="1.2.3",
        )
        self.assertEqual(verified.status, STATUS_SUCCESS)
        self.assertIn("<redacted>", repr(signer))


if __name__ == "__main__":
    unittest.main()

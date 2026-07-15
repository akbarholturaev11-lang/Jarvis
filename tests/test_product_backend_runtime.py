from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from product_backend.api_auth import AdminPasswordCredential, BackendConfigurationError
from product_backend.runtime import create_app_from_environment


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@unittest.skipUnless(os.name == "posix", "secure local backend runtime is POSIX-only")
class ProductBackendRuntimeTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        data = root / "data"
        artifacts = root / "artifacts"
        data.mkdir(mode=0o700)
        artifacts.mkdir(mode=0o700)
        entitlement = Ed25519PrivateKey.generate()
        entitlement_file = root / "entitlement.key"
        entitlement_file.write_bytes(
            entitlement.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        entitlement_file.chmod(0o600)
        pepper_file = root / "activation.pepper"
        pepper_file.write_bytes(b"p" * 32)
        pepper_file.chmod(0o600)
        mfa_key_file = root / "admin-mfa.key"
        mfa_key_file.write_bytes(b"m" * 32)
        mfa_key_file.chmod(0o600)
        release = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        credential = AdminPasswordCredential.derive_for_configuration(
            subject="admin:runtime",
            password="strong-runtime-password",
            salt=b"s" * 32,
        )
        return {
            "JARVIS_BACKEND_DATA_DIR": str(data),
            "JARVIS_RELEASE_ARTIFACT_ROOT": str(artifacts),
            "JARVIS_RELEASE_PUBLIC_KEYS_JSON": json.dumps(
                {"release-key-001": _b64(release)}
            ),
            "JARVIS_ENTITLEMENT_KEY_ID": "entitlement-key-001",
            "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE": str(entitlement_file),
            "JARVIS_ACTIVATION_PEPPER_FILE": str(pepper_file),
            "JARVIS_ADMIN_MFA_KEY_FILE": str(mfa_key_file),
            "JARVIS_ADMIN_SUBJECT": credential.subject,
            "JARVIS_ADMIN_PASSWORD_SALT_B64URL": _b64(credential.salt),
            "JARVIS_ADMIN_PASSWORD_HASH_B64URL": _b64(
                credential.password_digest
            ),
            "JARVIS_ADMIN_PBKDF2_ITERATIONS": str(credential.iterations),
            "JARVIS_ADMIN_SESSION_SECRET_B64URL": _b64(b"z" * 32),
            "JARVIS_API_ALLOWED_HOSTS": "product.example.com",
        }

    def test_factory_assembles_and_closes_explicit_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            app = create_app_from_environment(self._environment(root))
            paths = {getattr(route, "path", "") for route in app.routes}
            self.assertIn("/admin", paths)
            self.assertIn("/v1/client/activation/challenge", paths)
            credential_path = root / "data" / "admin-credentials.sqlite3"
            self.assertTrue(credential_path.is_file())
            self.assertEqual(stat.S_IMODE(credential_path.stat().st_mode), 0o600)
            app.state.close_backend_resources()
            app.state.close_backend_resources()

    def test_missing_config_and_loose_private_key_permissions_fail_closed(self) -> None:
        with self.assertRaises(BackendConfigurationError):
            create_app_from_environment({})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            environment = self._environment(root)
            Path(environment["JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE"]).chmod(0o644)
            with self.assertRaises(BackendConfigurationError):
                create_app_from_environment(environment)

    def test_invalid_admin_network_allowlist_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            environment = self._environment(root)
            environment["JARVIS_ADMIN_ALLOWED_NETWORKS"] = "not-a-network"
            with self.assertRaises(BackendConfigurationError):
                create_app_from_environment(environment)


if __name__ == "__main__":
    unittest.main()

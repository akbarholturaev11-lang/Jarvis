"""Tests for client product.json provisioning from public trust material.

These cover the bridge between backend secret generation (``ops.gen_secrets``)
and the pinned client config (``core.product_config``): the entitlement public
key emitted for the client must correspond to the backend's entitlement private
key, and ``ops.build_client_config`` must only ever produce a strict, HTTPS,
loader-accepted document.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.app_paths import resolve_app_paths
from core.product_config import STATUS_SUCCESS, load_product_client_config
from ops import build_client_config, gen_secrets
from ops._common import POSIX


def _valid_key(seed: int) -> str:
    return base64.urlsafe_b64encode(bytes([seed]) * 32).rstrip(b"=").decode("ascii")


def _trust(
    *,
    entitlement_id: str = "entitlement-key-001",
    release_id: str = "release-key-001",
) -> dict[str, object]:
    return {
        "schema": build_client_config.TRUST_SCHEMA,
        "entitlement_public_keys": {entitlement_id: _valid_key(1)},
        "release_public_keys": {release_id: _valid_key(2)},
    }


def _loads_ok(document: dict[str, object]) -> bool:
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        candidate = temp_dir / "product.json"
        candidate.write_text(json.dumps(document), encoding="utf-8")
        paths = resolve_app_paths(
            platform_name="linux",
            home=temp_dir,
            environ={},
            resource_root=temp_dir,
        )
        result = load_product_client_config(
            app_paths=paths, source_config=candidate, packaged=True
        )
    return result.status == STATUS_SUCCESS and result.config is not None


class BuildClientDocumentTests(unittest.TestCase):
    def test_valid_document_round_trips_through_the_client_loader(self) -> None:
        document = build_client_config.build_client_document(
            _trust(), api_base_url="https://api.example.com"
        )
        self.assertEqual(document["schema"], "jarvis.product-client.v1")
        self.assertEqual(document["api_base_url"], "https://api.example.com")
        self.assertIs(document["allow_insecure_localhost"], False)
        self.assertTrue(_loads_ok(document))
        # validate_document must agree with the loader.
        build_client_config.validate_document(document)

    def test_non_https_origin_is_rejected(self) -> None:
        for url in (
            "http://api.example.com",
            "ftp://api.example.com",
            "api.example.com",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    build_client_config.build_client_document(
                        _trust(), api_base_url=url
                    )

    def test_origin_with_query_or_credentials_is_rejected(self) -> None:
        for url in (
            "https://api.example.com/?x=1",
            "https://user:pw@api.example.com",
            "https://api.example.com#frag",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    build_client_config.build_client_document(
                        _trust(), api_base_url=url
                    )

    def test_malformed_trust_material_is_rejected(self) -> None:
        cases: list[dict[str, object]] = [
            {**_trust(), "schema": "jarvis.product-client.v1"},  # wrong schema
            {k: v for k, v in _trust().items() if k != "release_public_keys"},
            {**_trust(), "extra": True},
            {**_trust(), "entitlement_public_keys": {}},
            {**_trust(), "entitlement_public_keys": {"ent": 123}},
        ]
        for bad in cases:
            with self.subTest(bad=sorted(bad)):
                with self.assertRaises(ValueError):
                    build_client_config.build_client_document(
                        bad, api_base_url="https://api.example.com"
                    )

    def test_localhost_dev_profile_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(ValueError):
            build_client_config.build_client_document(
                _trust(), api_base_url="http://127.0.0.1:8443"
            )
        document = build_client_config.build_client_document(
            _trust(),
            api_base_url="http://127.0.0.1:8443",
            allow_insecure_localhost=True,
        )
        self.assertIs(document["allow_insecure_localhost"], True)
        self.assertTrue(_loads_ok(document))

    def test_invalid_public_key_length_is_caught_by_validation(self) -> None:
        bad = {
            "schema": build_client_config.TRUST_SCHEMA,
            "entitlement_public_keys": {"entitlement-key-001": "A" * 42},
            "release_public_keys": {"release-key-001": _valid_key(2)},
        }
        document = build_client_config.build_client_document(
            bad, api_base_url="https://api.example.com"
        )
        with self.assertRaises(ValueError):
            build_client_config.validate_document(document)


class BuildClientConfigFileTests(unittest.TestCase):
    def test_written_file_loads_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            trust_file = temp_dir / "client-trust.json"
            trust_file.write_text(json.dumps(_trust()), encoding="utf-8")
            out_path = temp_dir / "config" / "product.json"
            build_client_config.build_client_config_file(
                trust_file=trust_file,
                api_base_url="https://api.example.com",
                out_path=out_path,
            )
            self.assertTrue(out_path.is_file())
            self.assertEqual(list(out_path.parent.glob(".product-*.tmp")), [])
            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["api_base_url"], "https://api.example.com")

    def test_symlink_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            trust_file = temp_dir / "client-trust.json"
            trust_file.write_text(json.dumps(_trust()), encoding="utf-8")
            real = temp_dir / "real.json"
            real.write_text("{}", encoding="utf-8")
            link = temp_dir / "product.json"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                build_client_config.build_client_config_file(
                    trust_file=trust_file,
                    api_base_url="https://api.example.com",
                    out_path=link,
                )

    def test_cli_rejects_http_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            trust_file = temp_dir / "client-trust.json"
            trust_file.write_text(json.dumps(_trust()), encoding="utf-8")
            code = build_client_config.main(
                [
                    "--trust-file",
                    str(trust_file),
                    "--api-base-url",
                    "http://api.example.com",
                    "--out",
                    str(temp_dir / "product.json"),
                ]
            )
        self.assertEqual(code, 1)


@unittest.skipUnless(POSIX, "secret generation requires POSIX owner-only output")
class GenSecretsTrustCorrespondenceTests(unittest.TestCase):
    def _generate(self, out_dir: Path) -> gen_secrets.SecretBundle:
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(out_dir, 0o700)
        return gen_secrets.generate_secret_bundle(
            out_dir,
            admin_subject="admin:ops",
            allowed_hosts="api.example.com",
            admin_password="a-strong-admin-password",
        )

    def test_client_entitlement_public_key_matches_backend_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secrets_dir = Path(temp) / "secrets"
            bundle = self._generate(secrets_dir)

            trust = bundle.client_trust
            self.assertEqual(trust["schema"], "jarvis.product-client-trust.v1")
            key_id = bundle.env["JARVIS_ENTITLEMENT_KEY_ID"]
            self.assertIn(key_id, trust["entitlement_public_keys"])

            private_bytes = (secrets_dir / "entitlement.key").read_bytes()
            private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
            derived_public = base64.urlsafe_b64encode(
                private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ).rstrip(b"=").decode("ascii")
            self.assertEqual(
                trust["entitlement_public_keys"][key_id], derived_public
            )

            # Full trust-chain proof: a signature from the backend private key
            # verifies under the public key the client pins.
            pinned = base64.urlsafe_b64decode(
                trust["entitlement_public_keys"][key_id] + "="
            )
            message = b"entitlement:0.1.0:device"
            signature = private_key.sign(message)
            Ed25519PublicKey.from_public_bytes(pinned).verify(signature, message)

    def test_release_public_key_matches_backend_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self._generate(Path(temp) / "secrets")
            env_release = json.loads(bundle.env["JARVIS_RELEASE_PUBLIC_KEYS_JSON"])
            self.assertEqual(env_release, bundle.client_trust["release_public_keys"])

    def test_generated_trust_builds_a_loadable_client_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secrets_dir = Path(temp) / "secrets"
            self._generate(secrets_dir)
            out_path = Path(temp) / "config" / "product.json"
            document = build_client_config.build_client_config_file(
                trust_file=secrets_dir / "client-trust.json",
                api_base_url="https://api.example.com",
                out_path=out_path,
            )
            self.assertEqual(document["schema"], "jarvis.product-client.v1")
            self.assertTrue(_loads_ok(document))


if __name__ == "__main__":
    unittest.main()

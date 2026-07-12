from __future__ import annotations

import base64
import inspect
import json
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import core.release_manifest as release_manifest_module
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion, SemanticVersion
from core.release_manifest import (
    ENVELOPE_SCHEMA,
    MANIFEST_SCHEMA,
    MAX_ARTIFACT_BYTES,
    MAX_COMPATIBLE_SOURCE_VERSIONS,
    SCHEMA_VERSION,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    ArtifactKind,
    verify_release_manifest,
)


OLD_KEY_ID = "release-key-001"
NEW_KEY_ID = "release-key-002"
VERSION = "2.0.0"
BUILD = 42
SHA256 = "ab" * 32
BYTE_SIZE = 512 * 1024 * 1024
STORAGE_KEY = "releases/2.0.0/macos/arm64/Jarvis.dmg"


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "bundle_id": BUNDLE_ID,
        "version": VERSION,
        "build": BUILD,
        "platform": "macos",
        "architecture": "arm64",
        "artifact_kind": ArtifactKind.INITIAL_INSTALLER.value,
        "sha256": SHA256,
        "byte_size": BYTE_SIZE,
        "storage_key": STORAGE_KEY,
        "signing_key_id": OLD_KEY_ID,
        "compatible_source_versions": [],
    }
    payload.update(overrides)
    return payload


def _envelope(
    payload_bytes: bytes,
    private_key: Ed25519PrivateKey,
    *,
    signature_bytes: bytes | None = None,
    **overrides: object,
) -> str:
    envelope: dict[str, object] = {
        "schema": ENVELOPE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "payload": _b64url(payload_bytes),
        "signature": _b64url(
            private_key.sign(payload_bytes)
            if signature_bytes is None
            else signature_bytes
        ),
    }
    envelope.update(overrides)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_private_key = Ed25519PrivateKey.generate()
        self.new_private_key = Ed25519PrivateKey.generate()
        self.trusted: dict[str, object] = {
            OLD_KEY_ID: self.old_private_key.public_key(),
            NEW_KEY_ID: self.new_private_key.public_key(),
        }

    def _certificate(
        self,
        *,
        private_key: Ed25519PrivateKey | None = None,
        **payload_overrides: object,
    ) -> str:
        return _envelope(
            _canonical(_payload(**payload_overrides)),
            self.old_private_key if private_key is None else private_key,
        )

    def _verify(
        self,
        manifest: str | bytes | None,
        *,
        trusted: dict[str, object] | None = None,
        **expectations: object,
    ):
        return verify_release_manifest(
            manifest,
            trusted_public_keys=self.trusted if trusted is None else trusted,
            **expectations,
        )

    def test_valid_initial_installer_exposes_canonical_verified_claims(self):
        payload_bytes = _canonical(_payload())
        manifest = _envelope(payload_bytes, self.old_private_key)

        result = self._verify(
            manifest,
            expected_platform="macos",
            expected_architecture="arm64",
            expected_version=VERSION,
            expected_build=BUILD,
            expected_artifact_kind="initial_installer",
            expected_sha256=SHA256,
            expected_byte_size=BYTE_SIZE,
            expected_storage_key=STORAGE_KEY,
        )

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.claims.version, SemanticVersion.parse(VERSION))
        self.assertEqual(result.claims.product_version, ProductVersion.parse(VERSION, BUILD))
        self.assertEqual(result.claims.artifact_kind, ArtifactKind.INITIAL_INSTALLER)
        self.assertEqual(result.claims.compatible_source_versions, ())
        self.assertEqual(result.claims.canonical_payload_bytes(), payload_bytes)

    def test_valid_update_package_requires_older_sorted_sources(self):
        manifest = self._certificate(
            artifact_kind="update_package",
            compatible_source_versions=["1.2.0", "1.10.0"],
            storage_key="releases/2.0.0/macos/arm64/Jarvis.update",
        )

        result = self._verify(manifest, expected_artifact_kind="update_package")

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(
            result.claims.compatible_source_versions,
            (SemanticVersion.parse("1.2.0"), SemanticVersion.parse("1.10.0")),
        )

    def test_none_is_not_found_but_empty_or_malformed_input_is_invalid(self):
        self.assertEqual(self._verify(None).status, STATUS_NOT_FOUND)
        for malformed in ("", b"", "null", "{}"):
            with self.subTest(malformed=malformed):
                self.assertEqual(self._verify(malformed).status, STATUS_INVALID)

    def test_key_rotation_accepts_each_pinned_key_and_rejects_wrong_mapping(self):
        old_manifest = self._certificate()
        new_manifest = self._certificate(
            private_key=self.new_private_key,
            signing_key_id=NEW_KEY_ID,
        )

        self.assertEqual(self._verify(old_manifest).status, STATUS_SUCCESS)
        self.assertEqual(self._verify(new_manifest).status, STATUS_SUCCESS)
        self.assertEqual(
            self._verify(
                new_manifest,
                trusted={OLD_KEY_ID: self.old_private_key.public_key()},
            ).status,
            STATUS_INVALID,
        )
        self.assertEqual(
            self._verify(
                old_manifest,
                trusted={OLD_KEY_ID: self.new_private_key.public_key()},
            ).status,
            STATUS_INVALID,
        )

    def test_raw_public_key_bytes_are_accepted(self):
        raw_public_key = self.old_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        result = self._verify(
            self._certificate(),
            trusted={OLD_KEY_ID: raw_public_key},
        )

        self.assertEqual(result.status, STATUS_SUCCESS)

    def test_tampered_payload_and_signature_are_invalid(self):
        original = _canonical(_payload())
        signature = self.old_private_key.sign(original)
        tampered_payload = _canonical(_payload(sha256="cd" * 32))
        tampered_signature = bytearray(signature)
        tampered_signature[0] ^= 1

        wrong_payload = self._verify(
            _envelope(
                tampered_payload,
                self.old_private_key,
                signature_bytes=signature,
            )
        )
        wrong_signature = self._verify(
            _envelope(
                original,
                self.old_private_key,
                signature_bytes=bytes(tampered_signature),
            )
        )

        self.assertEqual(wrong_payload.status, STATUS_INVALID)
        self.assertEqual(wrong_signature.status, STATUS_INVALID)

    def test_every_supplied_expectation_must_match(self):
        manifest = self._certificate()
        wrong_expectations = (
            {"expected_platform": "windows"},
            {"expected_architecture": "x86_64"},
            {"expected_version": "2.0.1"},
            {"expected_build": BUILD + 1},
            {"expected_artifact_kind": "update_package"},
            {"expected_sha256": "cd" * 32},
            {"expected_byte_size": BYTE_SIZE + 1},
            {"expected_storage_key": "releases/other/Jarvis.dmg"},
        )

        for expectation in wrong_expectations:
            with self.subTest(expectation=expectation):
                self.assertEqual(
                    self._verify(manifest, **expectation).status,
                    STATUS_INVALID,
                )

    def test_noncanonical_payload_is_invalid_even_when_signature_is_valid(self):
        noncanonical = json.dumps(_payload(), indent=2, sort_keys=False).encode("utf-8")

        result = self._verify(_envelope(noncanonical, self.old_private_key))

        self.assertEqual(result.status, STATUS_INVALID)

    def test_duplicate_payload_and_envelope_keys_are_invalid(self):
        payload = _canonical(_payload())
        duplicate_payload = payload[:-1] + f',"build":{BUILD}}}'.encode("ascii")
        duplicate_payload_result = self._verify(
            _envelope(duplicate_payload, self.old_private_key)
        )

        valid_envelope = json.loads(self._certificate())
        duplicate_envelope = (
            "{"
            f'"schema":"{ENVELOPE_SCHEMA}",'
            f'"schema_version":{SCHEMA_VERSION},'
            f'"payload":"{valid_envelope["payload"]}",'
            f'"signature":"{valid_envelope["signature"]}",'
            f'"signature":"{valid_envelope["signature"]}"'
            "}"
        )

        self.assertEqual(duplicate_payload_result.status, STATUS_INVALID)
        self.assertEqual(self._verify(duplicate_envelope).status, STATUS_INVALID)

    def test_unknown_expiry_revocation_missing_and_wrong_type_fields_are_invalid(self):
        missing = _payload()
        del missing["storage_key"]
        payloads = (
            missing,
            _payload(expires_at="2030-01-01T00:00:00Z"),
            _payload(revoked_at=None),
            _payload(schema_version=True),
            _payload(build=True),
            _payload(byte_size="100"),
            _payload(artifact_kind="delta"),
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                manifest = _envelope(_canonical(payload), self.old_private_key)
                self.assertEqual(self._verify(manifest).status, STATUS_INVALID)

    def test_product_semver_target_build_hash_and_size_are_strict(self):
        invalid_claims = (
            {"product_id": "other"},
            {"bundle_id": "com.example.other"},
            {"version": "2.0"},
            {"version": "2.0.0-alpha"},
            {"build": 0},
            {"build": -1},
            {"platform": "Darwin"},
            {"architecture": "aarch64"},
            {"sha256": SHA256.upper()},
            {"sha256": "a" * 63},
            {"byte_size": 0},
            {"byte_size": MAX_ARTIFACT_BYTES + 1},
        )

        for claims in invalid_claims:
            with self.subTest(claims=claims):
                self.assertEqual(
                    self._verify(self._certificate(**claims)).status,
                    STATUS_INVALID,
                )

    def test_source_compatibility_rules_fail_closed(self):
        invalid_sources = (
            {
                "artifact_kind": "initial_installer",
                "compatible_source_versions": ["1.0.0"],
            },
            {"artifact_kind": "update_package", "compatible_source_versions": []},
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": ["2.0.0"],
            },
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": ["2.0.1"],
            },
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": ["1.10.0", "1.2.0"],
            },
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": ["1.0.0", "1.0.0"],
            },
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": ["1.0"],
            },
            {
                "artifact_kind": "update_package",
                "compatible_source_versions": "1.0.0",
            },
        )

        for claims in invalid_sources:
            with self.subTest(claims=claims):
                self.assertEqual(
                    self._verify(self._certificate(**claims)).status,
                    STATUS_INVALID,
                )

    def test_storage_key_is_opaque_private_and_path_safe(self):
        invalid_keys = (
            "https://public.invalid/Jarvis.dmg",
            "/absolute/Jarvis.dmg",
            "../outside/Jarvis.dmg",
            "releases/../outside/Jarvis.dmg",
            "releases//Jarvis.dmg",
            "releases\\Jarvis.dmg",
            "releases/Jarvis.dmg/",
            "releases/secret\nJarvis.dmg",
            "x" * 513,
        )

        for storage_key in invalid_keys:
            with self.subTest(storage_key=storage_key):
                self.assertEqual(
                    self._verify(self._certificate(storage_key=storage_key)).status,
                    STATUS_INVALID,
                )

    def test_dos_bounds_reject_huge_source_list_payload_and_envelope(self):
        sources = [f"1.0.{index}" for index in range(MAX_COMPATIBLE_SOURCE_VERSIONS + 1)]
        huge_sources = self._certificate(
            artifact_kind="update_package",
            compatible_source_versions=sources,
        )
        huge_payload_bytes = _canonical(
            _payload(padding="x" * (40 * 1024))
        )
        huge_payload = _envelope(huge_payload_bytes, self.old_private_key)

        self.assertEqual(self._verify(huge_sources).status, STATUS_INVALID)
        self.assertEqual(self._verify(huge_payload).status, STATUS_INVALID)
        self.assertEqual(self._verify("x" * (70 * 1024)).status, STATUS_INVALID)

    def test_bad_base64url_and_signature_length_are_invalid(self):
        payload = _canonical(_payload())
        manifests = (
            _envelope(payload, self.old_private_key, payload="bad+base64"),
            _envelope(payload, self.old_private_key, signature="padded=="),
            _envelope(payload, self.old_private_key, signature_bytes=b"short"),
        )

        for manifest in manifests:
            with self.subTest(manifest=manifest):
                self.assertEqual(self._verify(manifest).status, STATUS_INVALID)

    def test_missing_crypto_or_trusted_keys_are_not_available(self):
        manifest = self._certificate()

        self.assertEqual(
            self._verify(manifest, trusted={}).status,
            STATUS_NOT_AVAILABLE,
        )
        self.assertEqual(
            self._verify(manifest, trusted={OLD_KEY_ID: object()}).status,
            STATUS_NOT_AVAILABLE,
        )
        with mock.patch("core.release_manifest._ED25519_AVAILABLE", False):
            result = self._verify(manifest)
        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)

    def test_result_and_claims_repr_hide_storage_signature_and_raw_manifest(self):
        manifest = self._certificate()
        signature = json.loads(manifest)["signature"]
        result = self._verify(manifest)
        rendered = repr(result) + repr(result.claims)

        self.assertNotIn(manifest, rendered)
        self.assertNotIn(signature, rendered)
        self.assertNotIn(STORAGE_KEY, rendered)

    def test_production_module_contains_no_private_key_or_signing_code(self):
        source = inspect.getsource(release_manifest_module)

        self.assertNotIn("Ed25519PrivateKey", source)
        self.assertNotIn(".sign(", source)


if __name__ == "__main__":
    unittest.main()

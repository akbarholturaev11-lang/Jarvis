from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA,
    SCHEMA_VERSION,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    verify_entitlement_certificate,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID, SemanticVersion


KEY_ID = "entitlement-key-001"
LICENSE_ID = "lic_0123456789abcdef0123456789abcdef"
DEVICE_FINGERPRINT = "sha256:" + ("a1" * 32)
OTHER_DEVICE_FINGERPRINT = "sha256:" + ("b2" * 32)
VERSION = "1.2.3"


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
    claims: dict[str, object] = {
        "schema": CERTIFICATE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "bundle_id": BUNDLE_ID,
        "license_id": LICENSE_ID,
        "device_key_fingerprint": DEVICE_FINGERPRINT,
        "version": VERSION,
        "issued_at": "2020-01-02T03:04:05Z",
        "key_id": KEY_ID,
    }
    claims.update(overrides)
    return claims


def _envelope(
    payload_bytes: bytes,
    private_key: Ed25519PrivateKey,
    *,
    signature_bytes: bytes | None = None,
    **overrides: object,
) -> str:
    document: dict[str, object] = {
        "schema": ENVELOPE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "payload": _b64url(payload_bytes),
        "signature": _b64url(
            private_key.sign(payload_bytes)
            if signature_bytes is None
            else signature_bytes
        ),
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


class EntitlementCertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        self.other_private_key = Ed25519PrivateKey.generate()
        self.trusted = {KEY_ID: self.private_key.public_key()}

    def _verify(
        self,
        certificate: str | bytes | None,
        *,
        trusted: dict[str, object] | None = None,
        device: str = DEVICE_FINGERPRINT,
        version: str | SemanticVersion = VERSION,
    ):
        return verify_entitlement_certificate(
            certificate,
            trusted_public_keys=self.trusted if trusted is None else trusted,
            expected_license_id=LICENSE_ID,
            expected_device_fingerprint=device,
            expected_version=version,
        )

    def _valid_certificate(self, **payload_overrides: object) -> str:
        payload_bytes = _canonical(_payload(**payload_overrides))
        return _envelope(payload_bytes, self.private_key)

    def test_valid_exact_version_verifies_offline_without_expiry(self):
        certificate = self._valid_certificate()

        result = self._verify(certificate)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.certificate)
        self.assertEqual(result.certificate.license_id, LICENSE_ID)
        self.assertEqual(result.certificate.version, SemanticVersion.parse(VERSION))
        self.assertEqual(result.certificate.issued_at, "2020-01-02T03:04:05Z")

    def test_none_is_only_not_found_case(self):
        self.assertEqual(self._verify(None).status, STATUS_NOT_FOUND)
        for malformed in ("", b"", "null", "{}"):
            with self.subTest(malformed=malformed):
                self.assertEqual(self._verify(malformed).status, STATUS_INVALID)

    def test_valid_raw_public_key_bytes_are_supported(self):
        raw_public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        result = self._verify(
            self._valid_certificate(),
            trusted={KEY_ID: raw_public_key},
        )

        self.assertEqual(result.status, STATUS_SUCCESS)

    def test_key_rotation_accepts_each_explicitly_trusted_key(self):
        rotated_key_id = "entitlement-key-002"
        rotated_payload = _canonical(_payload(key_id=rotated_key_id))
        certificate = _envelope(rotated_payload, self.other_private_key)

        result = self._verify(
            certificate,
            trusted={
                KEY_ID: self.private_key.public_key(),
                rotated_key_id: self.other_private_key.public_key(),
            },
        )

        self.assertEqual(result.status, STATUS_SUCCESS)

    def test_tampered_signed_claim_is_invalid(self):
        original = _canonical(_payload())
        signature = self.private_key.sign(original)
        tampered = _canonical(_payload(license_id="lic_fedcba9876543210fedcba9876543210"))

        result = self._verify(
            _envelope(tampered, self.private_key, signature_bytes=signature)
        )

        self.assertEqual(result.status, STATUS_INVALID)

    def test_wrong_device_and_next_semver_are_invalid_even_when_signed(self):
        wrong_device = self._verify(
            self._valid_certificate(),
            device=OTHER_DEVICE_FINGERPRINT,
        )
        next_version = self._verify(
            self._valid_certificate(version="1.2.4"),
            version=VERSION,
        )

        self.assertEqual(wrong_device.status, STATUS_INVALID)
        self.assertEqual(next_version.status, STATUS_INVALID)

    def test_signed_certificate_for_another_license_is_invalid(self):
        another_license = "lic_fedcba9876543210fedcba9876543210"

        result = self._verify(
            self._valid_certificate(license_id=another_license)
        )

        self.assertEqual(result.status, STATUS_INVALID)

    def test_wrong_trusted_key_and_unknown_key_id_are_invalid(self):
        wrong_key = self._verify(
            self._valid_certificate(),
            trusted={KEY_ID: self.other_private_key.public_key()},
        )
        unknown_key_id = self._verify(
            self._valid_certificate(key_id="entitlement-key-002")
        )

        self.assertEqual(wrong_key.status, STATUS_INVALID)
        self.assertEqual(unknown_key_id.status, STATUS_INVALID)

    def test_noncanonical_payload_is_rejected_even_with_valid_signature(self):
        noncanonical = json.dumps(_payload(), indent=2, sort_keys=False).encode("utf-8")
        certificate = _envelope(noncanonical, self.private_key)

        result = self._verify(certificate)

        self.assertEqual(result.status, STATUS_INVALID)

    def test_duplicate_payload_and_envelope_keys_are_rejected(self):
        payload = _canonical(_payload())
        duplicate_payload = payload[:-1] + b',"version":"1.2.3"}'
        duplicate_payload_result = self._verify(
            _envelope(duplicate_payload, self.private_key)
        )

        valid = json.loads(self._valid_certificate())
        duplicate_envelope = (
            "{"
            f'"schema":"{ENVELOPE_SCHEMA}",'
            f'"schema_version":{SCHEMA_VERSION},'
            f'"payload":"{valid["payload"]}",'
            f'"signature":"{valid["signature"]}",'
            f'"signature":"{valid["signature"]}"'
            "}"
        )
        duplicate_envelope_result = self._verify(duplicate_envelope)

        self.assertEqual(duplicate_payload_result.status, STATUS_INVALID)
        self.assertEqual(duplicate_envelope_result.status, STATUS_INVALID)

    def test_expiry_and_remote_revocation_fields_are_forbidden(self):
        forbidden_claims = (
            {"expires_at": "2030-01-01T00:00:00Z"},
            {"revoked_at": None},
            {"revocation_url": "https://example.invalid/revoke"},
        )

        for extra in forbidden_claims:
            with self.subTest(extra=extra):
                result = self._verify(self._valid_certificate(**extra))
                self.assertEqual(result.status, STATUS_INVALID)

    def test_exactly_one_strict_semver_string_is_required(self):
        invalid_versions = (
            {"version": "1.2"},
            {"version": "1.2.3-alpha"},
            {"version": ["1.2.3"]},
            {"versions": ["1.2.3"]},
        )

        for changes in invalid_versions:
            with self.subTest(changes=changes):
                payload = _payload()
                if "versions" in changes:
                    payload.update(changes)
                else:
                    payload["version"] = changes["version"]
                result = self._verify(_envelope(_canonical(payload), self.private_key))
                self.assertEqual(result.status, STATUS_INVALID)

    def test_unknown_missing_and_wrong_type_fields_fail_closed(self):
        cases: list[dict[str, object]] = []
        missing = _payload()
        del missing["license_id"]
        cases.append(missing)
        cases.append(_payload(schema_version=True))
        cases.append(_payload(license_id=123))
        cases.append(_payload(unknown="field"))
        cases.append(_payload(schema="jarvis.entitlement.future"))

        for payload in cases:
            with self.subTest(payload=payload):
                result = self._verify(_envelope(_canonical(payload), self.private_key))
                self.assertEqual(result.status, STATUS_INVALID)

    def test_product_bundle_identifiers_and_normalized_ids_are_enforced(self):
        invalid_claims = (
            {"product_id": "other"},
            {"bundle_id": "com.example.other"},
            {"license_id": "LIC_0123456789ABCDEF"},
            {"license_id": " lic_0123"},
            {"key_id": "Key-01"},
            {"device_key_fingerprint": "sha256:" + ("A1" * 32)},
            {"device_key_fingerprint": "device-01"},
        )

        for claims in invalid_claims:
            with self.subTest(claims=claims):
                self.assertEqual(
                    self._verify(self._valid_certificate(**claims)).status,
                    STATUS_INVALID,
                )

    def test_issued_at_requires_real_normalized_utc_iso8601(self):
        invalid_timestamps = (
            "2026-07-13T00:00:00+00:00",
            "2026-07-13T01:00:00+01:00",
            "2026-07-13 00:00:00Z",
            "2026-02-30T00:00:00Z",
            123,
        )

        for issued_at in invalid_timestamps:
            with self.subTest(issued_at=issued_at):
                self.assertEqual(
                    self._verify(self._valid_certificate(issued_at=issued_at)).status,
                    STATUS_INVALID,
                )

    def test_bad_base64url_and_bad_signature_length_are_invalid(self):
        payload = _canonical(_payload())
        cases = (
            _envelope(payload, self.private_key, payload="not+base64"),
            _envelope(payload, self.private_key, signature="padded=="),
            _envelope(payload, self.private_key, signature_bytes=b"short"),
        )

        for certificate in cases:
            with self.subTest(certificate=certificate):
                self.assertEqual(self._verify(certificate).status, STATUS_INVALID)

    def test_oversized_inputs_and_invalid_trusted_key_fail_closed(self):
        oversized_text = "x" * (32 * 1024 + 1)
        oversized_payload = b"{" + (b" " * (8 * 1024)) + b"}"

        self.assertEqual(self._verify(oversized_text).status, STATUS_INVALID)
        self.assertEqual(
            self._verify(_envelope(oversized_payload, self.private_key)).status,
            STATUS_INVALID,
        )
        self.assertEqual(
            self._verify(
                self._valid_certificate(),
                trusted={KEY_ID: b"too-short"},
            ).status,
            STATUS_NOT_AVAILABLE,
        )

    def test_missing_crypto_or_trusted_key_configuration_is_not_available(self):
        certificate = self._valid_certificate()

        self.assertEqual(
            self._verify(certificate, trusted={}).status,
            STATUS_NOT_AVAILABLE,
        )
        with mock.patch("core.entitlement_certificate._ED25519_AVAILABLE", False):
            result = self._verify(certificate)
        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)

    def test_result_repr_never_contains_raw_certificate_signature_or_key(self):
        certificate = self._valid_certificate()
        envelope = json.loads(certificate)
        raw_public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        result = self._verify(certificate)
        rendered = repr(result)

        self.assertNotIn(certificate, rendered)
        self.assertNotIn(envelope["signature"], rendered)
        self.assertNotIn(_b64url(raw_public_key), rendered)
        self.assertNotIn(LICENSE_ID, rendered)
        self.assertNotIn(LICENSE_ID, repr(result.certificate))
        self.assertNotIn(DEVICE_FINGERPRINT, repr(result.certificate))


if __name__ == "__main__":
    unittest.main()

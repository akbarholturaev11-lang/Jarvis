from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.entitlement_cache import (
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SignedEntitlementCache,
)
from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA,
    SCHEMA_VERSION,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID


KEY_ID = "entitlement-key-001"
LICENSE_ID = "lic_cache_001"
FINGERPRINT = "sha256:" + ("a" * 64)
VERSION = "1.2.0"


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


def _certificate(private_key: Ed25519PrivateKey, **overrides: object) -> str:
    payload: dict[str, object] = {
        "schema": CERTIFICATE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "bundle_id": BUNDLE_ID,
        "license_id": LICENSE_ID,
        "device_key_fingerprint": FINGERPRINT,
        "version": VERSION,
        "issued_at": "2026-07-13T03:00:00Z",
        "key_id": KEY_ID,
    }
    payload.update(overrides)
    raw = _canonical(payload)
    return json.dumps(
        {
            "schema": ENVELOPE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "payload": _b64url(raw),
            "signature": _b64url(private_key.sign(raw)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class SignedEntitlementCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "entitlements"
        self.private_key = Ed25519PrivateKey.generate()
        self.cache = SignedEntitlementCache(
            self.root,
            trusted_public_keys={KEY_ID: self.private_key.public_key()},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_verified_certificate_is_atomically_cached_and_reverified_offline(self):
        certificate = _certificate(self.private_key)

        stored = self.cache.store_verified(
            certificate,
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        loaded = self.cache.load_verified(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )

        self.assertEqual(stored.status, STATUS_SUCCESS)
        self.assertEqual(loaded.status, STATUS_SUCCESS)
        self.assertEqual(str(loaded.certificate.version), VERSION)
        path = self.cache.certificate_path(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        self.assertEqual(path.read_text(), certificate)
        if os.name != "nt":
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(LICENSE_ID, path.name)
        self.assertNotIn(LICENSE_ID, repr(self.cache))
        self.assertNotIn(certificate, repr(stored))

    def test_wrong_device_version_or_signature_is_never_persisted(self):
        wrong_key = Ed25519PrivateKey.generate()
        cases = (
            (
                _certificate(self.private_key),
                "sha256:" + ("b" * 64),
                VERSION,
            ),
            (_certificate(self.private_key), FINGERPRINT, "1.3.0"),
            (_certificate(wrong_key), FINGERPRINT, VERSION),
        )
        for certificate, fingerprint, version in cases:
            with self.subTest(fingerprint=fingerprint, version=version):
                result = self.cache.store_verified(
                    certificate,
                    license_id=LICENSE_ID,
                    device_fingerprint=fingerprint,
                    version=version,
                )
                self.assertEqual(result.status, STATUS_INVALID)
                path = self.cache.certificate_path(
                    license_id=LICENSE_ID,
                    device_fingerprint=fingerprint,
                    version=version,
                )
                self.assertFalse(path.exists())

    def test_tampered_cache_and_symlink_are_rejected_on_every_read(self):
        certificate = _certificate(self.private_key)
        self.cache.store_verified(
            certificate,
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        path = self.cache.certificate_path(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        envelope = json.loads(path.read_text())
        signature = envelope["signature"]
        envelope["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        tampered = self.cache.load_verified(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        self.assertEqual(tampered.status, STATUS_INVALID)

        path.unlink()
        target = self.root / "outside.entitlement"
        target.write_text(certificate)
        path.symlink_to(target)
        linked = self.cache.load_verified(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        self.assertEqual(linked.status, STATUS_INVALID)

    def test_missing_and_unavailable_pinned_keys_are_honest(self):
        missing = self.cache.load_verified(
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )
        unavailable_cache = SignedEntitlementCache(
            Path(self.temp.name) / "no-keys",
            trusted_public_keys={},
        )
        unavailable = unavailable_cache.store_verified(
            _certificate(self.private_key),
            license_id=LICENSE_ID,
            device_fingerprint=FINGERPRINT,
            version=VERSION,
        )

        self.assertEqual(missing.status, STATUS_NOT_FOUND)
        self.assertEqual(unavailable.status, STATUS_NOT_AVAILABLE)


if __name__ == "__main__":
    unittest.main()

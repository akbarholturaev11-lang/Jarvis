from __future__ import annotations

import unittest

from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    PRODUCT_NAME,
    ProductVersion,
    ReleaseIdentity,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
    require_monotonic_upgrade,
    validate_release_manifest,
)


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "product_id": PRODUCT_ID,
        "bundle_id": BUNDLE_ID,
        "version": "0.6.0",
        "build": 1,
        "platform": "Darwin",
        "architecture": "arm64",
    }
    manifest.update(overrides)
    return manifest


class SemanticVersionTests(unittest.TestCase):
    def test_strict_semver_parses_and_orders(self):
        version = SemanticVersion.parse("12.3.40")

        self.assertEqual((version.major, version.minor, version.patch), (12, 3, 40))
        self.assertEqual(str(version), "12.3.40")
        self.assertLess(SemanticVersion.parse("1.9.9"), SemanticVersion.parse("2.0.0"))

    def test_strict_semver_rejects_non_triplet_forms(self):
        invalid = (
            "1.2",
            "1.2.3.4",
            "v1.2.3",
            "1.2.3-alpha",
            "1.2.3+4",
            "01.2.3",
            "1.02.3",
            "1.2.03",
            " 1.2.3",
            "1.2.3 ",
            "-1.2.3",
            "",
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SemanticVersion.parse(value)

    def test_semver_rejects_wrong_component_types(self):
        with self.assertRaises(TypeError):
            SemanticVersion(True, 0, 0)
        with self.assertRaises(TypeError):
            SemanticVersion.parse(123)  # type: ignore[arg-type]


class ProductVersionTests(unittest.TestCase):
    def test_product_and_bundle_identifiers_are_stable(self):
        self.assertEqual(PRODUCT_ID, "jarvis")
        self.assertEqual(PRODUCT_NAME, "JARVIS")
        self.assertEqual(BUNDLE_ID, "com.jarvis.assistant")

    def test_product_version_requires_positive_integer_build(self):
        for invalid in (0, -1):
            with self.subTest(build=invalid), self.assertRaises(ValueError):
                ProductVersion.parse("0.6.0", invalid)
        for invalid in (True, 1.0, "1"):
            with self.subTest(build=invalid), self.assertRaises(TypeError):
                ProductVersion.parse("0.6.0", invalid)  # type: ignore[arg-type]

    def test_build_must_increase_monotonically(self):
        previous = ProductVersion.parse("0.6.0", 10)
        rebuild = ProductVersion.parse("0.6.0", 11)
        next_version = ProductVersion.parse("0.7.0", 12)

        self.assertIs(require_monotonic_upgrade(previous, rebuild), rebuild)
        self.assertIs(require_monotonic_upgrade(rebuild, next_version), next_version)
        self.assertTrue(rebuild.is_newer_than(previous))

    def test_non_monotonic_or_downgrade_candidate_is_rejected(self):
        previous = ProductVersion.parse("0.6.0", 10)

        with self.assertRaises(ValueError):
            require_monotonic_upgrade(previous, ProductVersion.parse("0.7.0", 10))
        with self.assertRaises(ValueError):
            require_monotonic_upgrade(previous, ProductVersion.parse("0.7.0", 9))
        with self.assertRaises(ValueError):
            require_monotonic_upgrade(previous, ProductVersion.parse("0.5.9", 11))

    def test_module_does_not_claim_a_current_release(self):
        import core.product_version as product_version

        self.assertFalse(hasattr(product_version, "CURRENT_VERSION"))
        self.assertFalse(hasattr(product_version, "APP_VERSION"))


class ReleaseTargetTests(unittest.TestCase):
    def test_platform_normalization(self):
        cases = {
            "Darwin": "macos",
            "macOS": "macos",
            "win32": "windows",
            "Windows": "windows",
            "Linux": "linux",
            "linux2": "linux",
            "Plan9": "unknown",
            "": "unknown",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_platform(raw), expected)
        self.assertEqual(normalize_platform(123), "unknown")

    def test_architecture_normalization(self):
        cases = {
            "AMD64": "x86_64",
            "x86_64": "x86_64",
            "i686": "x86",
            "aarch64": "arm64",
            "ARM64": "arm64",
            "armv7l": "armv7",
            "universal": "universal2",
            "sparc": "unknown",
            "": "unknown",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_architecture(raw), expected)
        self.assertEqual(normalize_architecture(False), "unknown")

    def test_manifest_identity_is_validated_and_normalized(self):
        identity = validate_release_manifest(
            _manifest(notes={"en": "ignored by identity validation"})
        )

        self.assertIsInstance(identity, ReleaseIdentity)
        self.assertEqual(str(identity.version), "0.6.0")
        self.assertEqual(identity.build, 1)
        self.assertEqual(identity.platform, "macos")
        self.assertEqual(identity.architecture, "arm64")
        self.assertEqual(identity.product_version, ProductVersion.parse("0.6.0", 1))

    def test_manifest_rejects_wrong_product_or_bundle_identity(self):
        with self.assertRaises(ValueError):
            validate_release_manifest(_manifest(product_id="other"))
        with self.assertRaises(ValueError):
            validate_release_manifest(_manifest(bundle_id="com.example.other"))

    def test_manifest_rejects_missing_or_invalid_identity_fields(self):
        missing = _manifest()
        del missing["architecture"]
        invalid_manifests = (
            missing,
            _manifest(version="0.6"),
            _manifest(build=0),
            _manifest(build=True),
            _manifest(platform="Plan9"),
            _manifest(architecture="sparc"),
        )

        for manifest in invalid_manifests:
            with self.subTest(manifest=manifest), self.assertRaises((TypeError, ValueError)):
                validate_release_manifest(manifest)
        with self.assertRaises(TypeError):
            validate_release_manifest([])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

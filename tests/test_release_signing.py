from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.platform_adapters.release_base import ReleaseCapabilityStatus
from core.platform_adapters.release_signing import (
    ENV_NOTARY_PROFILE,
    ENV_SIGN_IDENTITY,
    SigningConfig,
    enumerate_signable_paths,
    plan_macos_signing,
)


IDENTITY = "Developer ID Application: JARVIS Test (AB12CD34EF)"


def _fake_app(root: Path) -> Path:
    app = root / "JARVIS.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks" / "Python.framework").mkdir(parents=True)
    (app / "Contents" / "Resources" / "lib").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "JARVIS").write_bytes(b"\xcf\xfa\xed\xfe")
    (app / "Contents" / "Resources" / "lib" / "libfoo.dylib").write_bytes(b"\x00")
    return app


class SigningConfigTests(unittest.TestCase):
    def test_env_absent_cannot_sign_or_notarize(self):
        config = SigningConfig.from_env({})
        self.assertFalse(config.can_sign)
        self.assertFalse(config.can_notarize)

    def test_team_id_and_profile_are_validated(self):
        config = SigningConfig.from_env(
            {
                ENV_SIGN_IDENTITY: IDENTITY,
                "JARVIS_MACOS_TEAM_ID": "AB12CD34EF",
                ENV_NOTARY_PROFILE: "jarvis-notary",
            }
        )
        self.assertTrue(config.can_sign)
        self.assertTrue(config.can_notarize)
        self.assertEqual(config.team_id, "AB12CD34EF")

    def test_malformed_team_id_is_rejected(self):
        config = SigningConfig.from_env({"JARVIS_MACOS_TEAM_ID": "short"})
        self.assertIsNone(config.team_id)


class SigningPlanTests(unittest.TestCase):
    def test_no_identity_is_honest_unsigned_dev_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = _fake_app(root)
            entitlements = root / "entitlements.plist"
            entitlements.write_text("<plist/>", encoding="utf-8")
            plan = plan_macos_signing(
                app_path=app,
                entitlements_path=entitlements,
                resource_root=root,
                package_path=root / "JARVIS.dmg",
                config=SigningConfig(identity=None, team_id=None, notary_profile=None),
                codesign_tool=entitlements,  # any existing file stands in for the tool
            )
        self.assertEqual(plan.status, ReleaseCapabilityStatus.NOT_AVAILABLE)
        self.assertTrue(plan.unsigned_dev_build)
        self.assertFalse(plan.signed)
        self.assertEqual(plan.codesign_commands, ())
        self.assertTrue(any(ENV_SIGN_IDENTITY in item for item in plan.missing_requirements))

    def test_enumerate_signs_nested_before_outer(self):
        with tempfile.TemporaryDirectory() as temp:
            app = _fake_app(Path(temp))
            nested = enumerate_signable_paths(app)
        names = [path.name for path in nested]
        self.assertIn("libfoo.dylib", names)
        self.assertIn("Python.framework", names)
        self.assertIn("JARVIS", names)
        self.assertNotIn("JARVIS.app", names)
        # Deepest path (libfoo.dylib, 4 components) must sign first.
        self.assertEqual(nested[0].name, "libfoo.dylib")

    def test_full_signing_plan_is_ordered_and_credential_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = _fake_app(root)
            entitlements = root / "entitlements.plist"
            entitlements.write_text("<plist/>", encoding="utf-8")
            package = root / "JARVIS.dmg"
            package.write_bytes(b"dmg")
            plan = plan_macos_signing(
                app_path=app,
                entitlements_path=entitlements,
                resource_root=root,
                package_path=package,
                config=SigningConfig(
                    identity=IDENTITY,
                    team_id="AB12CD34EF",
                    notary_profile="jarvis-notary",
                ),
                codesign_tool=entitlements,
            )

        self.assertEqual(plan.status, ReleaseCapabilityStatus.AVAILABLE)
        self.assertTrue(plan.signed)
        self.assertFalse(plan.unsigned_dev_build)
        # Outer bundle is signed last.
        self.assertEqual(plan.codesign_commands[-1].argv[-1], str(app))
        # Every codesign command uses the hardened runtime and timestamp.
        for command in plan.codesign_commands:
            self.assertIn("runtime", command.argv)
            self.assertIn("--timestamp", command.argv)
            self.assertIn("--entitlements", command.argv)
        verify_names = [c.name for c in plan.verify_commands]
        self.assertIn("codesign_verify", verify_names)
        self.assertIn("spctl_assess_app", verify_names)
        notarize_names = [c.name for c in plan.notarize_commands]
        self.assertEqual(
            notarize_names,
            ["notarytool_submit", "stapler_staple", "stapler_validate", "spctl_assess_install"],
        )
        # No command argv contains a private-key blob or password.
        flat = " ".join(
            " ".join(c.argv)
            for c in (*plan.codesign_commands, *plan.verify_commands, *plan.notarize_commands)
        )
        self.assertNotIn("PRIVATE KEY", flat)
        self.assertNotIn("password", flat.lower())

    def test_signing_available_but_notarization_missing_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = _fake_app(root)
            entitlements = root / "entitlements.plist"
            entitlements.write_text("<plist/>", encoding="utf-8")
            plan = plan_macos_signing(
                app_path=app,
                entitlements_path=entitlements,
                resource_root=root,
                package_path=root / "JARVIS.dmg",
                config=SigningConfig(
                    identity=IDENTITY, team_id="AB12CD34EF", notary_profile=None
                ),
                codesign_tool=entitlements,
            )
        self.assertEqual(plan.status, ReleaseCapabilityStatus.AVAILABLE)
        self.assertTrue(plan.signed)
        self.assertEqual(plan.notarize_commands, ())
        self.assertTrue(
            any(ENV_NOTARY_PROFILE in item for item in plan.missing_requirements)
        )


if __name__ == "__main__":
    unittest.main()

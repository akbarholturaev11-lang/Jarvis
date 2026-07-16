from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = PROJECT_ROOT / "packaging" / "macos"
SPEC = PACKAGING / "Jarvis.spec"
ENTITLEMENTS = PACKAGING / "entitlements.plist"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "macos-release.yml"

SCRIPTS = (
    "clean.sh",
    "build_app.sh",
    "verify_app.sh",
    "build_dmg.sh",
    "generate_manifest.sh",
    "sign_artifact.sh",
    "smoke_launch.sh",
    "cleanup.sh",
    "make_icns.sh",
    "build_all.sh",
    "_common.sh",
)


class SpecResourceAndImportTests(unittest.TestCase):
    def setUp(self):
        self.spec = SPEC.read_text(encoding="utf-8")

    def test_spec_bundles_required_nonsecret_resources(self):
        for required in (
            "prompt.txt",
            "settings.json",
            "device_profile.example.json",
            "briefing_sources.example.json",
            "dashboard/static",
            "JARVIS_PRODUCT_CONFIG",
            "JARVIS_BUILD_METADATA",
        ):
            self.assertIn(required, self.spec)

    def test_spec_never_bundles_secret_material(self):
        for forbidden in (
            "api_keys.json",
            "long_term.json",
            "briefing_sources.json",
            "local_env.zsh",
            "device_profile.json",
            "config/certs",
            "macros.json",
            "payment_instructions.json",
        ):
            # example templates are allowed; the real secret filenames are not
            if forbidden.endswith("example.json"):
                continue
            self.assertNotIn(f'"{forbidden}"', self.spec)
            self.assertNotIn(f"/{forbidden}", self.spec)

    def test_spec_declares_required_hidden_imports(self):
        for hidden in (
            "collect_submodules",
            "collect_data_files",
            "google.genai",
            "google.generativeai",
            "PyQt6",
            "cryptography",
            "PIL",
            "cv2",
            "uvicorn",
        ):
            self.assertIn(hidden, self.spec)

    def test_spec_includes_the_updater_helper(self):
        # The frozen client must ship the update/verify/rollback helper modules.
        self.assertIn("core.macos_update", self.spec)
        self.assertIn("core.installer", self.spec)
        self.assertIn("core.update_startup", self.spec)
        self.assertTrue((PROJECT_ROOT / "core" / "macos_update.py").is_file())
        self.assertTrue((PROJECT_ROOT / "core" / "installer.py").is_file())

    def test_spec_is_a_windowed_app_needing_no_terminal_or_system_python(self):
        # A windowed .app bundle launched by double-click => no Terminal.
        self.assertIn("console=False", self.spec)
        self.assertIn("BUNDLE(", self.spec)
        # An embedded interpreter (COLLECT of PyInstaller binaries) => no system
        # Python requirement on the customer machine.
        self.assertIn("COLLECT(", self.spec)
        self.assertIn("LSMinimumSystemVersion", self.spec)

    def test_spec_icon_is_optional_and_bilingual_permissions_present(self):
        self.assertIn("JARVIS_APP_ICON", self.spec)
        self.assertIn("требуется", self.spec)  # bilingual usage strings


class EntitlementsTests(unittest.TestCase):
    def test_entitlements_are_hardened_runtime_without_sandbox(self):
        data = plistlib.loads(ENTITLEMENTS.read_bytes())
        for key in (
            "com.apple.security.cs.allow-unsigned-executable-memory",
            "com.apple.security.cs.disable-library-validation",
            "com.apple.security.device.audio-input",
            "com.apple.security.device.camera",
            "com.apple.security.automation.apple-events",
            "com.apple.security.network.client",
            "com.apple.security.network.server",
        ):
            self.assertIs(data.get(key), True)
        for forbidden in (
            "com.apple.security.cs.allow-jit",
            "com.apple.security.cs.allow-dyld-environment-variables",
        ):
            self.assertNotIn(forbidden, data)
        # Never silently enable the App Sandbox for this desktop automation app.
        self.assertNotIn("com.apple.security.app-sandbox", data)


class ScriptSafetyTests(unittest.TestCase):
    def test_all_scripts_exist_and_are_strict(self):
        for name in SCRIPTS:
            path = PACKAGING / name
            self.assertTrue(path.is_file(), f"missing {name}")
            body = path.read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", body)

    def test_scripts_do_not_leak_secret_env_values(self):
        for name in SCRIPTS:
            body = (PACKAGING / name).read_text(encoding="utf-8")
            self.assertNotIn("sudo", body)
            # Never echo/print signing or notary secrets.
            for secret_env in (
                "JARVIS_MACOS_CERT_PASSWORD",
                "JARVIS_MACOS_KEYCHAIN_PASSWORD",
                "JARVIS_MACOS_NOTARY_PASSWORD",
            ):
                self.assertNotIn(f"echo ${secret_env}", body)
                self.assertNotIn(f'echo "${secret_env}"', body)

    def test_sign_script_mechanically_rejects_execute(self):
        body = (PACKAGING / "sign_artifact.sh").read_text(encoding="utf-8")
        self.assertIn('"${1:-}" == "--execute"', body)
        self.assertIn("not_available", body)
        self.assertIn("exit 2", body)
        self.assertNotIn("pipeline sign --execute", body)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_ci_has_test_build_and_upload(self):
        for token in (
            "unit-tests",
            "build-unsigned",
            "actions/upload-artifact",
            "pytest",
        ):
            self.assertIn(token, self.workflow)

    def test_ci_has_no_unaudited_production_signing_job(self):
        self.assertNotIn("sign-notarize", self.workflow)
        self.assertNotIn("sign_artifact.sh --execute", self.workflow)
        self.assertNotIn("JARVIS_MACOS_SIGN_IDENTITY", self.workflow)

    def test_ci_never_echoes_signing_secrets(self):
        for secret_env in (
            "JARVIS_MACOS_CERT_PASSWORD",
            "JARVIS_MACOS_NOTARY_PASSWORD",
            "JARVIS_MACOS_KEYCHAIN_PASSWORD",
        ):
            self.assertNotIn(f"echo ${secret_env}", self.workflow)
        # The unsigned workflow must not accept or stage signing credentials.
        self.assertNotIn("CERT_P12_BASE64", self.workflow)
        self.assertNotIn("notarytool store-credentials", self.workflow)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.product_updates import VerifiedStagedUpdate
from core.product_version import ProductVersion, SemanticVersion
from core.platform_adapters.release_base import (
    ReleaseBuildRequest,
    ReleaseCapabilityStatus,
    ReleasePackageFormat,
    UpdateInstallRequest,
    UnavailableReleaseAdapter,
)
from core.platform_adapters.release_factory import create_release_adapter
from core.platform_adapters.release_linux import LinuxReleaseAdapter
from core.platform_adapters.release_macos import MacOSReleaseAdapter
from core.platform_adapters.release_windows import WindowsReleaseAdapter
from core.update_transaction import open_verified_staged_artifact
from scripts.build_macos_release import _validate_product_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = ProductVersion.parse("1.2.2", 41)
TARGET = ProductVersion.parse("1.2.3", 42)


class ReleasePackagingTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        required_files = (
            project / "main.py",
            project / "core" / "prompt.txt",
            project / "config" / "settings.json",
            project / "packaging" / "macos" / "Jarvis.spec",
        )
        for path in required_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("safe", encoding="utf-8")
        (project / "dashboard" / "static").mkdir(parents=True)
        python = root / "runtime" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("runtime", encoding="utf-8")
        return project, python

    def _request(self, root: Path) -> ReleaseBuildRequest:
        project, python = self._project(root)
        return ReleaseBuildRequest.create(
            project_root=project,
            output_root=root / "output",
            version="1.2.3",
            build=42,
            architecture="aarch64",
            python_executable=python,
        )

    def test_macos_plan_is_argv_only_and_uses_app_paths_resource_root(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            adapter = MacOSReleaseAdapter(
                host_system="Darwin",
                which=lambda name: "/usr/bin/hdiutil" if name == "hdiutil" else None,
                module_available=lambda name: name == "PyInstaller",
            )

            plan = adapter.plan_build(request)

        self.assertEqual(plan.status, ReleaseCapabilityStatus.AVAILABLE)
        self.assertTrue(plan.executable)
        self.assertEqual(plan.package_format, ReleasePackageFormat.DMG)
        self.assertEqual(plan.resource_root, request.project_root)
        self.assertEqual(plan.app_path, plan.workspace_root / "dist" / "JARVIS.app")
        self.assertTrue(str(plan.package_path).endswith("macos-arm64.dmg"))
        self.assertEqual([command.name for command in plan.commands], ["build_app", "create_dmg"])
        self.assertTrue(all(isinstance(command.argv, tuple) for command in plan.commands))
        self.assertIn("PyInstaller", plan.commands[0].argv)
        self.assertIn("hdiutil", plan.commands[1].argv[0])

    def test_macos_plan_fails_honestly_when_host_or_tooling_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            plan = MacOSReleaseAdapter(
                host_system="Linux",
                which=lambda _name: None,
                module_available=lambda _name: False,
            ).plan_build(request)

        self.assertEqual(plan.status, ReleaseCapabilityStatus.NOT_AVAILABLE)
        self.assertFalse(plan.executable)
        self.assertEqual(plan.commands, ())
        self.assertIn("macOS build host", plan.missing_requirements)
        self.assertIn("PyInstaller module", plan.missing_requirements)
        self.assertIn("hdiutil", plan.missing_requirements)

    def test_macos_plan_rejects_architectures_without_a_macos_artifact_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, python = self._project(root)
            request = ReleaseBuildRequest.create(
                project_root=project,
                output_root=root / "output",
                version="1.2.3",
                build=42,
                architecture="armv7",
                python_executable=python,
            )
            plan = MacOSReleaseAdapter(
                host_system="Darwin",
                which=lambda _name: "/usr/bin/hdiutil",
                module_available=lambda _name: True,
            ).plan_build(request)

        self.assertEqual(plan.status, ReleaseCapabilityStatus.NOT_AVAILABLE)
        self.assertIn("supported macOS architecture", plan.missing_requirements)

    def test_windows_linux_and_unknown_are_explicitly_not_available(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            adapters = (
                WindowsReleaseAdapter(),
                LinuxReleaseAdapter(),
                UnavailableReleaseAdapter(),
            )
            plans = tuple(adapter.plan_build(request) for adapter in adapters)

        self.assertTrue(
            all(plan.status is ReleaseCapabilityStatus.NOT_AVAILABLE for plan in plans)
        )
        self.assertTrue(all(not plan.executable for plan in plans))
        self.assertEqual(plans[0].package_format, ReleasePackageFormat.WINDOWS_INSTALLER)
        self.assertEqual(plans[1].package_format, ReleasePackageFormat.LINUX_PACKAGE)

    def test_factory_routes_all_platforms_without_silent_macos_fallback(self):
        self.assertIsInstance(
            create_release_adapter("Darwin", host_platform="Darwin"),
            MacOSReleaseAdapter,
        )
        self.assertIsInstance(create_release_adapter("Windows"), WindowsReleaseAdapter)
        self.assertIsInstance(create_release_adapter("Linux"), LinuxReleaseAdapter)
        self.assertIsInstance(create_release_adapter("Plan9"), UnavailableReleaseAdapter)

    def test_update_contract_never_claims_unimplemented_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            content = b"verified release adapter artifact"
            path = root / "JARVIS.update"
            path.write_bytes(content)
            staged = VerifiedStagedUpdate(
                path,
                SOURCE,
                TARGET,
                hashlib.sha256(content).hexdigest(),
                len(content),
            )
            with open_verified_staged_artifact(staged) as artifact:
                self.assertIsNotNone(artifact)
                with self.assertRaises(ValueError):
                    UpdateInstallRequest.create(
                        platform="macos",
                        architecture="arm64",
                        staged_artifact=artifact,
                        installed_app="/Applications/JARVIS.app",
                        backup_root=root / "jarvis-backup",
                        expected_version="1.2.3",
                        expected_build=42,
                        expected_sha256="0" * 64,
                        expected_byte_size=staged.byte_size,
                    )
                request = UpdateInstallRequest.create(
                    platform="macos",
                    architecture="arm64",
                    staged_artifact=artifact,
                    installed_app="/Applications/JARVIS.app",
                    backup_root=root / "jarvis-backup",
                    expected_version="1.2.3",
                    expected_build=42,
                    expected_sha256=staged.sha256,
                    expected_byte_size=staged.byte_size,
                )
                adapters = (
                    MacOSReleaseAdapter(host_system="Darwin"),
                    WindowsReleaseAdapter(),
                    LinuxReleaseAdapter(),
                )

                results = tuple(
                    adapter.install_update(request) for adapter in adapters
                )

        self.assertTrue(
            all(result.status is ReleaseCapabilityStatus.NOT_AVAILABLE for result in results)
        )
        self.assertTrue(all(not result.verified for result in results))

    def test_request_validation_is_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, python = self._project(root)
            invalid = (
                {"version": "1.2", "build": 1, "architecture": "arm64"},
                {"version": "1.2.3", "build": 0, "architecture": "arm64"},
                {"version": "1.2.3", "build": 1, "architecture": "sparc"},
            )
            for values in invalid:
                with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                    ReleaseBuildRequest.create(
                        project_root=project,
                        output_root=root / "output",
                        python_executable=python,
                        **values,
                    )

    def test_direct_request_construction_cannot_bypass_path_or_digest_validation(self):
        with self.assertRaises(ValueError):
            ReleaseBuildRequest(
                project_root=Path("relative-project"),
                output_root=Path("relative-output"),
                version=SemanticVersion.parse("1.2.3"),
                build=1,
                architecture="arm64",
                python_executable=Path("python"),
            )

        with self.assertRaises((TypeError, ValueError)):
            UpdateInstallRequest(
                platform="macos",
                architecture="arm64",
                staged_artifact=Path("/tmp/JARVIS.update"),  # type: ignore[arg-type]
                installed_app=Path("/Applications/JARVIS.app"),
                backup_root=Path("/tmp/jarvis-backup"),
                expected_version=SemanticVersion.parse("1.2.3"),
                expected_build=1,
                expected_sha256="a" * 64,
                expected_byte_size=1,
            )

    def test_spec_includes_only_explicit_nonsecret_resources_and_bilingual_permissions(self):
        spec = (PROJECT_ROOT / "packaging" / "macos" / "Jarvis.spec").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "api_keys.json",
            "long_term.json",
            "briefing_sources.json",
            "local_env.zsh",
            "config/certs",
        ):
            self.assertNotIn(forbidden, spec)
        self.assertIn("briefing_sources.example.json", spec)
        self.assertIn("device_profile.example.json", spec)
        self.assertIn("dashboard/static", spec)
        self.assertIn("требуется", spec)

    def test_build_rejects_placeholder_product_trust_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            product_config = Path(temp).resolve() / "product.json"
            base = {
                "schema": "jarvis.product-client.v1",
                "api_base_url": "https://product.example.test",
                "allow_insecure_localhost": False,
                "entitlement_public_keys": {
                    "entitlement-test-key": "A" * 43,
                },
                "release_public_keys": {"release-test-key": "A" * 43},
            }
            product_config.write_text(json.dumps(base), encoding="utf-8")
            _validate_product_config(product_config)

            base["release_public_keys"]["release-test-key"] = "placeholder"
            product_config.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _validate_product_config(product_config)


if __name__ == "__main__":
    unittest.main()

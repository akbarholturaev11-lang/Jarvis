from __future__ import annotations

import argparse
import plistlib
import tempfile
import unittest
from pathlib import Path

import scripts.release_pipeline as pipeline
from core.platform_adapters.release_base import ReleaseBuildRequest
from core.product_version import BUNDLE_ID


VERSION = "1.0.0"
BUILD = 1
ARCH = "arm64"


def _args(output_root: Path, **overrides) -> argparse.Namespace:
    values = {
        "version": VERSION,
        "build": BUILD,
        "architecture": ARCH,
        "output_root": output_root,
        "product_config": None,
        "execute": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _workspace(output_root: Path) -> Path:
    return output_root.resolve() / f"{VERSION}+{BUILD}" / f"macos-{ARCH}"


def _fake_built_app(output_root: Path, *, version: str = VERSION) -> Path:
    workspace = _workspace(output_root)
    app = workspace / "dist" / "JARVIS.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "JARVIS").write_bytes(b"\xcf\xfa\xed\xfe")
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleShortVersionString": version,
                "CFBundleVersion": str(BUILD),
            }
        )
    )
    return app


class PipelineCommandTests(unittest.TestCase):
    def test_plan_returns_zero_and_reports_status(self):
        with tempfile.TemporaryDirectory() as temp:
            code = pipeline.main(
                [
                    "plan",
                    "--version",
                    VERSION,
                    "--build",
                    str(BUILD),
                    "--architecture",
                    ARCH,
                    "--output-root",
                    temp,
                ]
            )
        self.assertEqual(code, 0)

    def test_clean_removes_workspace_within_output_root(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            workspace = _workspace(output_root)
            (workspace / "dist").mkdir(parents=True)
            result = pipeline._cmd_clean(_args(output_root))
            self.assertTrue(result["removed"])
            self.assertFalse(workspace.exists())

    def test_verify_app_accepts_a_clean_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            app = _fake_built_app(output_root)
            result = pipeline._cmd_verify_app(_args(output_root))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["secret_files_in_bundle"], [])
        self.assertTrue(str(app) in result["app_path"])
        self.assertFalse(bool(result["secret_files_in_bundle"]))

    def test_verify_app_rejects_a_bundle_containing_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            app = _fake_built_app(output_root)
            (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
            (app / "Contents" / "Resources" / "api_keys.json").write_text("{}", "utf-8")
            with self.assertRaises(pipeline.PipelineError):
                pipeline._cmd_verify_app(_args(output_root))

    def test_verify_app_allows_public_ca_pem_but_rejects_private_key(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            app = _fake_built_app(output_root)
            certifi_dir = app / "Contents" / "Resources" / "certifi"
            certifi_dir.mkdir(parents=True)
            # Public CA trust store: legitimate, must NOT be flagged.
            (certifi_dir / "cacert.pem").write_text(
                "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                encoding="utf-8",
            )
            result = pipeline._cmd_verify_app(_args(output_root))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["secret_files_in_bundle"], [])
            # A real private key IS a secret and must be rejected.
            (app / "Contents" / "Resources" / "leaked.key").write_text(
                "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            with self.assertRaises(pipeline.PipelineError):
                pipeline._cmd_verify_app(_args(output_root))

    def test_verify_app_rejects_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            _fake_built_app(output_root, version="9.9.9")
            with self.assertRaises(pipeline.PipelineError):
                pipeline._cmd_verify_app(_args(output_root))

    def test_manifest_from_fake_dmg_writes_build_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            workspace = _workspace(output_root)
            workspace.mkdir(parents=True)
            dmg = workspace / f"JARVIS-{VERSION}-build{BUILD}-macos-{ARCH}.dmg"
            dmg.write_bytes(b"fake dmg bytes")
            result = pipeline._cmd_manifest(_args(output_root))
            manifest_path = workspace / "build_manifest.json"
            self.assertTrue(manifest_path.is_file())
        self.assertEqual(result["step"], "manifest")
        self.assertIs(result["distribution_ready"], False)
        self.assertEqual(result["byte_size"], len(b"fake dmg bytes"))

    def test_manifest_requires_a_built_dmg(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(pipeline.PipelineError):
                pipeline._cmd_manifest(_args(Path(temp)))

    def test_sign_without_credentials_is_honest_not_available(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            _fake_built_app(output_root)
            args = _args(output_root)
            # Ensure no ambient signing identity leaks in from the environment.
            import os

            for key in (
                "JARVIS_MACOS_SIGN_IDENTITY",
                "JARVIS_MACOS_NOTARY_PROFILE",
                "JARVIS_MACOS_TEAM_ID",
            ):
                os.environ.pop(key, None)
            result = pipeline._cmd_sign(args)
        self.assertEqual(result["status"], "not_available")
        self.assertTrue(result["unsigned_dev_build"])
        self.assertIs(result["distribution_ready"], False)
        self.assertEqual(result["codesign_commands"], [])

    def test_venv_python_symlink_is_not_followed_out_of_the_venv(self):
        # A venv's bin/python is a symlink to the base interpreter; resolving it
        # fully would run PyInstaller outside the build venv. The final component
        # must be preserved.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            base = root / "base" / "python3.12"
            base.parent.mkdir(parents=True)
            base.write_text("#!/bin/sh\n", encoding="utf-8")
            venv_bin = root / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            venv_python = venv_bin / "python"
            venv_python.symlink_to(base)  # mimic a venv symlink to the base
            request = ReleaseBuildRequest.create(
                project_root=root,
                output_root=root / "out",
                version=VERSION,
                build=BUILD,
                architecture=ARCH,
                python_executable=venv_python,
            )
        self.assertEqual(request.python_executable, venv_python)
        self.assertTrue(str(request.python_executable).endswith("venv/bin/python"))

    def test_smoke_reports_self_contained_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            output_root = Path(temp)
            _fake_built_app(output_root)
            result = pipeline._cmd_smoke(_args(output_root))
        self.assertEqual(result["status"], "self_contained")
        self.assertIs(result["requires_system_python"], False)
        self.assertIs(result["requires_terminal"], False)


if __name__ == "__main__":
    unittest.main()

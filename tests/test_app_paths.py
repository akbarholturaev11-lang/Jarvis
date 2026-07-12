from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from core.app_paths import resolve_app_paths


class AppPathsTests(unittest.TestCase):
    def test_development_layout_uses_project_root_without_creating_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "source" / "Jarvis"
            home = root / "not-created-home"
            module_file = project / "core" / "app_paths.py"

            paths = resolve_app_paths(
                platform_name="Darwin",
                home=home,
                environ={},
                source_file=module_file,
                frozen=False,
            )

            self.assertEqual(paths.resource_root, project.resolve())
            self.assertFalse(home.exists())
            self.assertFalse(paths.config_dir.exists())

    def test_macos_layout_uses_user_library_locations(self):
        home = Path("/Users/example")

        paths = resolve_app_paths(
            platform_name="macOS",
            home=home,
            environ={},
            resource_root="/Applications/JARVIS.app/Contents/Resources",
        )

        support = home / "Library" / "Application Support" / "JARVIS"
        self.assertEqual(paths.platform, "macos")
        self.assertEqual(paths.config_dir, support / "config")
        self.assertEqual(paths.data_dir, support / "data")
        self.assertEqual(paths.cache_dir, home / "Library" / "Caches" / "JARVIS")
        self.assertEqual(paths.log_dir, home / "Library" / "Logs" / "JARVIS")
        self.assertEqual(
            paths.update_staging_dir,
            paths.cache_dir / "updates" / "staging",
        )

    def test_windows_layout_honors_roaming_and_local_environment(self):
        home = Path("/home/test-user")
        roaming = Path("/mounted/roaming")
        local = Path("/mounted/local")

        paths = resolve_app_paths(
            platform_name="win32",
            home=home,
            environ={"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
            resource_root="/bundle/resources",
        )

        self.assertEqual(paths.platform, "windows")
        self.assertEqual(paths.config_dir, roaming / "JARVIS" / "config")
        self.assertEqual(paths.data_dir, local / "JARVIS" / "data")
        self.assertEqual(paths.cache_dir, local / "JARVIS" / "cache")
        self.assertEqual(paths.log_dir, local / "JARVIS" / "logs")

    def test_windows_layout_has_deterministic_environment_fallbacks(self):
        home = Path("/home/test-user")

        paths = resolve_app_paths(
            platform_name="Windows",
            home=home,
            environ={"APPDATA": "", "LOCALAPPDATA": "   "},
            resource_root="/bundle/resources",
        )

        self.assertEqual(
            paths.config_dir,
            home / "AppData" / "Roaming" / "JARVIS" / "config",
        )
        self.assertEqual(
            paths.data_dir,
            home / "AppData" / "Local" / "JARVIS" / "data",
        )

    def test_linux_layout_honors_all_xdg_locations(self):
        home = Path("/home/test-user")
        environment = {
            "XDG_CONFIG_HOME": "/xdg/config",
            "XDG_DATA_HOME": "/xdg/data",
            "XDG_CACHE_HOME": "/xdg/cache",
            "XDG_STATE_HOME": "/xdg/state",
        }

        paths = resolve_app_paths(
            platform_name="linux2",
            home=home,
            environ=environment,
            resource_root="/opt/jarvis/resources",
        )

        self.assertEqual(paths.platform, "linux")
        self.assertEqual(paths.config_dir, Path("/xdg/config/JARVIS"))
        self.assertEqual(paths.data_dir, Path("/xdg/data/JARVIS"))
        self.assertEqual(paths.cache_dir, Path("/xdg/cache/JARVIS"))
        self.assertEqual(paths.log_dir, Path("/xdg/state/JARVIS/logs"))

    def test_linux_layout_has_xdg_spec_fallbacks(self):
        home = Path("/home/test-user")

        paths = resolve_app_paths(
            platform_name="Linux",
            home=home,
            environ={},
            resource_root="/opt/jarvis/resources",
        )

        self.assertEqual(paths.config_dir, home / ".config" / "JARVIS")
        self.assertEqual(paths.data_dir, home / ".local" / "share" / "JARVIS")
        self.assertEqual(paths.cache_dir, home / ".cache" / "JARVIS")
        self.assertEqual(paths.log_dir, home / ".local" / "state" / "JARVIS" / "logs")

    def test_frozen_macos_app_uses_contents_resources(self):
        executable = "/Applications/JARVIS.app/Contents/MacOS/JARVIS"

        paths = resolve_app_paths(
            platform_name="Darwin",
            home="/Users/example",
            environ={},
            executable=executable,
            frozen=True,
        )

        self.assertEqual(
            paths.resource_root,
            Path("/Applications/JARVIS.app/Contents/Resources"),
        )

    def test_frozen_non_macos_bundle_accepts_packager_resource_root(self):
        paths = resolve_app_paths(
            platform_name="Windows",
            home="/home/example",
            environ={},
            executable="/bundle/JARVIS.exe",
            frozen=True,
            bundle_root="/bundle/_internal",
        )

        self.assertEqual(paths.resource_root, Path("/bundle/_internal"))

    def test_explicit_ensure_creates_only_writable_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            resources = root / "immutable-bundle"
            paths = resolve_app_paths(
                platform_name="Linux",
                home=home,
                environ={},
                resource_root=resources,
            )

            result = paths.ensure()

            self.assertIs(result, paths)
            self.assertFalse(resources.exists())
            for directory in paths.writable_directories:
                self.assertTrue(directory.is_dir())

    def test_path_layout_is_frozen(self):
        paths = resolve_app_paths(
            platform_name="Linux",
            home="/home/example",
            environ={},
            resource_root="/opt/jarvis/resources",
        )

        with self.assertRaises(FrozenInstanceError):
            paths.cache_dir = Path("/tmp/other")

    def test_unknown_platform_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve_app_paths(
                platform_name="Plan9",
                home="/home/example",
                environ={},
                resource_root="/resources",
            )


if __name__ == "__main__":
    unittest.main()

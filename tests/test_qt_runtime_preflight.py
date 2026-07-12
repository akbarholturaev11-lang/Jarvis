from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_qt_runtime import (
    build_runtime_paths,
    normalize_macos_hidden_flags,
    validate_qt_environment,
    validate_runtime_paths,
)


class QtRuntimePreflightTests(unittest.TestCase):
    def _fixture(self, platform: str = "darwin"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        physical_root = Path(temp_dir.name) / "Jarvis"
        expected_venv = physical_root / ".venv"
        pyqt_package = expected_venv / "lib/python3.12/site-packages/PyQt6"
        qt_prefix = pyqt_package / "Qt6"
        plugins_path = qt_prefix / "plugins"
        plugin_name = {
            "darwin": "libqcocoa.dylib",
            "win32": "qwindows.dll",
            "linux": "libqxcb.so",
        }[platform]
        plugin_file = plugins_path / "platforms" / plugin_name
        plugin_file.parent.mkdir(parents=True)
        plugin_file.touch()
        return physical_root, expected_venv, pyqt_package, qt_prefix, plugins_path

    def test_valid_runtime_paths_pass_for_supported_platforms(self):
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform):
                root, venv, pyqt, qt_prefix, plugins = self._fixture(platform)
                paths = build_runtime_paths(
                    project_root=root,
                    python_prefix=venv,
                    pyqt_package=pyqt,
                    qt_prefix=qt_prefix,
                    plugins_path=plugins,
                )
                self.assertEqual(validate_runtime_paths(paths, platform=platform), [])

    def test_symlink_project_alias_resolves_to_physical_venv(self):
        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        alias = root.parent / "Mark-XLVIII-AkbarCustom"
        try:
            alias.symlink_to(root, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        paths = build_runtime_paths(
            project_root=alias,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        self.assertEqual(validate_runtime_paths(paths, platform="darwin"), [])

    def test_moved_virtual_environment_is_rejected(self):
        root, _venv, pyqt, qt_prefix, plugins = self._fixture()
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=root.parent / "OldJarvis/.venv",
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        errors = validate_runtime_paths(paths, platform="darwin")
        self.assertTrue(any("does not match" in error for error in errors))

    def test_qt_outside_project_venv_is_rejected(self):
        root, venv, _pyqt, _qt_prefix, _plugins = self._fixture()
        outside = root.parent / "Other/.venv/site-packages/PyQt6"
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=outside,
            qt_prefix=outside / "Qt6",
            plugins_path=outside / "Qt6/plugins",
        )
        errors = validate_runtime_paths(paths, platform="darwin")
        self.assertTrue(any("outside" in error for error in errors))

    def test_missing_platform_plugin_is_rejected(self):
        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        (plugins / "platforms/libqcocoa.dylib").unlink()
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        errors = validate_runtime_paths(paths, platform="darwin")
        self.assertTrue(any("platform plugin is missing" in error for error in errors))

    def test_platform_plugin_symlink_is_rejected(self):
        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        plugin_file = plugins / "platforms/libqcocoa.dylib"
        plugin_file.unlink()
        outside = root.parent / "outside-libqcocoa.dylib"
        outside.touch()
        try:
            plugin_file.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        errors = validate_runtime_paths(paths, platform="darwin")
        self.assertTrue(any("must be a real file" in error for error in errors))

    def test_macos_hidden_plugin_is_rejected(self):
        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        hidden_plugin = (plugins / "platforms/libqcocoa.dylib").resolve()
        with patch(
            "scripts.check_qt_runtime._is_macos_hidden",
            side_effect=lambda path: path == hidden_plugin,
        ):
            errors = validate_runtime_paths(paths, platform="darwin")
        self.assertTrue(any("macOS hidden flag" in error for error in errors))

    def test_hidden_flag_normalization_is_a_noop_off_macos(self):
        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        self.assertEqual(
            normalize_macos_hidden_flags(paths, platform="linux"),
            0,
        )

    def test_qt_environment_overrides_are_rejected(self):
        for name in (
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QT_QPA_PLATFORM",
        ):
            with self.subTest(name=name):
                errors = validate_qt_environment({name: "offscreen"})
                self.assertTrue(any(name in error for error in errors))

        self.assertEqual(validate_qt_environment({}), [])

    def test_hidden_flag_normalization_refuses_external_qt_path(self):
        root, venv, _pyqt, _qt_prefix, _plugins = self._fixture()
        outside = root.parent / "Other/.venv/site-packages/PyQt6"
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=outside,
            qt_prefix=outside / "Qt6",
            plugins_path=outside / "Qt6/plugins",
        )
        with self.assertRaises(RuntimeError):
            normalize_macos_hidden_flags(paths, platform="darwin")

    @unittest.skipUnless(
        sys.platform == "darwin"
        and hasattr(os, "chflags")
        and hasattr(stat, "UF_HIDDEN"),
        "macOS file flags are required",
    )
    def test_hidden_flag_normalization_clears_real_macos_flag(self):
        from PyQt6.QtCore import QDir

        root, venv, pyqt, qt_prefix, plugins = self._fixture()
        hidden_plugin = plugins / "platforms/libqcocoa.dylib"
        hidden_directory = plugins / "position"
        hidden_child = hidden_directory / "libpositionplugin.dylib"
        hidden_directory.mkdir()
        hidden_child.touch()
        hidden_parent = pyqt.parent
        other_flag = getattr(stat, "UF_NODUMP", 0)
        os.chflags(
            hidden_plugin,
            hidden_plugin.stat().st_flags | stat.UF_HIDDEN | other_flag,
        )
        os.chflags(hidden_directory, hidden_directory.stat().st_flags | stat.UF_HIDDEN)
        os.chflags(hidden_child, hidden_child.stat().st_flags | stat.UF_HIDDEN)
        os.chflags(hidden_parent, hidden_parent.stat().st_flags | stat.UF_HIDDEN)
        paths = build_runtime_paths(
            project_root=root,
            python_prefix=venv,
            pyqt_package=pyqt,
            qt_prefix=qt_prefix,
            plugins_path=plugins,
        )
        self.assertNotIn(
            hidden_plugin.name,
            QDir(str(hidden_plugin.parent)).entryList(QDir.Filter.Files),
        )
        self.assertGreaterEqual(normalize_macos_hidden_flags(paths), 4)
        self.assertEqual(validate_runtime_paths(paths, platform="darwin"), [])
        self.assertIn(
            hidden_plugin.name,
            QDir(str(hidden_plugin.parent)).entryList(QDir.Filter.Files),
        )
        if other_flag:
            self.assertTrue(hidden_plugin.stat().st_flags & other_flag)
        self.assertFalse(hidden_parent.stat().st_flags & stat.UF_HIDDEN)


if __name__ == "__main__":
    unittest.main()

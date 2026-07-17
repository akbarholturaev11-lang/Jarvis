from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.autostart_manager as am
from core.platform_adapters.base import PlatformAdapter
from core.platform_adapters.linux import LinuxAdapter
from core.platform_adapters.macos import MacOSAdapter
from core.platform_adapters.windows import WindowsAdapter


class _FakeAdapter:
    def __init__(self, status_ret, set_ret):
        self._status_ret = status_ret
        self._set_ret = set_ret
        self.set_calls: list[tuple[bool, list[str]]] = []

    def autostart_status(self, label="com.jarvis.assistant"):
        return self._status_ret

    def set_autostart(self, enabled, command, label="com.jarvis.assistant"):
        self.set_calls.append((enabled, command))
        return self._set_ret


class TestAutostartFacade(unittest.TestCase):
    def test_status_routes_to_adapter(self):
        fake = _FakeAdapter((True, "on"), (True, "ok"))
        with mock.patch("core.autostart_manager.select_platform_adapter", return_value=fake):
            self.assertEqual(am.autostart_status(), (True, "on"))
            self.assertTrue(am.is_autostart_supported())

    def test_unsupported_status_is_honest(self):
        fake = _FakeAdapter((None, "unsupported"), (None, "unsupported"))
        with mock.patch("core.autostart_manager.select_platform_adapter", return_value=fake):
            state, _ = am.autostart_status()
            self.assertIsNone(state)
            self.assertFalse(am.is_autostart_supported())

    def test_set_passes_launch_command(self):
        fake = _FakeAdapter((False, "off"), (True, "on"))
        with mock.patch("core.autostart_manager.select_platform_adapter", return_value=fake):
            res = am.set_autostart(True)
            self.assertEqual(res, (True, "on"))
            self.assertEqual(len(fake.set_calls), 1)
            enabled, command = fake.set_calls[0]
            self.assertTrue(enabled)
            self.assertTrue(command)  # non-empty argv

    def test_status_exception_is_none(self):
        with mock.patch(
            "core.autostart_manager.select_platform_adapter",
            side_effect=RuntimeError("boom"),
        ):
            state, _ = am.autostart_status()
            self.assertIsNone(state)

    def test_set_exception_is_false(self):
        with mock.patch(
            "core.autostart_manager.select_platform_adapter",
            side_effect=RuntimeError("boom"),
        ):
            res, _ = am.set_autostart(True)
            self.assertFalse(res)


class TestBuildLaunchCommand(unittest.TestCase):
    def test_source_run_uses_interpreter_and_main(self):
        with mock.patch.object(am.sys, "frozen", False, create=True), \
             mock.patch.object(am.sys, "platform", "darwin"):
            cmd = am.build_launch_command()
            self.assertEqual(len(cmd), 2)
            self.assertTrue(cmd[1].endswith("main.py"))

    def test_frozen_run_uses_executable(self):
        with mock.patch.object(am.sys, "frozen", True, create=True), \
             mock.patch.object(am.sys, "executable", "/Apps/JARVIS"):
            self.assertEqual(am.build_launch_command(), ["/Apps/JARVIS"])


class TestBaseAdapterUnsupported(unittest.TestCase):
    def test_base_reports_unsupported_not_fake_success(self):
        adapter = PlatformAdapter()
        state, _ = adapter.autostart_status()
        self.assertIsNone(state)
        res, _ = adapter.set_autostart(True, ["python", "main.py"])
        self.assertIsNone(res)


class TestMacOSAdapter(unittest.TestCase):
    def test_enable_disable_roundtrip(self):
        adapter = MacOSAdapter()
        with tempfile.TemporaryDirectory() as d:
            plist = Path(d) / "com.jarvis.assistant.plist"
            # No-op _run so no real launchctl side effect during the test.
            with mock.patch.object(adapter, "_launch_agent_path", return_value=plist), \
                 mock.patch.object(adapter, "_run", return_value=None):
                self.assertIs(adapter.autostart_status()[0], False)
                res, _ = adapter.set_autostart(True, ["/py", "/proj/main.py"])
                self.assertIs(res, True)
                self.assertTrue(plist.exists())
                content = plist.read_text()
                self.assertIn("RunAtLoad", content)
                self.assertIn("/proj/main.py", content)
                self.assertIs(adapter.autostart_status()[0], True)
                res, _ = adapter.set_autostart(False, [])
                self.assertIs(res, True)
                self.assertFalse(plist.exists())

    def test_enable_without_command_fails(self):
        adapter = MacOSAdapter()
        with tempfile.TemporaryDirectory() as d:
            plist = Path(d) / "x.plist"
            with mock.patch.object(adapter, "_launch_agent_path", return_value=plist), \
                 mock.patch.object(adapter, "_run", return_value=None):
                res, _ = adapter.set_autostart(True, [])
                self.assertIs(res, False)
                self.assertFalse(plist.exists())


class TestLinuxAdapter(unittest.TestCase):
    def test_enable_disable_roundtrip(self):
        adapter = LinuxAdapter()
        with tempfile.TemporaryDirectory() as d:
            entry = Path(d) / "com.jarvis.assistant.desktop"
            with mock.patch.object(adapter, "_autostart_entry_path", return_value=entry):
                self.assertIs(adapter.autostart_status()[0], False)
                res, _ = adapter.set_autostart(True, ["/py", "/proj/main.py"])
                self.assertIs(res, True)
                self.assertTrue(entry.exists())
                self.assertIn("X-GNOME-Autostart-enabled=true", entry.read_text())
                self.assertIs(adapter.autostart_status()[0], True)
                res, _ = adapter.set_autostart(False, [])
                self.assertIs(res, True)
                self.assertFalse(entry.exists())


class TestWindowsAdapterOffPlatform(unittest.TestCase):
    @unittest.skipIf(sys.platform.startswith("win"), "winreg is available on Windows")
    def test_no_winreg_is_honest(self):
        adapter = WindowsAdapter()
        # winreg import fails off-Windows → honest unknown/failed, never fake success.
        state, _ = adapter.autostart_status()
        self.assertIsNone(state)
        res, _ = adapter.set_autostart(True, ["py", "main.py"])
        self.assertIs(res, False)


if __name__ == "__main__":
    unittest.main()

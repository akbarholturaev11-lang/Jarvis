from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.app_settings as aps


class TestAssistantConfig(unittest.TestCase):
    def _tmp_settings(self) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name) / "settings.json"

    def test_defaults_when_unset(self):
        with mock.patch.object(aps, "SETTINGS_FILE", self._tmp_settings()):
            cfg = aps.get_assistant_config()
            self.assertEqual(cfg["assistant_name"], "Jarvis")
            self.assertEqual(cfg["user_name"], "")
            self.assertTrue(aps.get_clipboard_actions_enabled())

    def test_save_and_read_back_trims_whitespace(self):
        with mock.patch.object(aps, "SETTINGS_FILE", self._tmp_settings()):
            aps.save_assistant_config("  Vision  ", "  Akbar ")
            cfg = aps.get_assistant_config()
            self.assertEqual(cfg["assistant_name"], "Vision")
            self.assertEqual(cfg["user_name"], "Akbar")

    def test_blank_name_falls_back_to_default(self):
        with mock.patch.object(aps, "SETTINGS_FILE", self._tmp_settings()):
            aps.save_assistant_config("   ", "")
            self.assertEqual(aps.get_assistant_config()["assistant_name"], "Jarvis")

    def test_toggle_preserves_unrelated_keys(self):
        with mock.patch.object(aps, "SETTINGS_FILE", self._tmp_settings()):
            aps.save_assistant_config("Vision", "Akbar")
            aps.set_clipboard_actions_enabled(False)
            self.assertFalse(aps.get_clipboard_actions_enabled())
            # The unrelated clipboard write must not clobber the assistant config.
            self.assertEqual(aps.get_assistant_config()["assistant_name"], "Vision")
            self.assertEqual(aps.get_assistant_config()["user_name"], "Akbar")


if __name__ == "__main__":
    unittest.main()

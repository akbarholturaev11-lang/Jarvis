from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import core.app_settings as app_settings
import core.macros as macros
from core.capabilities import (
    build_phrase,
    compose_macro,
    get_capability,
    list_capabilities,
)


class TestCapabilities(unittest.TestCase):
    def test_list_has_both_languages(self):
        en = list_capabilities("en")
        ru = list_capabilities("ru")
        self.assertTrue(en and ru)
        self.assertEqual(len(en), len(ru))
        self.assertIn("briefing", {c["id"] for c in en})
        # ru labels differ from en for at least one item
        self.assertNotEqual(en[0]["label"], ru[0]["label"])

    def test_compose_macro_joins_phrases(self):
        s = compose_macro([{"id": "screen_look"}, {"id": "media_pause"}])
        self.assertEqual(s, "ekranimga qara, va musiqani to'xtat")

    def test_build_phrase_with_value(self):
        self.assertEqual(build_phrase("open_app", "Telegram"), "Telegram ni och")

    def test_unknown_capability(self):
        self.assertEqual(build_phrase("nope"), "")
        self.assertIsNone(get_capability("nope"))

    def test_needs_input_flag(self):
        caps = {c["id"]: c for c in list_capabilities("en")}
        self.assertTrue(caps["open_app"]["needs_input"])
        self.assertFalse(caps["briefing"]["needs_input"])


class TestMacros(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = macros.MACROS_FILE
        macros.MACROS_FILE = Path(self._tmp.name) / "macros.json"

    def tearDown(self):
        macros.MACROS_FILE = self._orig
        self._tmp.cleanup()

    def test_add_load_remove(self):
        macros.add_macro("Ish rejimi", [{"id": "screen_look"}, {"id": "media_pause"}])
        loaded = macros.load_macros()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Ish rejimi")
        self.assertTrue(loaded[0]["phrase"])
        macros.remove_macro(loaded[0]["id"])
        self.assertEqual(macros.load_macros(), [])

    def test_set_macros_skips_unnamed(self):
        out = macros.set_macros([
            {"name": "", "steps": []},
            {"name": "Uy", "steps": [{"id": "home"}]},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Uy")

    def test_corrupt_file_is_tolerated(self):
        macros.MACROS_FILE.write_text("{ not json", encoding="utf-8")
        self.assertEqual(macros.load_macros(), [])


class TestAppSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = app_settings.SETTINGS_FILE
        app_settings.SETTINGS_FILE = Path(self._tmp.name) / "settings.json"

    def tearDown(self):
        app_settings.SETTINGS_FILE = self._orig
        self._tmp.cleanup()

    def test_tunnel_defaults(self):
        cfg = app_settings.get_tunnel_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode"], "quick")
        self.assertEqual(cfg["provider"], "cloudflare")

    def test_set_tunnel_enabled_preserves_other_keys(self):
        app_settings.update_settings({"ui_language": "en"})
        app_settings.set_tunnel_enabled(True)
        data = json.loads(app_settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["ui_language"], "en")           # not clobbered
        self.assertTrue(data["remote_tunnel"]["enabled"])

    def test_keep_awake_default_and_toggle(self):
        self.assertTrue(app_settings.get_keep_awake_enabled())  # default on
        app_settings.set_keep_awake_enabled(False)
        self.assertFalse(app_settings.get_keep_awake_enabled())


if __name__ == "__main__":
    unittest.main()

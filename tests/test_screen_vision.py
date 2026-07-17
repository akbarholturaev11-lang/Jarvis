"""screen_find / screen_click must not mask a model 404 as "element not found"."""

import unittest
from unittest import mock

from actions import computer_control as cc


class FakeAPIError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _fake_pyautogui():
    m = mock.MagicMock()
    m.size.return_value = (1920, 1080)
    shot = mock.MagicMock()
    shot.save.side_effect = lambda buf, format=None: buf.write(b"PNGDATA")
    m.screenshot.return_value = shot
    return m


class ScreenFindTests(unittest.TestCase):
    def _patched(self, client):
        return [
            mock.patch.object(cc, "_get_api_key", return_value="k"),
            mock.patch.object(cc, "pyautogui", _fake_pyautogui()),
            mock.patch.object(cc, "_require_pyautogui", lambda: None),
            mock.patch("google.genai.Client", return_value=client),
        ]

    def test_valid_model_response_returns_coords(self):
        resp = mock.MagicMock(); resp.text = "100,200"
        client = mock.MagicMock()
        client.models.generate_content.return_value = resp
        patches = self._patched(client)
        for p in patches:
            p.start()
        try:
            self.assertEqual(cc._screen_find("the button"), (100, 200))
        finally:
            for p in patches:
                p.stop()

    def test_model_404_raises_screen_vision_error(self):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = FakeAPIError(
            404, "models/gemini-2.5-flash-lite is no longer available to new users"
        )
        patches = self._patched(client)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(cc.ScreenVisionError):
                cc._screen_find("the button")
        finally:
            for p in patches:
                p.stop()

    def test_actual_not_found_returns_none(self):
        resp = mock.MagicMock(); resp.text = "NOT_FOUND"
        client = mock.MagicMock()
        client.models.generate_content.return_value = resp
        patches = self._patched(client)
        for p in patches:
            p.start()
        try:
            self.assertIsNone(cc._screen_find("the button"))
        finally:
            for p in patches:
                p.stop()


class ScreenDispatchTests(unittest.TestCase):
    def test_config_error_not_masked_as_not_found(self):
        with mock.patch.object(cc, "_screen_find", side_effect=cc.ScreenVisionError("model x unavailable")):
            result = cc.computer_control({"action": "screen_find", "description": "btn"})
        self.assertNotEqual(result.strip(), "NOT_FOUND")
        self.assertIn("unavailable", result.lower())

    def test_screen_click_config_error_message(self):
        with mock.patch.object(cc, "_screen_find", side_effect=cc.ScreenVisionError("boom")):
            result = cc.computer_control({"action": "screen_click", "description": "btn"})
        self.assertIn("configuration error", result.lower())
        self.assertNotIn("element not found", result.lower())

    def test_screen_click_actual_not_found(self):
        with mock.patch.object(cc, "_screen_find", return_value=None):
            result = cc.computer_control({"action": "screen_click", "description": "btn"})
        self.assertIn("not found", result.lower())


if __name__ == "__main__":
    unittest.main()

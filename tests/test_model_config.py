"""Central model-config resolution + model-unavailable classification."""

import os
import unittest
from unittest import mock

from core.model_config import (
    intent_model,
    is_model_unavailable_error,
    vision_model,
)


class FakeAPIError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class ModelUnavailableClassificationTests(unittest.TestCase):
    def test_404_code_is_unavailable(self):
        self.assertTrue(is_model_unavailable_error(FakeAPIError(404, "boom")))

    def test_not_found_text_is_unavailable(self):
        self.assertTrue(is_model_unavailable_error(
            Exception("models/gemini-2.5-flash-lite is no longer available to new users")
        ))

    def test_not_found_status_is_unavailable(self):
        self.assertTrue(is_model_unavailable_error(Exception("404 NOT_FOUND")))

    def test_normal_error_is_not_unavailable(self):
        self.assertFalse(is_model_unavailable_error(ValueError("bad content format")))
        self.assertFalse(is_model_unavailable_error(FakeAPIError(500, "server error")))


class ModelOverrideTests(unittest.TestCase):
    def test_vision_model_env_override(self):
        with mock.patch.dict(os.environ, {"JARVIS_VISION_MODEL": "gemini-custom-vision"}):
            self.assertEqual(vision_model(), "gemini-custom-vision")

    def test_intent_model_env_override(self):
        with mock.patch.dict(os.environ, {"JARVIS_INTENT_MODEL": "gemini-custom-intent"}):
            self.assertEqual(intent_model(), "gemini-custom-intent")

    def test_vision_model_settings_override(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("core.app_settings.load_settings", return_value={"vision_model": "gemini-cfg"}):
            os.environ.pop("JARVIS_VISION_MODEL", None)
            self.assertEqual(vision_model(), "gemini-cfg")

    def test_vision_model_default(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("core.app_settings.load_settings", return_value={}):
            os.environ.pop("JARVIS_VISION_MODEL", None)
            self.assertEqual(vision_model(), "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()

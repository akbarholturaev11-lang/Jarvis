"""Honest verified-reporting tests for macOS system control.

These cover the fix for the "fake success" bug: system-control commands must
not claim success when they were not verified, and must surface the macOS
Accessibility / Automation permission requirement clearly.

Three distinct outcomes are asserted through core.session_context.infer_result_status:
  - verified success   -> ("success", True)
  - requested, unverified -> ("uncertain", False)
  - failed             -> ("failed", False)
"""

import subprocess
import unittest
from unittest import mock

from actions import computer_settings as cs
from actions import media_control as mc
from core.session_context import infer_result_status


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_fake_run(script_map, default=None):
    """Build a subprocess.run replacement routed by the osascript -e script.

    Each value may be a FakeCompleted, an Exception instance (raised), or a
    zero-arg callable returning a FakeCompleted (for changing values per call).
    Non-osascript commands fall through to `default`.
    """
    def _run(cmd, *args, **kwargs):
        script = ""
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and str(cmd[0]).endswith("osascript"):
            script = str(cmd[2])
        elif isinstance(cmd, (list, tuple)):
            script = " ".join(str(c) for c in cmd)
        for key, val in script_map.items():
            if key in script:
                if isinstance(val, BaseException):
                    raise val
                if callable(val):
                    return val()
                return val
        if default is not None:
            return default
        return FakeCompleted(0, "", "")
    return _run


class VolumeVerificationTests(unittest.TestCase):
    def setUp(self):
        cs._accessibility_cache.update({"checked": False, "value": None})

    def _run_action(self, action, value=None, script_map=None):
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs.subprocess, "run", make_fake_run(script_map or {})):
            return cs.computer_settings(parameters={"action": action, "value": value})

    def test_volume_up_verified_success(self):
        vols = iter(["30", "40"])
        result = self._run_action("volume_up", script_map={
            "set volume output volume": FakeCompleted(0),
            "output volume of": lambda: FakeCompleted(0, next(vols)),
        })
        self.assertIn("verified", result.lower())
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))

    def test_volume_set_verified_success(self):
        result = self._run_action("volume_set", value=20, script_map={
            "set volume output volume": FakeCompleted(0),
            "output volume of": lambda: FakeCompleted(0, "20"),
        })
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))

    def test_volume_unchanged_is_unverified_not_success(self):
        result = self._run_action("volume_up", script_map={
            "set volume output volume": FakeCompleted(0),
            "output volume of": lambda: FakeCompleted(0, "50"),
        })
        self.assertIn("requested, unverified", result.lower())
        status, verified = infer_result_status("computer_settings", result)
        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)

    def test_volume_command_nonzero_returncode_fails(self):
        result = self._run_action("volume_set", value=30, script_map={
            "set volume output volume": FakeCompleted(1, "", "some osascript error"),
            "output volume of": lambda: FakeCompleted(0, "50"),
        })
        self.assertIn("could not", result.lower())
        self.assertEqual(infer_result_status("computer_settings", result), ("failed", False))

    def test_volume_command_timeout_fails(self):
        result = self._run_action("volume_up", script_map={
            "set volume output volume": subprocess.TimeoutExpired(cmd="osascript", timeout=3),
            "output volume of": lambda: FakeCompleted(0, "50"),
        })
        self.assertEqual(infer_result_status("computer_settings", result), ("failed", False))

    def test_volume_unreadable_is_unverified(self):
        # output volume cannot be parsed -> cannot confirm -> not success
        result = self._run_action("volume_up", script_map={
            "set volume output volume": FakeCompleted(0),
            "output volume of": FakeCompleted(0, "not-a-number"),
        })
        status, verified = infer_result_status("computer_settings", result)
        self.assertFalse(verified)
        self.assertNotEqual(status, "success")


class BrightnessTests(unittest.TestCase):
    def setUp(self):
        cs._accessibility_cache.update({"checked": False, "value": None})

    def _run(self, action, script_map):
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs.subprocess, "run", make_fake_run(script_map)):
            return cs.computer_settings(parameters={"action": action})

    def test_brightness_permission_denied_is_failed_with_hint(self):
        result = self._run("brightness_up", {
            "key code 144": FakeCompleted(1, "", "execution error: not allowed assistive access (-25211)"),
        })
        self.assertIn("accessibility", result.lower())
        self.assertEqual(infer_result_status("computer_settings", result), ("failed", False))

    def test_brightness_sent_is_unverified_not_success(self):
        result = self._run("brightness_down", {
            "key code 145": FakeCompleted(0),
        })
        self.assertIn("requested, unverified", result.lower())
        status, verified = infer_result_status("computer_settings", result)
        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)


class AccessibilityGateTests(unittest.TestCase):
    def setUp(self):
        cs._accessibility_cache.update({"checked": False, "value": None})

    def test_denied_accessibility_blocks_pyautogui_action(self):
        fake_pyautogui = mock.MagicMock()
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs, "pyautogui", fake_pyautogui), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({
                 "get name of first process": FakeCompleted(1, "", "not allowed assistive access (-25211)"),
             })):
            result = cs.computer_settings(parameters={"action": "copy"})
        self.assertIn("accessibility", result.lower())
        self.assertEqual(infer_result_status("computer_settings", result), ("failed", False))
        fake_pyautogui.hotkey.assert_not_called()

    def test_granted_accessibility_reports_done(self):
        fake_pyautogui = mock.MagicMock()
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs, "pyautogui", fake_pyautogui), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({
                 "get name of first process": FakeCompleted(0, "Finder"),
             })):
            result = cs.computer_settings(parameters={"action": "copy"})
        self.assertTrue(result.lower().startswith("done:"))
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))
        fake_pyautogui.hotkey.assert_called()


class CrossPlatformTests(unittest.TestCase):
    """Existing Windows/Linux behavior must not break (no macOS verification gate)."""

    def setUp(self):
        cs._accessibility_cache.update({"checked": False, "value": None})

    def test_windows_volume_set_still_returns_success(self):
        fake_pyautogui = mock.MagicMock()
        with mock.patch.object(cs, "_OS", "Windows"), \
             mock.patch.object(cs, "pyautogui", fake_pyautogui), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({})):
            result = cs.computer_settings(parameters={"action": "volume_set", "value": 30})
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))

    def test_linux_volume_up_still_returns_done(self):
        with mock.patch.object(cs, "_OS", "Linux"), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({})):
            result = cs.computer_settings(parameters={"action": "volume_up"})
        self.assertTrue(result.lower().startswith("done:"))
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))


class MediaControlPermissionTests(unittest.TestCase):
    def test_media_pause_accessibility_denied_is_failed(self):
        with mock.patch.object(mc, "_OS", "Darwin"), \
             mock.patch.object(mc.subprocess, "run", make_fake_run({
                 "key code 16": FakeCompleted(1, "", "not allowed assistive access (-25211)"),
             })):
            result = mc.media_control(parameters={"action": "pause"})
        self.assertIn("accessibility", result.lower())
        self.assertEqual(infer_result_status("media_control", result), ("failed", False))

    def test_media_pause_sent_but_unverified(self):
        with mock.patch.object(mc, "_OS", "Darwin"), \
             mock.patch.object(mc.subprocess, "run", make_fake_run({
                 "key code 16": FakeCompleted(0),
             })):
            result = mc.media_control(parameters={"action": "pause"})
        status, verified = infer_result_status("media_control", result)
        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)


class ActionNormalizationTests(unittest.TestCase):
    def test_increase_volume_maps_to_volume_up(self):
        self.assertEqual(cs._canonicalize_action("increase", "volume", None), "volume_up")

    def test_decrease_volume_maps_to_volume_down(self):
        self.assertEqual(cs._canonicalize_action("decrease", "volume", None), "volume_down")

    def test_increase_brightness_maps_to_brightness_up(self):
        self.assertEqual(cs._canonicalize_action("increase", "brightness", None), "brightness_up")

    def test_decrease_brightness_maps_to_brightness_down(self):
        self.assertEqual(cs._canonicalize_action("decrease", "brightness", None), "brightness_down")

    def test_mute_audio_maps_to_mute(self):
        self.assertEqual(cs._canonicalize_action("mute", "audio", None), "mute")
        self.assertEqual(cs._canonicalize_action("unmute", "volume", None), "unmute")

    def test_ambiguous_direction_returns_none(self):
        self.assertIsNone(cs._canonicalize_action("increase", "", None))
        self.assertIsNone(cs._canonicalize_action("decrease", "make it nicer", None))

    def test_canonical_action_passes_through(self):
        self.assertEqual(cs._canonicalize_action("volume_up", "", None), "volume_up")
        self.assertEqual(cs._canonicalize_action("copy", "", None), "copy")

    def test_end_to_end_increase_volume_is_verified(self):
        vols = iter(["30", "40"])
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({
                 "set volume output volume": FakeCompleted(0),
                 "output volume of": lambda: FakeCompleted(0, next(vols)),
             })):
            result = cs.computer_settings(parameters={"action": "increase", "description": "volume"})
        self.assertEqual(infer_result_status("computer_settings", result), ("success", True))

    def test_end_to_end_ambiguous_increase_is_failed(self):
        with mock.patch.object(cs, "_OS", "Darwin"), \
             mock.patch.object(cs.subprocess, "run", make_fake_run({})):
            result = cs.computer_settings(parameters={"action": "increase"})
        self.assertIn("please specify", result.lower())
        self.assertEqual(infer_result_status("computer_settings", result), ("failed", False))


class InferStatusVocabularyTests(unittest.TestCase):
    """The three states must be distinguishable by the shared classifier."""

    def test_verified_volume_is_success(self):
        self.assertEqual(
            infer_result_status("computer_settings", "Volume set to 40% (verified: 30% -> 40%)."),
            ("success", True),
        )

    def test_requested_unverified_is_uncertain(self):
        self.assertEqual(
            infer_result_status(
                "computer_settings",
                "Brightness change requested, unverified: the key was sent but macOS "
                "does not expose a reliable brightness value to confirm the change.",
            ),
            ("uncertain", False),
        )

    def test_permission_message_is_failed(self):
        status, verified = infer_result_status(
            "computer_settings",
            "Could not change brightness: macOS Accessibility permission is required. "
            "Enable it for Terminal in System Settings > Privacy & Security > Accessibility.",
        )
        self.assertEqual(status, "failed")
        self.assertFalse(verified)


if __name__ == "__main__":
    unittest.main()

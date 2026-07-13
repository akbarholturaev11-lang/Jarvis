import unittest
from unittest import mock

import actions.app_control as app_control
from core.platform_adapters.base import PlatformAdapter
from core.platform_adapters.linux import LinuxAdapter
from core.platform_adapters.macos import MacOSAdapter
from core.platform_adapters.windows import WindowsAdapter
from core.session_context import infer_result_status


class _FakeAdapter:
    def __init__(self, ret):
        self._ret = ret
        self.calls = []

    def close_app(self, app_name):
        self.calls.append(app_name)
        return self._ret


class CloseAppActionTests(unittest.TestCase):
    def _run_with(self, ret, app="Telegram"):
        fake = _FakeAdapter(ret)
        with mock.patch("actions.app_control.select_platform_adapter", return_value=fake):
            result = app_control.close_app({"app_name": app})
        return result, fake

    def test_empty_app_name_is_failed(self):
        result = app_control.close_app({"app_name": ""})
        self.assertEqual(result, "No application name provided.")
        status, verified = infer_result_status("close_app", result)
        self.assertEqual(status, "failed")
        self.assertFalse(verified)

    def test_verified_close_reports_success(self):
        result, _ = self._run_with((True, "Telegram quit and verified closed."))
        status, verified = infer_result_status("close_app", result)
        self.assertEqual(status, "success")
        self.assertTrue(verified)

    def test_unverified_close_is_uncertain(self):
        result, _ = self._run_with(
            (None, "Quit request sent to Telegram; running state could not be verified.")
        )
        status, verified = infer_result_status("close_app", result)
        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)
        self.assertNotIn("verified closed", result.lower())

    def test_failed_close_is_failed(self):
        result, _ = self._run_with((False, "osascript quit failed"))
        self.assertTrue(result.lower().startswith("could not close"))
        status, verified = infer_result_status("close_app", result)
        self.assertEqual(status, "failed")
        self.assertFalse(verified)


class CloseAppAdapterParityTests(unittest.TestCase):
    """The semantic contract (reject empty target, honest unsupported) must be
    identical across macOS / Windows / Linux, even though the OS command differs."""

    def test_all_adapters_reject_empty_target_identically(self):
        for adapter in (MacOSAdapter(), WindowsAdapter(), LinuxAdapter()):
            ok, detail = adapter.close_app("")
            self.assertIs(ok, False)
            self.assertIn("no application name", detail.lower())

    def test_base_adapter_is_honest_unsupported(self):
        ok, detail = PlatformAdapter().close_app("Telegram")
        self.assertIs(ok, False)
        self.assertIn("unsupported", detail.lower())

    def test_close_app_signature_shape_is_tuple(self):
        # Each adapter must return (ok, detail); ok in {True, False, None}.
        for adapter in (MacOSAdapter(), WindowsAdapter(), LinuxAdapter(), PlatformAdapter()):
            ok, detail = adapter.close_app("")
            self.assertIn(ok, (True, False, None))
            self.assertIsInstance(detail, str)


if __name__ == "__main__":
    unittest.main()

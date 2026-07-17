from __future__ import annotations

import unittest
from unittest import mock

import core.permissions_manager as pm
from core.platform_adapters.base import PlatformAdapter
from core.platform_adapters.macos import MacOSAdapter


class _FakeAdapter:
    def __init__(self, statuses):
        self._statuses = statuses
        self.requested: list[str] = []

    def permission_status(self, name):
        return self._statuses.get(name, "unknown")

    def request_permission(self, name):
        self.requested.append(name)
        return True, "opened"

    def open_permission_pane(self, name):
        return True, "opened"


class TestPermissionsManager(unittest.TestCase):
    def test_all_statuses_routes_to_adapter(self):
        fake = _FakeAdapter({"accessibility": "granted", "automation": "denied"})
        with mock.patch("core.permissions_manager.select_platform_adapter", return_value=fake):
            st = pm.all_statuses()
            self.assertEqual(st["accessibility"], "granted")
            self.assertEqual(st["automation"], "denied")
            self.assertEqual(st["camera"], "unknown")  # unmapped → unknown

    def test_blocking_only_denied(self):
        st = {
            "accessibility": "granted",
            "automation": "denied",
            "screen_recording": "unknown",
            "microphone": "not_required",
            "camera": "granted",
        }
        self.assertEqual(pm.blocking_permissions(st), ["automation"])

    def test_any_actionable(self):
        self.assertTrue(pm.any_actionable({"a": "denied"}))
        self.assertTrue(pm.any_actionable({"a": "unknown"}))
        self.assertFalse(pm.any_actionable({"a": "granted", "b": "not_required"}))

    def test_request_permission_routes(self):
        fake = _FakeAdapter({})
        with mock.patch("core.permissions_manager.select_platform_adapter", return_value=fake):
            ok, _ = pm.request_permission("screen_recording")
            self.assertTrue(ok)
            self.assertIn("screen_recording", fake.requested)

    def test_adapter_exception_is_honest(self):
        with mock.patch(
            "core.permissions_manager.select_platform_adapter",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(pm.permission_status("accessibility"), "unknown")
            self.assertEqual(pm.all_statuses()["camera"], "unknown")
            ok, _ = pm.request_permission("camera")
            self.assertFalse(ok)


class TestBaseAdapterPermissions(unittest.TestCase):
    def test_base_reports_not_required(self):
        adapter = PlatformAdapter()
        self.assertEqual(adapter.permission_status("accessibility"), "not_required")
        result, _ = adapter.request_permission("accessibility")
        self.assertIsNone(result)
        ok, _ = adapter.open_permission_pane("accessibility")
        self.assertFalse(ok)


class TestMacOSAdapterPermissions(unittest.TestCase):
    def test_open_pane_builds_correct_url(self):
        adapter = MacOSAdapter()
        calls = []

        def fake_run(cmd, timeout=2.0):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(adapter, "_run", side_effect=fake_run):
            ok, _ = adapter.open_permission_pane("screen_recording")
            self.assertTrue(ok)
            self.assertEqual(calls[0][0], "open")
            self.assertIn("Privacy_ScreenCapture", calls[0][1])

    def test_unmapped_pane_is_honest(self):
        adapter = MacOSAdapter()
        ok, _ = adapter.open_permission_pane("nonsense")
        self.assertFalse(ok)

    def test_accessibility_probe_maps_returncode(self):
        adapter = MacOSAdapter()
        with mock.patch.object(adapter, "_run", return_value=mock.Mock(returncode=0, stdout="Dock", stderr="")):
            self.assertEqual(adapter.permission_status("accessibility"), "granted")
        with mock.patch.object(adapter, "_run", return_value=mock.Mock(returncode=1, stdout="", stderr="error -25211 assistive access")):
            self.assertEqual(adapter.permission_status("accessibility"), "denied")
        with mock.patch.object(adapter, "_run", return_value=mock.Mock(returncode=1, stdout="", stderr="some other error")):
            self.assertEqual(adapter.permission_status("accessibility"), "unknown")

    def test_mic_camera_map_real_avfoundation_status(self):
        import core.platform_adapters.macos as macos

        adapter = MacOSAdapter()

        class _FakeAV:
            def __init__(self, val):
                self.val = val

            def authorizationStatusForMediaType_(self, media_type):
                return self.val

        # AVAuthorizationStatus → our vocabulary.
        for raw, expected in [(3, "granted"), (2, "denied"), (1, "denied"), (0, "unknown")]:
            with mock.patch.dict(
                macos._AVCAPTURE_CACHE, {"tried": True, "cls": _FakeAV(raw)}, clear=True
            ):
                self.assertEqual(adapter.permission_status("microphone"), expected)
                self.assertEqual(adapter.permission_status("camera"), expected)

    def test_mic_camera_unknown_when_avfoundation_unavailable(self):
        import core.platform_adapters.macos as macos

        adapter = MacOSAdapter()
        with mock.patch.dict(
            macos._AVCAPTURE_CACHE, {"tried": True, "cls": None}, clear=True
        ):
            self.assertEqual(adapter.permission_status("microphone"), "unknown")
            self.assertEqual(adapter.permission_status("camera"), "unknown")


if __name__ == "__main__":
    unittest.main()

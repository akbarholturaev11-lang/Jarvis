from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.device_profile import (
    check_permission_gate,
    default_profile,
    ensure_device_profile,
    find_privacy_violations,
    installed_browser_ids,
    resolve_browser_route,
    resolve_media_route,
    resolve_messaging_route,
    scrub_profile,
    is_device_profile_refresh_request,
    validate_device_profile,
)
from core.environment_discovery import detect_platform_key
from core.session_context import SessionContext


def _profile(
    browsers: list[str] | None = None,
    messaging: list[str] | None = None,
    default_browser: str = "unknown",
) -> dict:
    profile = default_profile(Path.cwd())
    profile["platform"]["os"] = "macos"
    profile["capabilities"]["app_launch"] = {
        "supported": True,
        "method": "open -a",
        "verified": False,
    }
    profile["capabilities"]["browser_control"] = {
        "supported": True,
        "default_browser": default_browser,
        "preferred_browser": "",
        "installed_browsers": browsers or [],
    }
    profile["apps"]["browsers"] = [
        {
            "id": browser,
            "name": browser,
            "launch_name": browser,
            "detected": True,
            "source": "test",
        }
        for browser in (browsers or [])
    ]
    profile["apps"]["messaging"] = [
        {
            "id": app,
            "name": app,
            "launch_name": app,
            "detected": True,
            "source": "test",
        }
        for app in (messaging or [])
    ]
    profile["capabilities"]["media_control"] = {
        "supported": True,
        "method": "system_events_media_key",
        "verified": False,
        "status": "available",
    }
    profile["capabilities"]["screen_capture"] = {
        "status": "available",
        "requires_permission": True,
        "method": "mss",
    }
    profile["capabilities"]["ui_automation"] = {
        "status": "available",
        "method": "pyautogui",
        "requires_permission": False,
    }
    return profile


class DeviceProfileTests(unittest.TestCase):
    def test_missing_profile_creates_profile_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "config" / "device_profile.json"

            profile = ensure_device_profile(root, profile_path)

            self.assertTrue(profile_path.exists())
            valid, errors = validate_device_profile(profile)
            self.assertTrue(valid, errors)

    def test_profile_schema_validates(self):
        profile = default_profile(Path.cwd())

        valid, errors = validate_device_profile(profile)

        self.assertTrue(valid, errors)

    def test_platform_detection_returns_allowed_value(self):
        self.assertIn(detect_platform_key(), {"macos", "windows", "linux", "unknown"})

    def test_browser_routing_explicit_browser_wins(self):
        profile = _profile(browsers=["chrome", "safari"], default_browser="safari")
        ctx = SessionContext()
        ctx.last_browser_used = "safari"

        route = resolve_browser_route(profile, "chrome", ctx)

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["browser"], "chrome")
        self.assertEqual(route["source"], "explicit")

    def test_browser_routing_session_context_wins_without_explicit_browser(self):
        profile = _profile(browsers=["chrome", "safari"], default_browser="safari")
        ctx = SessionContext()
        ctx.last_browser_used = "chrome"

        route = resolve_browser_route(profile, "", ctx)

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["browser"], "chrome")
        self.assertEqual(route["source"], "session_context")

    def test_browser_routing_device_default_used_when_no_session(self):
        profile = _profile(browsers=["chrome", "safari"], default_browser="safari")

        route = resolve_browser_route(profile, "", SessionContext())

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["browser"], "safari")
        self.assertEqual(route["source"], "system_default")

    def test_browser_routing_unknown_browser_fails_honestly(self):
        profile = _profile(browsers=["safari"], default_browser="safari")

        route = resolve_browser_route(profile, "chrome", SessionContext())

        self.assertEqual(route["status"], "failed")
        self.assertIn("not found", route["reason"].lower())

    def test_browser_routing_no_browser_asks_clarification(self):
        profile = _profile(browsers=[], default_browser="unknown")

        route = resolve_browser_route(profile, "", SessionContext())

        self.assertEqual(route["status"], "needs_confirmation")

    def test_media_routing_available_attempts_platform_method(self):
        profile = _profile()

        route = resolve_media_route(profile)

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["method"], "system_events_media_key")

    def test_media_routing_unsupported_is_uncertain(self):
        profile = _profile()
        profile["capabilities"]["media_control"] = {
            "supported": False,
            "method": "unknown",
            "verified": False,
            "status": "unsupported",
        }

        route = resolve_media_route(profile)

        self.assertEqual(route["status"], "uncertain")
        self.assertFalse(route["verified"])

    def test_messaging_installed_app_may_attempt_after_confirmation(self):
        profile = _profile(messaging=["telegram"])

        route = resolve_messaging_route(profile, "Telegram", "Ali", confirmed=True)

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["app"], "telegram")

    def test_messaging_undetected_app_allowed_under_confirmation(self):
        # The user may message ANY named app under explicit confirmation; an app
        # not verified in DeviceProfile is a best-effort attempt (detected=False),
        # never a hard failure and never a fake success.
        profile = _profile(messaging=["telegram"])

        route = resolve_messaging_route(profile, "WhatsApp", "Ali", confirmed=True)

        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["app"], "whatsapp")
        self.assertFalse(route["detected"])
        self.assertIn("not verified", route["reason"].lower())

    def test_messaging_undetected_app_asks_confirmation_first(self):
        profile = _profile(messaging=["telegram"])

        route = resolve_messaging_route(profile, "WhatsApp", "Ali", confirmed=False)

        self.assertEqual(route["status"], "needs_confirmation")
        self.assertFalse(route["detected"])

    def test_messaging_unverified_contact_asks_confirmation(self):
        profile = _profile(messaging=["telegram"])

        route = resolve_messaging_route(profile, "Telegram", "", confirmed=False)

        self.assertEqual(route["status"], "needs_confirmation")

    def test_permission_gating_blocks_unknown_screen_capture(self):
        profile = _profile()
        profile["capabilities"]["screen_capture"] = {
            "status": "unknown",
            "requires_permission": True,
            "method": "unknown",
        }

        gate = check_permission_gate(profile, "screen_capture")

        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["status"], "unknown")

    def test_permission_gating_blocks_unknown_ui_automation(self):
        profile = _profile()
        profile["capabilities"]["ui_automation"] = {
            "status": "unknown",
            "method": "unknown",
            "requires_permission": True,
        }

        gate = check_permission_gate(profile, "ui_automation")

        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["status"], "unknown")

    def test_profile_privacy_has_no_forbidden_secret_fields(self):
        profile = _profile(browsers=["safari"], messaging=["telegram"])

        self.assertEqual(find_privacy_violations(profile), [])

    def test_profile_scrubber_redacts_secret_like_fields(self):
        bad = {"platform": {"os": "macos"}, "gemini_api_key": "secret-value"}

        scrubbed = scrub_profile(bad)

        self.assertEqual(scrubbed["gemini_api_key"], "[redacted]")
        self.assertTrue(find_privacy_violations(bad))

    def test_real_device_profile_is_gitignored(self):
        root = Path(__file__).resolve().parents[1]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("config/device_profile.json", gitignore)

    def test_installed_browser_ids_supports_schema_list(self):
        profile = _profile(browsers=["chrome", "safari"])

        self.assertEqual(installed_browser_ids(profile), ["chrome", "safari"])

    def test_refresh_command_recognizes_uzbek_curly_apostrophe(self):
        self.assertTrue(is_device_profile_refresh_request("Mac’ni qayta tekshir"))
        self.assertTrue(is_device_profile_refresh_request("kompyuterni qayta o‘rgan"))


if __name__ == "__main__":
    unittest.main()

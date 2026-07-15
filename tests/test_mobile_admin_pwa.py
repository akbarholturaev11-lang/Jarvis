from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "product_backend" / "admin_web" / "static"


class MobileAdminPwaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.worker = (STATIC_ROOT / "sw.js").read_text(encoding="utf-8")
        cls.manifest = json.loads(
            (STATIC_ROOT / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        cls.catalog = json.loads(
            (STATIC_ROOT / "i18n.json").read_text(encoding="utf-8")
        )
        cls.docs = (PROJECT_ROOT / "docs" / "MOBILE_ADMIN.md").read_text(
            encoding="utf-8"
        )

    def test_manifest_is_local_scoped_and_has_local_maskable_artwork(self):
        self.assertEqual(self.manifest["id"], "./")
        self.assertEqual(self.manifest["start_url"], "./")
        self.assertEqual(self.manifest["scope"], "./")
        self.assertEqual(self.manifest["display"], "standalone")
        self.assertIn('rel="manifest" href="manifest.webmanifest"', self.html)
        self.assertIn("JARVIS", self.manifest["name"])
        self.assertIn("Администратор", self.manifest["name"])
        for icon in self.manifest["icons"]:
            with self.subTest(icon=icon):
                parsed = urlparse(icon["src"])
                self.assertEqual(parsed.scheme, "")
                self.assertEqual(parsed.netloc, "")
                self.assertNotIn("..", Path(parsed.path).parts)
                self.assertTrue((STATIC_ROOT / parsed.path).is_file())
                self.assertIn("maskable", icon["purpose"])

    def test_service_worker_caches_only_an_explicit_public_shell(self):
        shell_match = re.search(
            r"const SHELL_URLS = \[(.*?)\n\];",
            self.worker,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(shell_match)
        shell = shell_match.group(1)
        for asset in (
            'new URL("./", SCOPE_URL).href',
            'new URL("index.html", SCOPE_URL).href',
            'new URL("styles.css", SCOPE_URL).href',
            'new URL("app.js", SCOPE_URL).href',
            'new URL("i18n.json", SCOPE_URL).href',
            'new URL("manifest.webmanifest", SCOPE_URL).href',
            'new URL("icons/admin-icon.svg", SCOPE_URL).href',
        ):
            self.assertIn(asset, shell)
        for forbidden in ("/api/", "session", "evidence", "payments", "audit"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, shell.casefold())
        self.assertIn('if (url.pathname.startsWith("/api/")) return;', self.worker)
        self.assertIn("!SHELL_PATHS.has(url.pathname)", self.worker)
        self.assertIn("if (url.search || url.hash", self.worker)
        self.assertIn("key.startsWith(OWNED_CACHE_PREFIX)", self.worker)
        self.assertNotIn("caches.match(event.request)", self.worker)

    def test_client_uses_visible_online_bounded_polling_and_in_app_alerts(self):
        for required in (
            "const PAYMENT_POLL_INTERVAL_MS = 30_000;",
            'document.visibilityState === "visible"',
            "navigator.onLine",
            "schedulePaymentPoll",
            "stopPaymentPolling",
            "knownPendingPaymentIds",
            'loadPayments({ notifyNew: true })',
            'id="notification-banner"',
            'data-i18n="notifications_in_app"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.javascript + self.html)
        self.assertNotIn("setInterval", self.javascript)

    def test_background_privacy_cleanup_removes_sensitive_ephemeral_material(self):
        cleanup = re.search(
            r"function clearSensitiveUiForBackground\(\) \{(.*?)\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(cleanup)
        source = cleanup.group(1)
        for required in (
            "stopPaymentPolling()",
            "revokeEvidenceUrl()",
            "revokeMfaQrUrl()",
            "clearRecoveryCodes()",
            "clearActivationCredential()",
            'input[type=\'password\']',
            "privacy-shield",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)
        self.assertIn(
            'document.addEventListener("visibilitychange", handleVisibilityChange)',
            self.javascript,
        )
        self.assertIn('window.addEventListener("pagehide"', self.javascript)

    def test_mobile_navigation_is_five_target_touch_safe(self):
        self.assertEqual(len(re.findall(r'class="nav-button(?: is-active)?"', self.html)), 5)
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr));", self.css)
        self.assertIn("env(safe-area-inset-bottom)", self.css)
        self.assertRegex(self.css, r"\.nav-button \{[^}]*min-height: 48px",)
        self.assertIn("@media (pointer: coarse)", self.css)

    def test_admin_navigation_and_auth_material_stay_same_origin_and_bridge_free(self):
        for required in (
            "guardExternalNavigation",
            "target.origin !== window.location.origin",
            'credentials: "same-origin"',
            'new URL("sw.js", document.baseURI)',
            '{ scope: "./", updateViaCache: "none" }',
        ):
            self.assertIn(required, self.javascript)
        for forbidden in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "document.cookie",
            "window.open",
            "postMessage",
            "webkit.messageHandlers",
            "addJavascriptInterface",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.javascript + self.html)

    def test_customer_release_and_device_admin_reads_are_wired(self):
        for required in (
            '"/api/admin/accounts?limit=100&offset=0"',
            '"/api/admin/licenses?limit=100&offset=0&entitlements_limit=20"',
            '"/api/admin/releases?limit=100&offset=0"',
            'id="accounts-body"',
            'id="licenses-body"',
            'window.confirm(t("create_release_confirm"',
            "handleReplaceDevice",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.javascript + self.html)

    def test_new_visible_states_have_matching_english_and_russian_text(self):
        required = {
            "network_offline_title",
            "network_offline_note",
            "network_restored",
            "notifications_in_app",
            "new_payment_alert_title",
            "new_payment_alert",
            "privacy_title",
            "privacy_note",
            "customer_records",
            "license_records",
            "create_release_confirm",
        }
        self.assertEqual(set(self.catalog["en"]), set(self.catalog["ru"]))
        self.assertTrue(required <= set(self.catalog["en"]))
        for key in required:
            with self.subTest(key=key):
                self.assertTrue(self.catalog["en"][key].strip())
                self.assertTrue(self.catalog["ru"][key].strip())

    def test_native_and_push_statuses_are_documented_honestly(self):
        self.assertIn("Native iOS and Android applications are **`not_available`**", self.docs)
        self.assertIn("in-app only", self.docs)
        self.assertIn("APNs, FCM or Web Push provider is **`not_available`**", self.docs)
        self.assertIn("There is no JavaScript bridge", self.docs)
        self.assertIn("must never be recorded as a passing native test", self.docs)


if __name__ == "__main__":
    unittest.main()

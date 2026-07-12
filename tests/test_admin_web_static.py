from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from product_backend.admin_web import ADMIN_WEB_CSP, mount_admin_web


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "product_backend" / "admin_web" / "static"


class _AdminHtmlInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.translation_keys: set[str] = set()
        self.inline_handlers: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.remote_assets: list[str] = []
        self._script_without_source = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        for name, value in attrs:
            if name in {"data-i18n", "data-i18n-aria"} and value:
                self.translation_keys.add(value)
            if name.startswith("on"):
                self.inline_handlers.append(name)
            if name == "style":
                self.inline_styles += 1
        if tag == "script":
            self._script_without_source = "src" not in attributes
            if self._script_without_source:
                self.inline_scripts += 1
        for name in ("src", "href"):
            value = attributes.get(name)
            if value and re.match(r"(?i)(?:https?:)?//", value):
                self.remote_assets.append(value)


class AdminWebStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.catalog = json.loads(
            (STATIC_ROOT / "i18n.json").read_text(encoding="utf-8")
        )
        cls.inspector = _AdminHtmlInspector()
        cls.inspector.feed(cls.html)

    def test_admin_assets_are_local_and_do_not_use_inline_executable_content(self):
        self.assertEqual(self.inspector.remote_assets, [])
        self.assertEqual(self.inspector.inline_handlers, [])
        self.assertEqual(self.inspector.inline_scripts, 0)
        self.assertEqual(self.inspector.inline_styles, 0)
        for forbidden in (
            "unpkg",
            "jsdelivr",
            "fonts.googleapis",
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "innerHTML",
            "eval(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.html + self.css + self.javascript)

    def test_every_fixed_translation_key_has_matching_english_and_russian_text(self):
        self.assertEqual(set(self.catalog), {"en", "ru"})
        self.assertEqual(set(self.catalog["en"]), set(self.catalog["ru"]))
        self.assertTrue(self.inspector.translation_keys <= set(self.catalog["en"]))
        for language in ("en", "ru"):
            self.assertTrue(
                all(
                    isinstance(value, str) and value.strip()
                    for value in self.catalog[language].values()
                )
            )

    def test_javascript_uses_session_cookie_csrf_and_safe_dom_boundaries(self):
        for required in (
            '"/api/admin/session"',
            '"/api/admin/payments?limit=100"',
            '"/api/admin/audit?limit=100"',
            '"/api/releases?limit=100"',
            'headers["X-CSRF-Token"]',
            'credentials: "same-origin"',
            "encodeURIComponent(payment.id)",
            "encodeURIComponent(releaseId)",
            "URL.revokeObjectURL",
            "textContent",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.javascript)
        self.assertNotIn("csrf_token =", self.javascript)
        self.assertNotRegex(self.javascript, r"console\.(?:log|debug|info|warn|error)")

    def test_sensitive_actions_are_not_presented_as_automatic(self):
        for language in ("en", "ru"):
            text = " ".join(self.catalog[language].values()).casefold()
            with self.subTest(language=language):
                self.assertIn(
                    "never confirms payment automatically"
                    if language == "en"
                    else "никогда не подтверждает оплату автоматически",
                    text,
                )
        self.assertIn("evidence_type_invalid", self.javascript)
        self.assertIn('payment.state === "under_review"', self.javascript)

    def test_provisioning_uses_exact_admin_routes_and_csrf_mutations(self):
        for required in (
            'apiJson("/api/admin/accounts"',
            "`/api/admin/accounts/${encodeURIComponent(accountId)}/licenses`",
            "`/api/admin/licenses/${encodeURIComponent(licenseId)}/devices`",
            "`/api/admin/licenses/${encodeURIComponent(licenseId)}/versions/${encodeURIComponent(version)}/activation-credentials`",
            "handleCreateAccount",
            "handleIssueLicense",
            "handleBindDevice",
            "handleReplaceDevice",
            "handleIssueActivation",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.javascript)
        self.assertGreaterEqual(self.javascript.count("mutate: true"), 8)

    def test_device_replacement_form_is_explicit_bilingual_and_admin_mutated(self):
        for required in (
            'id="device-replace-form"',
            'name="current_device_key_fingerprint"',
            'name="new_device_key_fingerprint"',
            'name="new_platform"',
            'name="new_architecture"',
            'name="new_device_label"',
            'name="replacement_reason"',
            'id="device-replacement-result"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.html)
        for required in (
            "handleReplaceDevice",
            "`/api/admin/licenses/${encodeURIComponent(licenseId)}/devices/replace`",
            "current_device_key_fingerprint: currentFingerprint",
            "new_device_key_fingerprint: newFingerprint",
            "replacement_reason: reason",
            'window.confirm(t("replacement_confirm"',
            'showToast(t("device_replaced"))',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.javascript)
        for language in ("en", "ru"):
            with self.subTest(language=language):
                self.assertIn("replace_device", self.catalog[language])
                self.assertIn("replacement_confirm", self.catalog[language])
                self.assertIn(
                    "device_replacement_result",
                    self.catalog[language],
                )

    def test_activation_key_is_ephemeral_and_never_added_to_dashboard_state(self):
        self.assertIn('id="activation-key-value"', self.html)
        self.assertIn("readonly", self.html)
        self.assertIn('element("activation-key-value").value = issued.license_key', self.javascript)
        self.assertIn('element("activation-key-value").value = ""', self.javascript)
        self.assertIn(
            'element("activation-key-dialog").addEventListener("close", clearActivationCredential)',
            self.javascript,
        )
        self.assertIn("navigator.clipboard.writeText(field.value)", self.javascript)
        self.assertNotRegex(
            self.javascript,
            r"state\.[A-Za-z0-9_.]*\s*=\s*issued\.license_key",
        )
        for language in ("en", "ru"):
            warning = self.catalog[language]["activation_key_warning"].casefold()
            with self.subTest(language=language):
                self.assertIn(
                    "not stored in browser storage"
                    if language == "en"
                    else "не сохраняется в хранилище браузера",
                    warning,
                )


class AdminWebMountTests(unittest.TestCase):
    def _app(self) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def api_csp(request: Request, call_next):
            response = await call_next(request)
            response.headers["Content-Security-Policy"] = "default-src 'none'"
            return response

        @app.get("/api/ping")
        def ping() -> dict[str, bool]:
            return {"ok": True}

        mount_admin_web(app)
        return app

    def test_mount_serves_only_static_admin_assets_with_admin_csp(self):
        with TestClient(self._app()) as client:
            page = client.get("/admin/")
            script = client.get("/admin/app.js")
            hidden_python = client.get("/admin/mount.py")

        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertEqual(page.headers["content-security-policy"], ADMIN_WEB_CSP)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["permissions-policy"], "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", script.headers["content-type"])
        self.assertEqual(hidden_python.status_code, 404)

    def test_mount_does_not_capture_api_routes_or_relax_api_csp(self):
        with TestClient(self._app()) as client:
            response = client.get("/api/ping")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(
            response.headers["content-security-policy"],
            "default-src 'none'",
        )

    def test_mount_rejects_root_api_and_traversal_prefixes(self):
        for path in ("/", "/api", "/admin/../api", "admin"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mount_admin_web(FastAPI(), path=path)
        with self.assertRaises(TypeError):
            mount_admin_web(FastAPI(), path=None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

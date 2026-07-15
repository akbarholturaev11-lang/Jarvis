from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient

from product_backend.admin_mfa import (
    AdminMfaSettings,
    MfaSecretCipher,
    SQLiteAdminMfaManager,
)
from product_backend.api_app import create_product_backend_app
from product_backend.api_auth import (
    AdminAuthSettings,
    AdminPasswordCredential,
    AdminSessionManager,
    SessionAssurance,
    TrustedProxyConfig,
)
from product_backend.api_ports import (
    ClientActivationPort,
    DeviceChallengePort,
    PrivatePaymentEvidenceStore,
    ProductReadStore,
    ReleaseArtifactStore,
)
from product_backend.api_totp import decode_base32_secret, totp_code
from product_backend.repository import CommerceRepository


PASSWORD = "a-strong-admin-password"
SUBJECT = "admin:test"
COOKIE = "jarvis_admin_session"


def _auth_settings(**overrides) -> AdminAuthSettings:
    credential = AdminPasswordCredential.derive_for_configuration(
        subject=SUBJECT, password=PASSWORD, salt=b"s" * 32
    )
    return AdminAuthSettings(
        (credential,),
        b"session-secret-for-mfa-session-tests",
        ("testserver",),
        secure_cookie=False,
        **overrides,
    )


class SessionManagerHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = {"t": datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)}
        self.settings = _auth_settings()
        self.manager = AdminSessionManager(
            self.settings, clock=lambda: self.clock["t"]
        )

    def test_rotation_invalidates_old_token(self) -> None:
        issued = self.manager.issue_session(SUBJECT)
        self.assertIsNotNone(self.manager.resolve(issued.session_token))
        rotated = self.manager.rotate(issued.session_token)
        self.assertIsNotNone(rotated)
        self.assertNotEqual(rotated.session_token, issued.session_token)
        self.assertIsNone(self.manager.resolve(issued.session_token))
        self.assertIsNotNone(self.manager.resolve(rotated.session_token))

    def test_idle_timeout_expires_untouched_session(self) -> None:
        issued = self.manager.issue_session(SUBJECT)
        self.clock["t"] += timedelta(
            seconds=self.settings.idle_timeout_seconds + 1
        )
        self.assertIsNone(self.manager.resolve(issued.session_token))

    def test_absolute_timeout_expires_active_session(self) -> None:
        settings = _auth_settings(
            session_ttl_seconds=1800, idle_timeout_seconds=1800
        )
        manager = AdminSessionManager(settings, clock=lambda: self.clock["t"])
        issued = manager.issue_session(SUBJECT)
        # Keep the session "seen" well within the idle window.
        self.clock["t"] += timedelta(seconds=900)
        self.assertIsNotNone(manager.resolve(issued.session_token))
        # Cross the absolute lifetime; idle has not elapsed since the last touch.
        self.clock["t"] += timedelta(seconds=901)
        self.assertIsNone(manager.resolve(issued.session_token))

    def test_revoke_all_for_subject(self) -> None:
        first = self.manager.issue_session(SUBJECT)
        second = self.manager.issue_session(SUBJECT)
        self.assertEqual(self.manager.revoke_all_for_subject(SUBJECT), 2)
        self.assertIsNone(self.manager.resolve(first.session_token))
        self.assertIsNone(self.manager.resolve(second.session_token))

    def test_revoke_named_session_only(self) -> None:
        keep = self.manager.issue_session(SUBJECT)
        drop = self.manager.issue_session(SUBJECT)
        self.assertTrue(
            self.manager.revoke_session_id(SUBJECT, drop.session_id)
        )
        self.assertIsNone(self.manager.resolve(drop.session_token))
        self.assertIsNotNone(self.manager.resolve(keep.session_token))

    def test_requires_reauth_after_window(self) -> None:
        issued = self.manager.issue_session(SUBJECT)
        record = self.manager.resolve(issued.session_token)
        self.assertFalse(self.manager.requires_reauth(record))
        self.clock["t"] += timedelta(
            seconds=self.settings.reauth_window_seconds + 1
        )
        record = self.manager.resolve(issued.session_token)
        self.assertTrue(self.manager.requires_reauth(record))

    def test_pending_session_always_requires_reauth(self) -> None:
        issued = self.manager.issue_session(
            SUBJECT, assurance=SessionAssurance.MFA_PENDING
        )
        record = self.manager.resolve(issued.session_token)
        self.assertTrue(self.manager.requires_reauth(record))


class TrustedProxyTests(unittest.TestCase):
    def test_forwarded_header_ignored_without_configuration(self) -> None:
        config = TrustedProxyConfig.from_spec(None)
        self.assertEqual(config.client_ip("198.51.100.7", "203.0.113.9"), "198.51.100.7")

    def test_forwarded_header_honored_only_from_trusted_peer(self) -> None:
        config = TrustedProxyConfig.from_spec("10.0.0.0/8")
        # Direct peer is a trusted proxy: take the client hop it forwarded.
        self.assertEqual(
            config.client_ip("10.0.0.5", "203.0.113.9, 10.0.0.5"),
            "203.0.113.9",
        )
        # Direct peer is not trusted: ignore whatever it claims.
        self.assertEqual(
            config.client_ip("198.51.100.7", "203.0.113.9"),
            "198.51.100.7",
        )

    def test_invalid_spec_is_rejected(self) -> None:
        from product_backend.api_auth import BackendConfigurationError

        with self.assertRaises(BackendConfigurationError):
            TrustedProxyConfig.from_spec("not-a-network")


class _MfaAppHarness:
    def __init__(self, **mfa_kwargs) -> None:
        self.clock = {"t": datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)}
        settings_kwargs = mfa_kwargs.pop("settings_kwargs", {})
        auth_overrides = mfa_kwargs.pop("auth_overrides", {})
        self.mfa = SQLiteAdminMfaManager(
            MfaSecretCipher(b"m" * 32),
            ":memory:",
            settings=AdminMfaSettings(mandatory=True, **settings_kwargs),
            clock=lambda: self.clock["t"],
        )
        self.app = create_product_backend_app(
            commerce=Mock(spec=CommerceRepository),
            reads=Mock(spec=ProductReadStore),
            evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
            challenges=Mock(spec=DeviceChallengePort),
            activation=Mock(spec=ClientActivationPort),
            release_artifact_store=Mock(spec=ReleaseArtifactStore),
            auth_settings=_auth_settings(**auth_overrides),
            mfa=self.mfa,
            clock=lambda: self.clock["t"],
        )
        self.client = TestClient(self.app)

    def login(self, **body):
        payload = {"subject": SUBJECT, "password": PASSWORD}
        payload.update(body)
        return self.client.post("/api/admin/session", json=payload)

    def enroll_and_activate(self):
        login = self.login()
        csrf = login.json()["csrf_token"]
        start = self.client.post(
            "/api/admin/mfa/enrollment", headers={"X-CSRF-Token": csrf}
        ).json()
        secret = decode_base32_secret(start["secret_base32"])
        code = totp_code(secret, self.clock["t"].timestamp())
        activated = self.client.post(
            "/api/admin/mfa/enrollment/activate",
            json={"totp": code},
            headers={"X-CSRF-Token": csrf},
        )
        return secret, activated.json(), start


class MfaLoginFlowTests(unittest.TestCase):
    def test_not_enrolled_login_is_pending_and_restricted(self) -> None:
        harness = _MfaAppHarness()
        response = harness.login()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["mfa_enrollment_required"])
        self.assertEqual(body["assurance"], "mfa_pending")
        # A pending session may not reach a full-assurance route.
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 403)
        # But it can reach enrollment status.
        self.assertEqual(harness.client.get("/api/admin/mfa").status_code, 200)

    def test_enrollment_completion_activates_and_rotates_session(self) -> None:
        harness = _MfaAppHarness()
        csrf = harness.login().json()["csrf_token"]
        pre_cookie = harness.client.cookies.get(COOKIE)
        start = harness.client.post(
            "/api/admin/mfa/enrollment", headers={"X-CSRF-Token": csrf}
        ).json()
        secret = decode_base32_secret(start["secret_base32"])
        code = totp_code(secret, harness.clock["t"].timestamp())
        activated = harness.client.post(
            "/api/admin/mfa/enrollment/activate",
            json={"totp": code},
            headers={"X-CSRF-Token": csrf},
        ).json()
        self.assertEqual(activated["assurance"], "mfa_satisfied")
        self.assertEqual(len(activated["recovery_codes"]), 10)
        # Session was rotated: the pre-activation cookie no longer resolves.
        self.assertNotEqual(harness.client.cookies.get(COOKIE), pre_cookie)
        harness.client.cookies.clear()
        harness.client.cookies.set(COOKIE, pre_cookie)
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 401)

    def test_active_login_requires_valid_totp(self) -> None:
        harness = _MfaAppHarness()
        secret, activated, _ = harness.enroll_and_activate()
        csrf = activated["csrf_token"]
        harness.client.delete("/api/admin/session", headers={"X-CSRF-Token": csrf})
        # Password only, no code -> rejected.
        self.assertEqual(harness.login().status_code, 401)
        # Correct password + valid code -> satisfied.
        self.clock_advance(harness, 60)
        code = totp_code(secret, harness.clock["t"].timestamp())
        ok = harness.login(totp=code)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["assurance"], "mfa_satisfied")
        # Replaying the same code -> rejected.
        harness.client.delete(
            "/api/admin/session", headers={"X-CSRF-Token": ok.json()["csrf_token"]}
        )
        self.assertEqual(harness.login(totp=code).status_code, 401)

    def test_recovery_code_login_is_single_use(self) -> None:
        harness = _MfaAppHarness()
        _, activated, _ = harness.enroll_and_activate()
        recovery = activated["recovery_codes"][0]
        harness.client.delete(
            "/api/admin/session",
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        first = harness.login(recovery_code=recovery)
        self.assertEqual(first.status_code, 200)
        harness.client.delete(
            "/api/admin/session",
            headers={"X-CSRF-Token": first.json()["csrf_token"]},
        )
        self.assertEqual(harness.login(recovery_code=recovery).status_code, 401)

    def test_csrf_required_for_enrollment(self) -> None:
        harness = _MfaAppHarness()
        harness.login()  # pending session cookie set on the client
        self.assertEqual(
            harness.client.post("/api/admin/mfa/enrollment").status_code, 403
        )

    def test_login_factor_rate_limit(self) -> None:
        harness = _MfaAppHarness()
        secret, activated, _ = harness.enroll_and_activate()
        harness.client.delete(
            "/api/admin/session",
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        statuses = [harness.login(totp="000000").status_code for _ in range(6)]
        self.assertEqual(statuses, [401, 401, 401, 401, 401, 429])

    def test_enrollment_rate_limit(self) -> None:
        harness = _MfaAppHarness()
        csrf = harness.login().json()["csrf_token"]
        statuses = [
            harness.client.post(
                "/api/admin/mfa/enrollment", headers={"X-CSRF-Token": csrf}
            ).status_code
            for _ in range(6)
        ]
        self.assertEqual(statuses[-1], 429)
        self.assertTrue(all(code == 201 for code in statuses[:5]))

    def test_logout_and_revoke_all(self) -> None:
        harness = _MfaAppHarness()
        _, activated, _ = harness.enroll_and_activate()
        csrf = activated["csrf_token"]
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 200)
        harness.client.post(
            "/api/admin/sessions/revoke-all", headers={"X-CSRF-Token": csrf}
        )
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 401)

    def test_idle_timeout_over_http(self) -> None:
        harness = _MfaAppHarness()
        harness.enroll_and_activate()
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 200)
        self.clock_advance(harness, 901)
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 401)

    def test_mfa_audit_endpoint_and_no_secret_leakage(self) -> None:
        harness = _MfaAppHarness()
        secret, activated, start = harness.enroll_and_activate()
        audit = harness.client.get("/api/admin/mfa/audit")
        self.assertEqual(audit.status_code, 200)
        events = {item["event"] for item in audit.json()["events"]}
        self.assertIn("enrollment_completed", events)
        # No response other than the one-time reveals may contain the secret
        # or a recovery code.
        secret_b32 = start["secret_base32"]
        recovery = activated["recovery_codes"]
        for path in ("/api/admin/mfa", "/api/admin/mfa/audit", "/api/admin/sessions"):
            text = harness.client.get(path).text
            self.assertNotIn(secret_b32, text)
            for code in recovery:
                self.assertNotIn(code, text)

    def clock_advance(self, harness: _MfaAppHarness, seconds: int) -> None:
        harness.clock["t"] += timedelta(seconds=seconds)


class MfaDisabledManagerTests(unittest.TestCase):
    def test_app_without_mfa_keeps_single_factor_login(self) -> None:
        clock = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        app = create_product_backend_app(
            commerce=Mock(spec=CommerceRepository),
            reads=Mock(spec=ProductReadStore),
            evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
            challenges=Mock(spec=DeviceChallengePort),
            activation=Mock(spec=ClientActivationPort),
            release_artifact_store=Mock(spec=ReleaseArtifactStore),
            auth_settings=_auth_settings(),
            clock=lambda: clock,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/admin/session",
                json={"subject": SUBJECT, "password": PASSWORD},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["assurance"], "mfa_satisfied")
            # The MFA routes are not mounted when no manager is configured.
            self.assertEqual(client.get("/api/admin/mfa").status_code, 404)


if __name__ == "__main__":
    unittest.main()

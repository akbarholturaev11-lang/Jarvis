from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from product_backend.admin_mfa import (
    AdminMfaSettings,
    MfaSecretCipher,
    SQLiteAdminMfaManager,
)
from product_backend.admin_credentials import SQLiteAdminCredentialStore
from product_backend.api_app import create_product_backend_app
from product_backend.api_auth import (
    AdminAuthSettings,
    AdminIpAllowlist,
    AdminPasswordCredential,
    AdminSessionManager,
    BackendConfigurationError,
    PasswordChangeResult,
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
        secure_cookie=overrides.pop("secure_cookie", False),
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


class AdminIpAllowlistTests(unittest.TestCase):
    def test_disabled_allowlist_accepts_any_peer(self) -> None:
        self.assertTrue(AdminIpAllowlist.from_spec(None).allows("not-an-ip"))

    def test_configured_allowlist_fails_closed(self) -> None:
        policy = AdminIpAllowlist.from_spec("10.0.0.0/8,2001:db8::/32")
        self.assertTrue(policy.allows("10.4.5.6"))
        self.assertTrue(policy.allows("2001:db8::1"))
        self.assertFalse(policy.allows("198.51.100.2"))
        self.assertFalse(policy.allows("unknown"))

    def test_invalid_or_blank_spec_is_rejected(self) -> None:
        for value in ("   ", "not-a-network"):
            with self.subTest(value=value), self.assertRaises(
                BackendConfigurationError
            ):
                AdminIpAllowlist.from_spec(value)


class PersistentAdminCredentialTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_unsafe_existing_credential_file_is_rejected_before_sqlite_opens(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "admin-credentials.sqlite3"
            marker = b"must-not-be-overwritten"
            path.write_bytes(marker)
            path.chmod(0o644)
            with self.assertRaises(BackendConfigurationError):
                SQLiteAdminCredentialStore(path, _auth_settings().credentials)
            self.assertEqual(path.read_bytes(), marker)

    def test_password_change_survives_restart_without_plaintext_storage(self) -> None:
        new_password = "a-new-strong-admin-password"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "admin-credentials.sqlite3"
            settings = _auth_settings()
            store = SQLiteAdminCredentialStore(path, settings.credentials)
            manager = AdminSessionManager(settings, credential_store=store)
            self.assertEqual(
                manager.change_password(SUBJECT, PASSWORD, new_password),
                PasswordChangeResult.CHANGED,
            )
            store.close()

            reopened = SQLiteAdminCredentialStore(path, settings.credentials)
            restarted = AdminSessionManager(settings, credential_store=reopened)
            self.assertIsNone(restarted.verify_password(SUBJECT, PASSWORD))
            self.assertEqual(
                restarted.verify_password(SUBJECT, new_password), SUBJECT
            )
            self.assertNotIn(new_password.encode("utf-8"), path.read_bytes())
            reopened.close()


class _MfaAppHarness:
    def __init__(self, **mfa_kwargs) -> None:
        self.clock = {"t": datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)}
        settings_kwargs = mfa_kwargs.pop("settings_kwargs", {})
        auth_overrides = mfa_kwargs.pop("auth_overrides", {})
        trusted_proxy = mfa_kwargs.pop("trusted_proxy", None)
        admin_ip_allowlist = mfa_kwargs.pop("admin_ip_allowlist", None)
        self.mfa = SQLiteAdminMfaManager(
            MfaSecretCipher(b"m" * 32),
            ":memory:",
            settings=AdminMfaSettings(mandatory=True, **settings_kwargs),
            clock=lambda: self.clock["t"],
        )
        auth_settings = _auth_settings(**auth_overrides)
        self.credentials = SQLiteAdminCredentialStore(
            ":memory:", auth_settings.credentials, clock=lambda: self.clock["t"]
        )
        self.commerce = Mock(spec=CommerceRepository)
        self.app = create_product_backend_app(
            commerce=self.commerce,
            reads=Mock(spec=ProductReadStore),
            evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
            challenges=Mock(spec=DeviceChallengePort),
            activation=Mock(spec=ClientActivationPort),
            release_artifact_store=Mock(spec=ReleaseArtifactStore),
            auth_settings=auth_settings,
            mfa=self.mfa,
            trusted_proxy=trusted_proxy,
            admin_ip_allowlist=admin_ip_allowlist,
            admin_credential_store=self.credentials,
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

    def test_production_cookie_flags_are_secure_http_only_and_strict(self) -> None:
        harness = _MfaAppHarness(auth_overrides={"secure_cookie": True})
        response = harness.login()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/api/admin", cookie)

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

    def test_factor_rate_limit_is_account_global_across_client_ips(self) -> None:
        harness = _MfaAppHarness()
        secret, activated, _ = harness.enroll_and_activate()
        harness.client.delete(
            "/api/admin/session",
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.clock_advance(harness, 60)
        correct = totp_code(secret, harness.clock["t"].timestamp())
        wrong = "999999" if correct == "000000" else "000000"
        statuses = []
        for index in range(6):
            with TestClient(
                harness.app,
                client=(f"198.51.100.{index + 1}", 50_000 + index),
            ) as client:
                statuses.append(
                    client.post(
                        "/api/admin/session",
                        json={
                            "subject": SUBJECT,
                            "password": PASSWORD,
                            "totp": wrong,
                        },
                    ).status_code
                )
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

    def test_password_change_is_durable_revokes_sessions_and_is_audited(self) -> None:
        harness = _MfaAppHarness()
        secret, activated, _ = harness.enroll_and_activate()
        new_password = "a-new-strong-admin-password"
        changed = harness.client.post(
            "/api/admin/password",
            json={
                "current_password": PASSWORD,
                "new_password": new_password,
            },
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertNotIn(PASSWORD, changed.text)
        self.assertNotIn(new_password, changed.text)
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 401)
        self.assertIn(
            "password_changed",
            {item.event.value for item in harness.mfa.list_audit(subject=SUBJECT)},
        )

        self.clock_advance(harness, 60)
        code = totp_code(secret, harness.clock["t"].timestamp())
        self.assertEqual(harness.login(totp=code).status_code, 401)
        accepted = harness.client.post(
            "/api/admin/session",
            json={
                "subject": SUBJECT,
                "password": new_password,
                "totp": code,
            },
        )
        self.assertEqual(accepted.status_code, 200)

    def test_invalid_current_password_does_not_revoke_live_session_or_echo_secrets(self) -> None:
        harness = _MfaAppHarness()
        _, activated, _ = harness.enroll_and_activate()
        wrong = "wrong-current-password-marker"
        new = "unused-new-password-marker"
        response = harness.client.post(
            "/api/admin/password",
            json={"current_password": wrong, "new_password": new},
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(wrong, response.text)
        self.assertNotIn(new, response.text)
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 200)

    def test_http_named_session_revoke_leaves_current_session_active(self) -> None:
        harness = _MfaAppHarness()
        _, activated, _ = harness.enroll_and_activate()
        other = harness.app.state.admin_sessions.issue_session(SUBJECT)
        response = harness.client.delete(
            f"/api/admin/sessions/{other.session_id}",
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            harness.app.state.admin_sessions.resolve(other.session_token)
        )
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 200)

    def test_mfa_reset_is_audited_and_revokes_session(self) -> None:
        harness = _MfaAppHarness()
        _, activated, _ = harness.enroll_and_activate()
        reset = harness.client.post(
            "/api/admin/mfa/disable",
            json={"reset": True},
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(harness.mfa.state(SUBJECT).value, "not_enrolled")
        self.assertIn(
            "mfa_reset",
            {item.event.value for item in harness.mfa.list_audit(subject=SUBJECT)},
        )
        self.assertEqual(harness.client.get("/api/admin/sessions").status_code, 401)

    def test_sensitive_core_mutation_requires_recent_step_up(self) -> None:
        harness = _MfaAppHarness(auth_overrides={"reauth_window_seconds": 60})
        secret, activated, _ = harness.enroll_and_activate()
        harness.commerce.create_account.return_value = SimpleNamespace(
            id="acct_recent_001",
            external_subject="customer:recent",
            created_at="2026-07-15T11:02:00.000000Z",
        )
        self.clock_advance(harness, 61)
        stale = harness.client.post(
            "/api/admin/accounts",
            json={"external_subject": "customer:recent"},
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(stale.status_code, 403)
        self.assertEqual(harness.commerce.create_account.call_count, 0)

        code = totp_code(secret, harness.clock["t"].timestamp())
        stepped_up = harness.client.post(
            "/api/admin/session/reauth",
            json={"totp": code},
            headers={"X-CSRF-Token": activated["csrf_token"]},
        )
        self.assertEqual(stepped_up.status_code, 200)
        accepted = harness.client.post(
            "/api/admin/accounts",
            json={"external_subject": "customer:recent"},
            headers={"X-CSRF-Token": stepped_up.json()["csrf_token"]},
        )
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(harness.commerce.create_account.call_count, 1)

    def test_configured_admin_network_blocks_other_peers(self) -> None:
        harness = _MfaAppHarness(
            admin_ip_allowlist=AdminIpAllowlist.from_spec("127.0.0.0/8")
        )
        self.assertEqual(harness.login().status_code, 403)
        with TestClient(harness.app, client=("127.0.0.8", 50000)) as client:
            allowed = client.post(
                "/api/admin/session",
                json={"subject": SUBJECT, "password": PASSWORD},
            )
        self.assertEqual(allowed.status_code, 200)

    def test_admin_allowlist_uses_forwarded_client_only_from_trusted_proxy(self) -> None:
        harness = _MfaAppHarness(
            trusted_proxy=TrustedProxyConfig.from_spec("10.0.0.0/8"),
            admin_ip_allowlist=AdminIpAllowlist.from_spec("203.0.113.0/24"),
        )
        headers = {"X-Forwarded-For": "203.0.113.7"}
        with TestClient(harness.app, client=("10.0.0.5", 50000)) as trusted:
            self.assertEqual(
                trusted.post(
                    "/api/admin/session",
                    json={"subject": SUBJECT, "password": PASSWORD},
                    headers=headers,
                ).status_code,
                200,
            )
        with TestClient(harness.app, client=("198.51.100.9", 50001)) as direct:
            self.assertEqual(
                direct.post(
                    "/api/admin/session",
                    json={"subject": SUBJECT, "password": PASSWORD},
                    headers=headers,
                ).status_code,
                403,
            )

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
    def test_app_without_mfa_fails_closed_by_default(self) -> None:
        with self.assertRaises(BackendConfigurationError):
            create_product_backend_app(
                commerce=Mock(spec=CommerceRepository),
                reads=Mock(spec=ProductReadStore),
                evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
                challenges=Mock(spec=DeviceChallengePort),
                activation=Mock(spec=ClientActivationPort),
                release_artifact_store=Mock(spec=ReleaseArtifactStore),
                auth_settings=_auth_settings(),
            )

    def test_explicit_password_only_override_keeps_single_factor_login(self) -> None:
        clock = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        app = create_product_backend_app(
            commerce=Mock(spec=CommerceRepository),
            reads=Mock(spec=ProductReadStore),
            evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
            challenges=Mock(spec=DeviceChallengePort),
            activation=Mock(spec=ClientActivationPort),
            release_artifact_store=Mock(spec=ReleaseArtifactStore),
            auth_settings=_auth_settings(),
            allow_password_only_admin=True,
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

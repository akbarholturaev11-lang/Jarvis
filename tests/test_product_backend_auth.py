from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from product_backend.api_app import create_product_backend_app
from product_backend.api_auth import (
    MIN_PBKDF2_ITERATIONS,
    AdminAuthSettings,
    AdminPasswordCredential,
    BoundedAttemptLimiter,
)
from product_backend.api_ports import (
    ClientActivationPort,
    DeviceChallengePort,
    PrivatePaymentEvidenceStore,
    ProductReadStore,
    ReleaseArtifactStore,
)
from product_backend.device_challenges import (
    DeviceChallengeAction,
    VerifiedDeviceChallenge,
)
from product_backend.models import VerifiedDevicePrincipal
from product_backend.repository import CommerceRepository


NOW = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)


def _login_test_app():
    credential = AdminPasswordCredential(
        "admin:test",
        b"s" * 32,
        b"d" * 32,
        MIN_PBKDF2_ITERATIONS,
    )
    settings = AdminAuthSettings(
        (credential,),
        b"session-secret-for-login-tests-32b",
        ("testserver",),
        secure_cookie=False,
    )
    return create_product_backend_app(
        commerce=Mock(spec=CommerceRepository),
        reads=Mock(spec=ProductReadStore),
        evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
        challenges=Mock(spec=DeviceChallengePort),
        activation=Mock(spec=ClientActivationPort),
        release_artifact_store=Mock(spec=ReleaseArtifactStore),
        auth_settings=settings,
        clock=lambda: NOW,
    )


class AdminLoginRateLimitTests(unittest.TestCase):
    def test_subject_rotation_and_forwarded_for_cannot_bypass_client_budget(self):
        app = _login_test_app()
        with TestClient(app) as client, patch.object(
            AdminPasswordCredential,
            "verify",
            autospec=True,
            return_value=False,
        ) as verify:
            for index in range(20):
                response = client.post(
                    "/api/admin/session",
                    json={
                        "subject": f"fake:{index:03d}",
                        "password": "incorrect",
                    },
                    headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
                )
                self.assertEqual(response.status_code, 401)

            blocked = client.post(
                "/api/admin/session",
                json={"subject": "fake:blocked", "password": "incorrect"},
                headers={"X-Forwarded-For": "198.51.100.200"},
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(verify.call_count, 20)

    def test_oversized_login_is_rejected_before_password_verification(self):
        app = _login_test_app()
        with TestClient(app) as client, patch.object(
            AdminPasswordCredential,
            "verify",
            autospec=True,
            return_value=False,
        ) as verify:
            response = client.post(
                "/api/admin/session",
                content=b'{' + (b'"padding":"' + b"x" * (300 * 1024) + b'"}'),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(verify.call_count, 0)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"], "default-src 'none'")

    def test_fake_payment_grant_cannot_open_larger_body_cap(self):
        app = _login_test_app()
        with TestClient(app) as client:
            response = client.post(
                "/api/customer/licenses/lic_test_001/releases/rel_test_001/payments",
                data={"paid_at": "2026-07-13T03:00:00Z"},
                files={"file": ("proof.png", b"x" * (300 * 1024), "image/png")},
                headers={"X-Device-Grant": "A" * 43},
            )

        self.assertEqual(response.status_code, 413)

    def test_real_payment_grant_opens_larger_cap_once_and_malformed_body_consumes_it(self):
        app = _login_test_app()
        license_id = "lic_test_001"
        release_id = "rel_test_001"
        verified = VerifiedDeviceChallenge(
            challenge_id="chl_test_001",
            license_id=license_id,
            action=DeviceChallengeAction.SUBMIT_PAYMENT,
            resource_id=release_id,
            device_principal=VerifiedDevicePrincipal(
                device_key_fingerprint="sha256:" + ("a" * 64),
                platform="macos",
                architecture="arm64",
                proof_verified=True,
            ),
            verified_at="2026-07-13T03:00:00.000000Z",
        )
        device_grant = app.state.device_grants.issue(verified).token
        headers = {
            "Content-Type": "multipart/form-data; boundary=broken",
            "X-Device-Grant": device_grant,
        }
        malformed_body = b"x" * (300 * 1024)

        with TestClient(app) as client:
            accepted_once = client.post(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                content=malformed_body,
                headers=headers,
            )
            rejected_after_consumption = client.post(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                content=malformed_body,
                headers=headers,
            )

        self.assertNotEqual(accepted_once.status_code, 413)
        self.assertEqual(rejected_after_consumption.status_code, 413)

    def test_per_subject_throttle_is_retained(self):
        app = _login_test_app()
        with TestClient(app) as client, patch.object(
            AdminPasswordCredential,
            "verify",
            autospec=True,
            return_value=False,
        ) as verify:
            responses = [
                client.post(
                    "/api/admin/session",
                    json={"subject": "admin:test", "password": "incorrect"},
                )
                for _ in range(6)
            ]

        self.assertEqual([item.status_code for item in responses], [401] * 5 + [429])
        self.assertEqual(verify.call_count, 5)

    def test_atomic_attempt_reservation_never_exceeds_budget(self):
        limiter = BoundedAttemptLimiter(
            max_attempts=20,
            window_seconds=300,
            max_keys=4,
            clock=lambda: NOW,
        )
        with ThreadPoolExecutor(max_workers=32) as executor:
            accepted = tuple(executor.map(lambda _: limiter.consume("peer"), range(100)))

        self.assertEqual(sum(accepted), 20)


if __name__ == "__main__":
    unittest.main()

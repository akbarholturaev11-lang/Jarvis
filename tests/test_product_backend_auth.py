from __future__ import annotations

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core.product_state import PaymentState
from product_backend.api_app import _BoundedBodyMiddleware, create_product_backend_app
from product_backend.api_auth import (
    MIN_PBKDF2_ITERATIONS,
    AdminAuthSettings,
    AdminPasswordCredential,
    AuthenticationCapacityError,
    BoundedAttemptLimiter,
    DeviceActionGrantManager,
    ReservedDeviceGrant,
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
from product_backend.models import PaymentSubmission, VerifiedDevicePrincipal
from product_backend.private_storage import (
    PrivateObjectMetadata,
    PrivateStorageValidationError,
)
from product_backend.repository import CommerceRepository


NOW = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)


def _verified_payment_grant(
    *,
    license_id: str = "lic_test_001",
    release_id: str = "rel_test_001",
) -> VerifiedDeviceChallenge:
    return VerifiedDeviceChallenge(
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


def _login_test_app(*, commerce=None, evidence_store=None):
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
    selected_commerce = (
        Mock(spec=CommerceRepository) if commerce is None else commerce
    )
    selected_evidence = (
        Mock(spec=PrivatePaymentEvidenceStore)
        if evidence_store is None
        else evidence_store
    )
    return create_product_backend_app(
        commerce=selected_commerce,
        reads=Mock(spec=ProductReadStore),
        evidence_store=selected_evidence,
        challenges=Mock(spec=DeviceChallengePort),
        activation=Mock(spec=ClientActivationPort),
        release_artifact_store=Mock(spec=ReleaseArtifactStore),
        auth_settings=settings,
        allow_password_only_admin=True,
        clock=lambda: NOW,
    )


class DeviceActionGrantReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.manager = DeviceActionGrantManager(
            b"device-grant-reservation-test-secret",
            max_grants=4,
            clock=lambda: self.now,
        )
        self.verified = _verified_payment_grant()

    def _issue(self) -> str:
        return self.manager.issue(self.verified).token

    def _reserve(self, token: str, **overrides) -> ReservedDeviceGrant | None:
        context = {
            "license_id": self.verified.license_id,
            "action": self.verified.action,
            "resource_id": self.verified.resource_id,
        }
        context.update(overrides)
        return self.manager.reserve(token, **context)

    def test_wrong_context_does_not_consume_grant(self) -> None:
        token = self._issue()

        self.assertIsNone(self._reserve(token, resource_id="rel_wrong_001"))
        consumed = self.manager.consume(
            token,
            license_id=self.verified.license_id,
            action=self.verified.action,
            resource_id=self.verified.resource_id,
        )

        self.assertEqual(consumed, self.verified)

    def test_parallel_replay_gets_exactly_one_reservation(self) -> None:
        token = self._issue()
        with ThreadPoolExecutor(max_workers=16) as executor:
            reservations = tuple(
                executor.map(lambda _: self._reserve(token), range(64))
            )
        held = tuple(item for item in reservations if item is not None)

        self.assertEqual(len(held), 1)
        self.assertIsNone(self._reserve(token))
        self.assertTrue(self.manager.release(held[0]))
        self.assertIsNotNone(self._reserve(token))

    def test_commit_is_single_use_and_redacts_reservation(self) -> None:
        token = self._issue()
        reservation = self._reserve(token)
        self.assertIsNotNone(reservation)
        assert reservation is not None

        self.assertNotIn(token, repr(reservation))
        self.assertNotIn(reservation.reservation_token, repr(reservation))
        self.assertTrue(self.manager.commit(reservation))
        self.assertFalse(self.manager.commit(reservation))
        self.assertFalse(self.manager.release(reservation))
        self.assertIsNone(self._reserve(token))

    def test_release_after_original_expiry_does_not_restore_grant(self) -> None:
        token = self._issue()
        reservation = self._reserve(token)
        self.assertIsNotNone(reservation)
        self.now += timedelta(seconds=61)

        self.assertTrue(self.manager.release(reservation))
        self.assertIsNone(self._reserve(token))

    def test_reserved_grants_remain_inside_capacity_bound(self) -> None:
        manager = DeviceActionGrantManager(
            b"device-grant-capacity-test-secret-32b",
            max_grants=1,
            clock=lambda: NOW,
        )
        issued = manager.issue(self.verified)
        reservation = manager.reserve(
            issued.token,
            license_id=self.verified.license_id,
            action=self.verified.action,
            resource_id=self.verified.resource_id,
        )
        self.assertIsNotNone(reservation)

        with self.assertRaises(AuthenticationCapacityError):
            manager.issue(self.verified)


class AdminLoginRateLimitTests(unittest.TestCase):
    def test_password_budget_is_account_global_across_source_ips(self):
        app = _login_test_app()
        statuses = []
        with patch.object(
            AdminPasswordCredential,
            "verify",
            autospec=True,
            return_value=False,
        ) as verify:
            for index in range(6):
                with TestClient(
                    app,
                    client=(f"198.51.100.{index + 1}", 50_000 + index),
                ) as client:
                    statuses.append(
                        client.post(
                            "/api/admin/session",
                            json={
                                "subject": "admin:test",
                                "password": "incorrect",
                            },
                        ).status_code
                    )
        self.assertEqual(statuses, [401, 401, 401, 401, 401, 429])
        self.assertEqual(verify.call_count, 5)

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

    def test_malformed_multipart_releases_real_payment_grant_for_retry(self):
        app = _login_test_app()
        license_id = "lic_test_001"
        release_id = "rel_test_001"
        verified = _verified_payment_grant(
            license_id=license_id,
            release_id=release_id,
        )
        device_grant = app.state.device_grants.issue(verified).token
        headers = {
            "Content-Type": "multipart/form-data; boundary=broken",
            "X-Device-Grant": device_grant,
        }
        malformed_body = b"x" * (300 * 1024)

        with TestClient(app) as client:
            first = client.post(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                content=malformed_body,
                headers=headers,
            )
            retry = client.post(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                content=malformed_body,
                headers=headers,
            )

        self.assertNotEqual(first.status_code, 413)
        self.assertEqual(retry.status_code, first.status_code)

    def test_invalid_mime_and_corrupt_image_release_grant_for_retry(self):
        evidence = Mock(spec=PrivatePaymentEvidenceStore)
        evidence.store_payment_screenshot.side_effect = (
            PrivateStorageValidationError("private parser detail")
        )
        app = _login_test_app(evidence_store=evidence)
        verified = _verified_payment_grant()
        path = (
            f"/api/customer/licenses/{verified.license_id}/releases/"
            f"{verified.resource_id}/payments"
        )

        mime_grant = app.state.device_grants.issue(verified).token
        with TestClient(app) as client:
            invalid_mime = [
                client.post(
                    path,
                    headers={"X-Device-Grant": mime_grant},
                    data={
                        "paid_at": "2026-07-13T03:00:00Z",
                        "client_submission_id": "submission-mime-001",
                    },
                    files={"file": ("proof.bin", b"not-image", "application/pdf")},
                )
                for _ in range(2)
            ]

            corrupt_grant = app.state.device_grants.issue(verified).token
            corrupt = [
                client.post(
                    path,
                    headers={"X-Device-Grant": corrupt_grant},
                    data={
                        "paid_at": "2026-07-13T03:00:00Z",
                        "client_submission_id": "submission-corrupt-001",
                    },
                    files={"file": ("proof.png", b"not-a-png", "image/png")},
                )
                for _ in range(2)
            ]

        self.assertEqual([item.status_code for item in invalid_mime], [400, 400])
        self.assertEqual([item.status_code for item in corrupt], [400, 400])
        self.assertEqual(evidence.store_payment_screenshot.call_count, 2)

    def test_valid_submission_commits_grant_before_replay(self):
        commerce = Mock(spec=CommerceRepository)
        evidence = Mock(spec=PrivatePaymentEvidenceStore)
        evidence.store_payment_screenshot.return_value = PrivateObjectMetadata(
            "payments/test/proof.png",
            "a" * 64,
            9,
            "image/png",
            "2026-07-13T03:00:00.000000Z",
        )
        verified = _verified_payment_grant()
        commerce.submit_payment.return_value = PaymentSubmission(
            "pay_test_001",
            verified.license_id,
            verified.resource_id,
            100_000,
            "UZS",
            "payments/test/proof.png",
            "a" * 64,
            9,
            "image/png",
            "2026-07-13T03:00:00.000000Z",
            "2026-07-13T03:00:00.000000Z",
            PaymentState.PENDING,
            None,
            None,
            None,
            None,
            None,
            "submission-valid-001",
            None,
        )
        app = _login_test_app(commerce=commerce, evidence_store=evidence)
        grant = app.state.device_grants.issue(verified).token
        path = (
            f"/api/customer/licenses/{verified.license_id}/releases/"
            f"{verified.resource_id}/payments"
        )
        request = {
            "headers": {"X-Device-Grant": grant},
            "data": {
                "paid_at": "2026-07-13T03:00:00Z",
                "client_submission_id": "submission-valid-001",
            },
            "files": {"file": ("proof.png", b"valid-png", "image/png")},
        }

        with TestClient(app) as client:
            accepted = client.post(path, **request)
            replay = client.post(path, **request)

        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(replay.status_code, 401, replay.text)
        self.assertEqual(evidence.store_payment_screenshot.call_count, 1)
        self.assertEqual(commerce.submit_payment.call_count, 1)

    def test_streamed_oversize_releases_reservation(self):
        manager = DeviceActionGrantManager(
            b"streamed-upload-reservation-secret-32b",
            clock=lambda: NOW,
        )
        verified = _verified_payment_grant()
        token = manager.issue(verified).token

        async def consume_body(scope, receive, send):
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = _BoundedBodyMiddleware(
            consume_body,
            default_maximum_bytes=64,
            payment_maximum_bytes=128,
            payment_grants=manager,
        )

        async def request(chunks, *, declared_length=None):
            messages = [
                {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
                for index, chunk in enumerate(chunks)
            ]
            responses = []

            async def receive():
                return messages.pop(0)

            async def send(message):
                responses.append(message)

            headers = [
                (b"content-type", b"multipart/form-data; boundary=test"),
                (b"x-device-grant", token.encode("ascii")),
            ]
            if declared_length is not None:
                headers.append(
                    (b"content-length", str(declared_length).encode("ascii"))
                )
            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": (
                        f"/api/customer/licenses/{verified.license_id}/releases/"
                        f"{verified.resource_id}/payments"
                    ),
                    "headers": tuple(headers),
                    "state": {},
                },
                receive,
                send,
            )
            return responses

        declared = asyncio.run(request((b"ignored",), declared_length=129))
        oversized = asyncio.run(request((b"a" * 80, b"b" * 80)))
        retried = asyncio.run(request((b"c" * 48, b"d" * 48)))

        self.assertEqual(declared[0]["status"], 413)
        self.assertEqual(oversized[0]["status"], 413)
        self.assertEqual(retried[0]["status"], 204)

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

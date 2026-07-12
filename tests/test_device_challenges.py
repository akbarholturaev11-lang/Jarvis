from __future__ import annotations

import base64
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import verify_device_challenge
from product_backend.device_challenges import (
    STATUS_ALREADY_USED,
    STATUS_EXPIRED,
    STATUS_INVALID,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    DeviceChallengeAction,
    SQLiteDeviceChallengeService,
)
from product_backend.models import (
    ArtifactVerificationReceipt,
    InstallAuthorization,
    InstallDecisionReason,
    InstallMode,
)
from product_backend.sqlite_repository import SQLiteCommerceRepository


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class ThreadSafeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.value

    def advance(self, **kwargs: int) -> None:
        with self.lock:
            self.value += timedelta(**kwargs)


class ReceiptVerifier:
    def verify(self, candidate):
        return ArtifactVerificationReceipt(
            "2026-07-13T02:00:00Z", candidate.signing_key_id
        )


class DeviceChallengeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.commerce = SQLiteCommerceRepository(
            root / "commerce.sqlite3", artifact_verifier=ReceiptVerifier()
        )
        account = self.commerce.create_account("buyer:challenge-001")
        self.license = self.commerce.issue_license(account.id)
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key = _b64url(public_bytes)
        import hashlib

        self.fingerprint = "sha256:" + hashlib.sha256(public_bytes).hexdigest()
        self.commerce.activate_device(
            self.license.id,
            self.fingerprint,
            platform="macos",
            architecture="arm64",
        )
        self.clock = ThreadSafeClock()
        self.database = root / "challenges.sqlite3"
        self.service = SQLiteDeviceChallengeService(
            self.commerce,
            self.database,
            clock=self.clock,
            ttl_seconds=60,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.commerce.close()
        self.temp.cleanup()

    def _issue(self, resource_id: str = "art_0123456789abcdef"):
        result = self.service.issue(
            license_id=self.license.id,
            device_key_fingerprint=self.fingerprint,
            action=DeviceChallengeAction.AUTHORIZE_INSTALL,
            resource_id=resource_id,
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        return result.issued

    def _signature(self, nonce: str, private_key=None) -> str:
        key = self.private_key if private_key is None else private_key
        raw = base64.urlsafe_b64decode(nonce + "=")
        signature = key.sign(b"jarvis.device.challenge.v1\x00" + raw)
        return _b64url(signature)

    def test_success_is_bound_to_license_action_resource_and_active_target(self):
        issued = self._issue()
        result = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=issued.challenge_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(issued.challenge_nonce),
        )

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(result.ok)
        verified = result.verified
        self.assertEqual(verified.license_id, self.license.id)
        self.assertEqual(verified.action, DeviceChallengeAction.AUTHORIZE_INSTALL)
        self.assertEqual(verified.resource_id, "art_0123456789abcdef")
        self.assertTrue(verified.device_principal.proof_verified)
        self.assertEqual(verified.device_principal.platform, "macos")

    def test_combined_install_boundary_uses_only_server_verified_principal(self):
        artifact_id = "art_0123456789abcdef"
        issued = self._issue(artifact_id)
        expected = InstallAuthorization(
            True, InstallDecisionReason.AUTHORIZED
        )
        with mock.patch.object(
            self.commerce, "authorize_install", return_value=expected
        ) as authorize:
            result = self.service.verify_and_authorize_install(
                challenge_id=issued.id,
                challenge_nonce=issued.challenge_nonce,
                public_key_base64=self.public_key,
                signature_base64=self._signature(issued.challenge_nonce),
                artifact_id=artifact_id,
                install_mode=InstallMode.FRESH_INSTALL,
            )

        self.assertTrue(result.ok)
        kwargs = authorize.call_args.kwargs
        self.assertTrue(kwargs["device_principal"].proof_verified)
        self.assertEqual(kwargs["artifact_id"], artifact_id)

    def test_combined_install_boundary_rejects_resource_substitution(self):
        issued = self._issue("art_0123456789abcdef")
        with mock.patch.object(self.commerce, "authorize_install") as authorize:
            result = self.service.verify_and_authorize_install(
                challenge_id=issued.id,
                challenge_nonce=issued.challenge_nonce,
                public_key_base64=self.public_key,
                signature_base64=self._signature(issued.challenge_nonce),
                artifact_id="art_fedcba9876543210",
                install_mode=InstallMode.FRESH_INSTALL,
            )

        self.assertEqual(result.status, STATUS_INVALID)
        authorize.assert_not_called()

    def test_replay_is_rejected_atomically(self):
        issued = self._issue()
        kwargs = {
            "challenge_id": issued.id,
            "challenge_nonce": issued.challenge_nonce,
            "public_key_base64": self.public_key,
            "signature_base64": self._signature(issued.challenge_nonce),
        }

        first = self.service.verify_and_consume(**kwargs)
        second = self.service.verify_and_consume(**kwargs)

        self.assertEqual(first.status, STATUS_SUCCESS)
        self.assertEqual(second.status, STATUS_ALREADY_USED)

    def test_invalid_signature_consumes_challenge(self):
        issued = self._issue()
        wrong = Ed25519PrivateKey.generate()

        invalid = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=issued.challenge_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(issued.challenge_nonce, wrong),
        )
        replay = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=issued.challenge_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(issued.challenge_nonce),
        )

        self.assertEqual(invalid.status, STATUS_INVALID)
        self.assertEqual(replay.status, STATUS_ALREADY_USED)

    def test_wrong_encoded_key_or_signature_length_is_not_decoded_and_is_consumed(self):
        invalid_fields = {
            "public_key_base64": self.public_key + "A",
            "signature_base64": "A" * 87,
        }
        for field, invalid_value in invalid_fields.items():
            with self.subTest(field=field):
                issued = self._issue(f"art_{field}_0123456789abcdef")
                valid = {
                    "challenge_id": issued.id,
                    "challenge_nonce": issued.challenge_nonce,
                    "public_key_base64": self.public_key,
                    "signature_base64": self._signature(issued.challenge_nonce),
                }
                attempted = valid | {field: invalid_value}
                with mock.patch(
                    "product_backend.device_challenges.verify_device_challenge"
                ) as crypto_verify:
                    invalid = self.service.verify_and_consume(**attempted)
                replay = self.service.verify_and_consume(**valid)

                self.assertEqual(invalid.status, STATUS_INVALID)
                crypto_verify.assert_not_called()
                self.assertEqual(replay.status, STATUS_ALREADY_USED)

    def test_expired_challenge_is_consumed(self):
        issued = self._issue()
        self.clock.advance(seconds=61)

        expired = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=issued.challenge_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(issued.challenge_nonce),
        )

        self.assertEqual(expired.status, STATUS_EXPIRED)

    def test_wrong_nonce_unknown_challenge_and_wrong_binding_fail_closed(self):
        issued = self._issue()
        wrong_nonce = _b64url(b"x" * 32)
        wrong = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=wrong_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(wrong_nonce),
        )
        missing = self.service.verify_and_consume(
            challenge_id="chl_0123456789abcdef0123456789abcdef",
            challenge_nonce=wrong_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(wrong_nonce),
        )
        other_fingerprint = "sha256:" + ("f" * 64)
        no_binding = self.service.issue(
            license_id=self.license.id,
            device_key_fingerprint=other_fingerprint,
            action=DeviceChallengeAction.SUBMIT_PAYMENT,
            resource_id="rel_0123456789abcdef",
        )

        self.assertEqual(wrong.status, STATUS_INVALID)
        self.assertEqual(missing.status, STATUS_NOT_FOUND)
        self.assertEqual(no_binding.status, STATUS_NOT_FOUND)

    def test_device_replacement_between_issue_and_verify_invalidates_proof(self):
        issued = self._issue()
        replacement_key = Ed25519PrivateKey.generate()
        replacement_public = replacement_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        import hashlib

        replacement_fingerprint = (
            "sha256:" + hashlib.sha256(replacement_public).hexdigest()
        )
        self.commerce.replace_device(
            self.license.id,
            current_device_key_fingerprint=self.fingerprint,
            new_device_key_fingerprint=replacement_fingerprint,
            new_platform="windows",
            new_architecture="x86_64",
            replacement_reason="Owner-approved replacement",
        )

        result = self.service.verify_and_consume(
            challenge_id=issued.id,
            challenge_nonce=issued.challenge_nonce,
            public_key_base64=self.public_key,
            signature_base64=self._signature(issued.challenge_nonce),
        )

        self.assertEqual(result.status, STATUS_INVALID)

    def test_two_service_instances_allow_only_one_success(self):
        issued = self._issue()
        second = SQLiteDeviceChallengeService(
            self.commerce,
            self.database,
            clock=self.clock,
            ttl_seconds=60,
        )
        kwargs = {
            "challenge_id": issued.id,
            "challenge_nonce": issued.challenge_nonce,
            "public_key_base64": self.public_key,
            "signature_base64": self._signature(issued.challenge_nonce),
        }
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    future.result(timeout=5)
                    for future in (
                        executor.submit(self.service.verify_and_consume, **kwargs),
                        executor.submit(second.verify_and_consume, **kwargs),
                    )
                ]
        finally:
            second.close()

        self.assertEqual(
            {result.status for result in results},
            {STATUS_SUCCESS, STATUS_ALREADY_USED},
        )

    def test_database_never_stores_raw_nonce_key_or_signature(self):
        issued = self._issue()
        signature = self._signature(issued.challenge_nonce)
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute("SELECT * FROM device_challenges").fetchone()
            rendered = repr(row)
        finally:
            connection.close()

        self.assertNotIn(issued.challenge_nonce, rendered)
        self.assertNotIn(self.public_key, rendered)
        self.assertNotIn(signature, rendered)
        self.assertNotIn(issued.challenge_nonce, repr(issued))

    def test_crypto_primitive_matches_core_verifier(self):
        issued = self._issue()
        proof = verify_device_challenge(
            public_key_base64=self.public_key,
            device_key_fingerprint=self.fingerprint,
            challenge_nonce=issued.challenge_nonce,
            signature_base64=self._signature(issued.challenge_nonce),
        )
        self.assertTrue(proof.ok)


if __name__ == "__main__":
    unittest.main()

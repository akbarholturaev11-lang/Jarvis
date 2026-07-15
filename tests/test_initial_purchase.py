from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentity
from product_backend.initial_purchase import (
    DEFAULT_GRANT_TTL_SECONDS,
    STATUS_INVALID,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    InitialPurchaseAuthorizer,
)


FINGERPRINT = "sha256:" + ("a" * 64)
PURCHASE_ID = "purchase_" + ("1" * 32)
RELEASE_ID = "rel_initial_001"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class InitialPurchaseAuthorizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.identity = DeviceIdentity(Ed25519PrivateKey.generate())
        self.authority = InitialPurchaseAuthorizer(
            b"a" * 32,
            clock=self.clock,
        )

    def _grant(self):
        issued = self.authority.issue_challenge(
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
            device_key_fingerprint=self.identity.fingerprint,
            platform="macos",
            architecture="arm64",
        )
        self.assertEqual(issued.status, STATUS_SUCCESS)
        assert issued.challenge is not None
        verified = self.authority.verify_and_issue_grant(
            challenge_id=issued.challenge.id,
            challenge_nonce=issued.challenge.challenge_nonce,
            public_key_base64=self.identity.public_key_base64,
            signature_base64=self.identity.sign_challenge(
                issued.challenge.challenge_nonce
            ),
        )
        self.assertEqual(verified.status, STATUS_SUCCESS)
        assert verified.grant is not None
        return issued, verified.grant

    def test_device_proof_grant_reserve_release_commit_and_replay(self) -> None:
        issued, grant = self._grant()
        self.assertNotIn(issued.challenge.challenge_nonce, repr(issued))
        self.assertNotIn(grant.token, repr(grant))
        reservation = self.authority.reserve_grant(
            grant.token,
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
        )
        self.assertIsNotNone(reservation)
        self.assertIsNone(
            self.authority.reserve_grant(
                grant.token,
                purchase_id=PURCHASE_ID,
                release_id=RELEASE_ID,
            )
        )
        self.assertTrue(self.authority.release_grant(reservation))
        retried = self.authority.reserve_grant(
            grant.token,
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
        )
        self.assertIsNotNone(retried)
        self.assertTrue(self.authority.commit_grant(retried))
        self.assertIsNone(
            self.authority.reserve_grant(
                grant.token,
                purchase_id=PURCHASE_ID,
                release_id=RELEASE_ID,
            )
        )

    def test_reserved_grant_commits_after_expiry_and_blocks_replay(self) -> None:
        # Grant expiry gates admission at reserve time.  Once a request has
        # reserved a grant, a slow or large upload that pushes the wall clock
        # past the grant TTL must not turn the closing commit -- which runs only
        # after the payment row is durably persisted -- into a failure.  This is
        # the P1 race: a spurious 503 for an accepted, stored payment.
        _, grant = self._grant()
        reservation = self.authority.reserve_grant(
            grant.token,
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
        )
        self.assertIsNotNone(reservation)
        # The upload has taken longer than the grant TTL.
        self.clock.value += timedelta(seconds=DEFAULT_GRANT_TTL_SECONDS + 60)
        # Persistence succeeded, so the commit must be deterministic.
        self.assertTrue(self.authority.commit_grant(reservation))
        # The one-time grant is consumed and cannot be replayed afterwards.
        self.assertIsNone(
            self.authority.reserve_grant(
                grant.token,
                purchase_id=PURCHASE_ID,
                release_id=RELEASE_ID,
            )
        )

    def test_released_grant_expired_during_request_cannot_be_reused(self) -> None:
        # Releasing a reservation whose grant expired mid-request returns the
        # hold, but the now-expired grant must then be pruned so it can never be
        # admitted again -- expiry still fails closed for a fresh request.
        _, grant = self._grant()
        reservation = self.authority.reserve_grant(
            grant.token,
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
        )
        self.assertIsNotNone(reservation)
        self.clock.value += timedelta(seconds=DEFAULT_GRANT_TTL_SECONDS + 60)
        self.assertTrue(self.authority.release_grant(reservation))
        self.assertIsNone(
            self.authority.reserve_grant(
                grant.token,
                purchase_id=PURCHASE_ID,
                release_id=RELEASE_ID,
            )
        )

    def test_invalid_signature_and_challenge_replay_grant_nothing(self) -> None:
        issued = self.authority.issue_challenge(
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
            device_key_fingerprint=self.identity.fingerprint,
            platform="linux",
            architecture="x86_64",
        )
        assert issued.challenge is not None
        wrong = DeviceIdentity(Ed25519PrivateKey.generate())
        invalid = self.authority.verify_and_issue_grant(
            challenge_id=issued.challenge.id,
            challenge_nonce=issued.challenge.challenge_nonce,
            public_key_base64=wrong.public_key_base64,
            signature_base64=wrong.sign_challenge(issued.challenge.challenge_nonce),
        )
        self.assertEqual(invalid.status, STATUS_INVALID)
        replay = self.authority.verify_and_issue_grant(
            challenge_id=issued.challenge.id,
            challenge_nonce=issued.challenge.challenge_nonce,
            public_key_base64=self.identity.public_key_base64,
            signature_base64=self.identity.sign_challenge(
                issued.challenge.challenge_nonce
            ),
        )
        self.assertEqual(replay.status, STATUS_NOT_FOUND)

    def test_wrong_context_preserves_grant_and_parallel_reserve_has_one_winner(self):
        _, wrong_context_grant = self._grant()
        self.assertIsNone(
            self.authority.reserve_grant(
                wrong_context_grant.token,
                purchase_id=PURCHASE_ID,
                release_id="rel_other_001",
            )
        )
        reservation = self.authority.reserve_grant(
            wrong_context_grant.token,
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
        )
        self.assertIsNotNone(reservation)
        self.assertTrue(self.authority.commit_grant(reservation))

        _, grant = self._grant()
        outcomes = []
        lock = threading.Lock()

        def reserve() -> None:
            result = self.authority.reserve_grant(
                grant.token,
                purchase_id=PURCHASE_ID,
                release_id=RELEASE_ID,
            )
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in outcomes), 1)

    def test_expired_challenge_and_grant_fail_closed(self) -> None:
        issued = self.authority.issue_challenge(
            purchase_id=PURCHASE_ID,
            release_id=RELEASE_ID,
            device_key_fingerprint=self.identity.fingerprint,
            platform="windows",
            architecture="x86_64",
        )
        assert issued.challenge is not None
        self.clock.value += timedelta(seconds=121)
        expired = self.authority.verify_and_issue_grant(
            challenge_id=issued.challenge.id,
            challenge_nonce=issued.challenge.challenge_nonce,
            public_key_base64=self.identity.public_key_base64,
            signature_base64=self.identity.sign_challenge(
                issued.challenge.challenge_nonce
            ),
        )
        self.assertIn(expired.status, {"expired", "not_found"})


if __name__ == "__main__":
    unittest.main()

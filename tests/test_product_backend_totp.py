from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from product_backend.admin_mfa import (
    AdminMfaSettings,
    LoginFactorResult,
    MfaAuditEvent,
    MfaConfigurationError,
    MfaCryptoError,
    MfaSecretCipher,
    MfaState,
    MfaStateError,
    SQLiteAdminMfaManager,
)
from product_backend.api_totp import (
    base32_secret,
    decode_base32_secret,
    generate_recovery_code,
    generate_totp_secret,
    normalize_recovery_code,
    provisioning_uri,
    totp_code,
    verify_totp,
)


PERIOD = 30
BASE = 1_700_000_000  # aligned to a step boundary for deterministic tests


class TotpAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = generate_totp_secret()

    def test_valid_code_is_accepted_and_returns_its_step(self) -> None:
        code = totp_code(self.secret, BASE)
        matched = verify_totp(self.secret, code, timestamp=BASE)
        self.assertEqual(matched, BASE // PERIOD)

    def test_invalid_code_is_rejected(self) -> None:
        code = totp_code(self.secret, BASE)
        wrong = "000000" if code != "000000" else "111111"
        self.assertIsNone(verify_totp(self.secret, wrong, timestamp=BASE))
        self.assertIsNone(verify_totp(self.secret, "12345", timestamp=BASE))
        self.assertIsNone(verify_totp(self.secret, "abcdef", timestamp=BASE))
        self.assertIsNone(verify_totp(self.secret, None, timestamp=BASE))

    def test_expired_code_outside_window_is_rejected(self) -> None:
        code = totp_code(self.secret, BASE)
        # Two full steps later the code is neither current nor within ±1 drift.
        self.assertIsNone(
            verify_totp(self.secret, code, timestamp=BASE + 2 * PERIOD)
        )

    def test_bounded_clock_drift_is_accepted(self) -> None:
        previous = totp_code(self.secret, BASE - PERIOD)
        upcoming = totp_code(self.secret, BASE + PERIOD)
        self.assertIsNotNone(verify_totp(self.secret, previous, timestamp=BASE))
        self.assertIsNotNone(verify_totp(self.secret, upcoming, timestamp=BASE))
        # Two steps of drift is out of range.
        two_back = totp_code(self.secret, BASE - 2 * PERIOD)
        self.assertIsNone(verify_totp(self.secret, two_back, timestamp=BASE))

    def test_secret_round_trips_through_base32(self) -> None:
        self.assertEqual(decode_base32_secret(base32_secret(self.secret)), self.secret)

    def test_provisioning_uri_hides_no_metadata_but_encodes_secret(self) -> None:
        uri = provisioning_uri(
            self.secret, account_name="admin:test", issuer="JARVIS Admin"
        )
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={base32_secret(self.secret)}", uri)
        self.assertIn("algorithm=SHA1", uri)

    def test_recovery_code_normalization(self) -> None:
        code = generate_recovery_code()
        self.assertRegex(code, r"^[0-9A-Z]{5}-[0-9A-Z]{5}$")
        normalized = normalize_recovery_code(code)
        self.assertEqual(normalized, code.replace("-", ""))
        self.assertEqual(normalize_recovery_code(code.lower()), normalized)
        self.assertIsNone(normalize_recovery_code("short"))
        self.assertIsNone(normalize_recovery_code("111I1-OOOO0"))  # ambiguous chars


class MfaSecretCipherTests(unittest.TestCase):
    def test_seal_open_round_trip_and_aad_binding(self) -> None:
        cipher = MfaSecretCipher(b"k" * 32)
        secret = generate_totp_secret()
        nonce, ciphertext = cipher.seal_secret("admin:one", secret)
        self.assertEqual(cipher.open_secret("admin:one", nonce, ciphertext), secret)
        # A different subject is bound into the AAD and must fail authentication.
        with self.assertRaises(MfaCryptoError):
            cipher.open_secret("admin:two", nonce, ciphertext)

    def test_wrong_master_key_cannot_decrypt(self) -> None:
        secret = generate_totp_secret()
        nonce, ciphertext = MfaSecretCipher(b"a" * 32).seal_secret("s", secret)
        with self.assertRaises(MfaCryptoError):
            MfaSecretCipher(b"b" * 32).open_secret("s", nonce, ciphertext)

    def test_missing_or_short_master_key_is_fail_closed(self) -> None:
        for bad in (b"", b"tooshort", b"x" * 31, "not-bytes", None):
            with self.subTest(bad=bad):
                with self.assertRaises(MfaConfigurationError):
                    MfaSecretCipher(bad)  # type: ignore[arg-type]

    def test_stored_ciphertext_never_equals_plaintext(self) -> None:
        cipher = MfaSecretCipher(b"k" * 32)
        secret = generate_totp_secret()
        _, ciphertext = cipher.seal_secret("admin:one", secret)
        self.assertNotIn(base32_secret(secret), ciphertext)


class AdminMfaManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = {"t": datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)}
        self.manager = SQLiteAdminMfaManager(
            MfaSecretCipher(b"m" * 32),
            ":memory:",
            settings=AdminMfaSettings(mandatory=True),
            clock=lambda: self.clock["t"],
        )
        self.subject = "admin:test"

    def _secret(self, start) -> bytes:
        return decode_base32_secret(start.secret_base32)

    def _activate(self):
        start = self.manager.begin_enrollment(self.subject)
        secret = self._secret(start)
        code = totp_code(secret, self.clock["t"].timestamp())
        batch = self.manager.activate_enrollment(self.subject, code)
        return secret, batch

    def test_enrollment_incomplete_stays_pending(self) -> None:
        self.manager.begin_enrollment(self.subject)
        self.assertIs(self.manager.state(self.subject), MfaState.ENROLLING)
        self.assertFalse(self.manager.is_active(self.subject))
        # A wrong first code does not activate.
        self.assertIsNone(self.manager.activate_enrollment(self.subject, "000000"))
        self.assertIs(self.manager.state(self.subject), MfaState.ENROLLING)

    def test_enrollment_complete_activates_and_issues_codes(self) -> None:
        _, batch = self._activate()
        self.assertIs(self.manager.state(self.subject), MfaState.ACTIVE)
        self.assertEqual(len(batch.codes), 10)
        self.assertEqual(len(set(batch.codes)), 10)

    def test_totp_replay_same_step_is_rejected(self) -> None:
        secret, _ = self._activate()
        self.clock["t"] += timedelta(seconds=PERIOD)
        code = totp_code(secret, self.clock["t"].timestamp())
        self.assertIs(
            self.manager.verify_login_totp(self.subject, code),
            LoginFactorResult.ACCEPTED,
        )
        self.assertIs(
            self.manager.verify_login_totp(self.subject, code),
            LoginFactorResult.REPLAY,
        )

    def test_recovery_code_is_single_use(self) -> None:
        _, batch = self._activate()
        code = batch.codes[0]
        self.assertIs(
            self.manager.verify_recovery_code(self.subject, code),
            LoginFactorResult.ACCEPTED,
        )
        self.assertIs(
            self.manager.verify_recovery_code(self.subject, code),
            LoginFactorResult.INVALID,
        )

    def test_regenerate_revokes_previous_recovery_codes(self) -> None:
        _, batch = self._activate()
        fresh = self.manager.regenerate_recovery_codes(self.subject)
        self.assertIs(
            self.manager.verify_recovery_code(self.subject, batch.codes[1]),
            LoginFactorResult.INVALID,
        )
        self.assertIs(
            self.manager.verify_recovery_code(self.subject, fresh.codes[0]),
            LoginFactorResult.ACCEPTED,
        )

    def test_disable_ends_active_mfa(self) -> None:
        self._activate()
        self.manager.disable(self.subject)
        self.assertIs(self.manager.state(self.subject), MfaState.DISABLED)
        self.assertIs(
            self.manager.verify_login_totp(self.subject, "123456"),
            LoginFactorResult.NOT_ACTIVE,
        )

    def test_reset_clears_record(self) -> None:
        self._activate()
        self.manager.disable(self.subject, reset=True)
        self.assertIs(self.manager.state(self.subject), MfaState.NOT_ENROLLED)

    def test_already_active_cannot_restart_enrollment(self) -> None:
        self._activate()
        with self.assertRaises(MfaStateError):
            self.manager.begin_enrollment(self.subject)

    def test_audit_records_events_without_secret_leakage(self) -> None:
        start = self.manager.begin_enrollment(self.subject)
        secret = self._secret(start)
        self.manager.activate_enrollment(
            self.subject, totp_code(secret, self.clock["t"].timestamp())
        )
        self.clock["t"] += timedelta(seconds=PERIOD)
        self.manager.verify_login_totp(self.subject, "000000")  # failure
        events = {e.event for e in self.manager.list_audit(subject=self.subject)}
        self.assertIn(MfaAuditEvent.ENROLLMENT_STARTED, events)
        self.assertIn(MfaAuditEvent.ENROLLMENT_COMPLETED, events)
        self.assertIn(MfaAuditEvent.TOTP_FAILURE, events)
        details = " ".join(
            str(e.detail) for e in self.manager.list_audit(subject=self.subject)
        )
        self.assertNotIn(start.secret_base32, details)


if __name__ == "__main__":
    unittest.main()

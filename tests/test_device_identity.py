from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.device_identity import (
    CHALLENGE_NONCE_MAX_BYTES,
    CHALLENGE_NONCE_MIN_BYTES,
    DEVICE_IDENTITY_ACCOUNT,
    DEVICE_IDENTITY_SERVICE,
    STATUS_FAILED,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    DeviceIdentityManager,
    verify_device_challenge,
)
from core.secure_store import (
    STATUS_FAILED as STORE_STATUS_FAILED,
    STATUS_NOT_AVAILABLE as STORE_STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND as STORE_STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class MemorySecureStore(SecureStore):
    def __init__(
        self,
        value: str | None = None,
        *,
        get_status: str | None = None,
        set_status: str = STORE_STATUS_SUCCESS,
        corrupt_after_set: bool = False,
    ) -> None:
        self.value = value
        self.get_status = get_status
        self.set_status = set_status
        self.corrupt_after_set = corrupt_after_set
        self.get_count = 0
        self.set_count = 0
        self.last_service: str | None = None
        self.last_account: str | None = None

    def _get(self, service: str, account: str) -> SecureStoreResult:
        self.get_count += 1
        self.last_service = service
        self.last_account = account
        if self.get_status is not None:
            return SecureStoreResult(self.get_status, message="mocked get")
        if self.value is None:
            return SecureStoreResult(STORE_STATUS_NOT_FOUND, message="missing")
        return SecureStoreResult(
            STORE_STATUS_SUCCESS,
            value=self.value,
            message="stored value",
        )

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.set_count += 1
        self.last_service = service
        self.last_account = account
        if self.set_status != STORE_STATUS_SUCCESS:
            return SecureStoreResult(self.set_status, message="mocked set")
        self.value = "corrupted-after-set" if self.corrupt_after_set else secret
        return SecureStoreResult(STORE_STATUS_SUCCESS, message="stored")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        self.last_service = service
        self.last_account = account
        if self.value is None:
            return SecureStoreResult(STORE_STATUS_NOT_FOUND, message="missing")
        self.value = None
        return SecureStoreResult(STORE_STATUS_SUCCESS, message="deleted")


class DeviceIdentityStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.lock_path = str(Path(self.temp.name) / "device-identity.lock")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manager(self, store: SecureStore) -> DeviceIdentityManager:
        return DeviceIdentityManager(store, creation_lock_path=self.lock_path)

    def test_load_not_found_then_create_and_stable_reload(self):
        store = MemorySecureStore()
        manager = self._manager(store)

        missing = manager.load()
        created = manager.get_or_create()
        reloaded = self._manager(store).load()

        self.assertEqual(missing.status, STATUS_NOT_FOUND)
        self.assertEqual(created.status, STATUS_SUCCESS)
        self.assertEqual(reloaded.status, STATUS_SUCCESS)
        self.assertTrue(created.ok)
        self.assertEqual(created.identity.fingerprint, reloaded.identity.fingerprint)
        self.assertEqual(
            created.identity.public_key_bytes,
            reloaded.identity.public_key_bytes,
        )
        self.assertEqual(store.set_count, 1)
        self.assertGreaterEqual(store.get_count, 3)
        self.assertEqual(store.last_service, DEVICE_IDENTITY_SERVICE)
        self.assertEqual(store.last_account, DEVICE_IDENTITY_ACCOUNT)

    def test_private_key_is_canonical_unpadded_base64url_raw_32_bytes(self):
        store = MemorySecureStore()

        result = self._manager(store).get_or_create()

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertIsInstance(store.value, str)
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]{43}", store.value))
        self.assertNotIn("=", store.value)
        self.assertEqual(len(_decode_b64url(store.value)), 32)

    def test_public_identity_is_sha256_of_raw_public_key(self):
        result = self._manager(MemorySecureStore()).get_or_create()
        identity = result.identity

        expected = "sha256:" + hashlib.sha256(identity.public_key_bytes).hexdigest()
        self.assertEqual(identity.fingerprint, expected)
        self.assertRegex(identity.fingerprint, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            _decode_b64url(identity.public_key_base64),
            identity.public_key_bytes,
        )
        with self.assertRaises(AttributeError):
            identity.fingerprint = "sha256:" + ("0" * 64)
        self.assertFalse(hasattr(identity, "private_key"))
        self.assertFalse(hasattr(identity, "private_key_bytes"))
        self.assertNotIn(identity.fingerprint, repr(identity))

    def test_set_failure_never_claims_creation_success(self):
        store = MemorySecureStore(set_status=STORE_STATUS_FAILED)

        result = self._manager(store).get_or_create()

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.identity)
        self.assertIsNone(store.value)
        self.assertEqual(store.set_count, 1)

    def test_unavailable_store_is_honest_and_never_attempts_set(self):
        store = MemorySecureStore(get_status=STORE_STATUS_NOT_AVAILABLE)
        manager = self._manager(store)

        loaded = manager.load()
        created = manager.get_or_create()

        self.assertEqual(loaded.status, STATUS_NOT_AVAILABLE)
        self.assertEqual(created.status, STATUS_NOT_AVAILABLE)
        self.assertEqual(store.set_count, 0)

    def test_creation_requires_interprocess_coordination(self):
        store = MemorySecureStore()

        result = DeviceIdentityManager(store).get_or_create()

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
        self.assertEqual(store.get_count, 0)
        self.assertEqual(store.set_count, 0)

    def test_store_failure_is_failed_not_not_found(self):
        store = MemorySecureStore(get_status=STORE_STATUS_FAILED)

        result = self._manager(store).load()

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.identity)

    def test_corrupted_existing_key_fails_closed_without_overwrite(self):
        corrupted = "not+canonical-base64"
        store = MemorySecureStore(value=corrupted)

        loaded = self._manager(store).load()
        get_or_created = self._manager(store).get_or_create()

        self.assertEqual(loaded.status, STATUS_INVALID)
        self.assertEqual(get_or_created.status, STATUS_INVALID)
        self.assertEqual(store.value, corrupted)
        self.assertEqual(store.set_count, 0)

    def test_successful_set_is_not_success_until_reread_parses(self):
        store = MemorySecureStore(corrupt_after_set=True)

        result = self._manager(store).get_or_create()

        self.assertEqual(result.status, STATUS_INVALID)
        self.assertIsNone(result.identity)
        self.assertEqual(store.set_count, 1)
        self.assertEqual(store.get_count, 2)

    def test_crypto_unavailable_returns_not_available_without_store_access(self):
        store = MemorySecureStore()
        with mock.patch("core.device_identity._ED25519_AVAILABLE", False):
            loaded = self._manager(store).load()
            created = self._manager(store).get_or_create()

        self.assertEqual(loaded.status, STATUS_NOT_AVAILABLE)
        self.assertEqual(created.status, STATUS_NOT_AVAILABLE)
        self.assertEqual(store.get_count, 0)
        self.assertEqual(store.set_count, 0)

    def test_repr_messages_and_manager_never_expose_stored_private_value(self):
        store = MemorySecureStore()
        manager = self._manager(store)
        result = manager.get_or_create()
        stored_private = store.value

        rendered = " ".join(
            (repr(result), str(result), repr(result.identity), repr(manager), result.message)
        )
        self.assertNotIn(stored_private, rendered)
        self.assertNotIn(result.identity.public_key_base64, repr(result))
        self.assertNotIn(result.identity.public_key_base64, repr(result.identity))

    def test_identity_layer_does_not_send_secret_to_subprocess_or_print(self):
        store = MemorySecureStore()
        with mock.patch.object(subprocess, "run") as run, mock.patch(
            "builtins.print"
        ) as printer:
            result = self._manager(store).get_or_create()

        self.assertEqual(result.status, STATUS_SUCCESS)
        run.assert_not_called()
        printer.assert_not_called()
        self.assertNotIn(store.value, result.message)


class DeviceChallengeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        lock_path = str(Path(self.temp.name) / "device-identity.lock")
        first = DeviceIdentityManager(
            MemorySecureStore(), creation_lock_path=lock_path
        ).get_or_create()
        second = DeviceIdentityManager(
            MemorySecureStore(), creation_lock_path=lock_path
        ).get_or_create()
        self.identity = first.identity
        self.other_identity = second.identity
        self.nonce = _b64url(b"n" * 32)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _verify(
        self,
        *,
        identity=None,
        fingerprint: str | None = None,
        nonce: str | None = None,
        signature: str | None = None,
    ):
        selected = self.identity if identity is None else identity
        selected_nonce = self.nonce if nonce is None else nonce
        selected_signature = (
            self.identity.sign_challenge(self.nonce)
            if signature is None
            else signature
        )
        return verify_device_challenge(
            public_key_base64=selected.public_key_base64,
            device_key_fingerprint=(
                selected.fingerprint if fingerprint is None else fingerprint
            ),
            challenge_nonce=selected_nonce,
            signature_base64=selected_signature,
        )

    def test_valid_domain_separated_challenge(self):
        signature = self.identity.sign_challenge(self.nonce)

        result = self._verify(signature=signature)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(result.ok)
        public_key = Ed25519PublicKey.from_public_bytes(
            self.identity.public_key_bytes
        )
        with self.assertRaises(InvalidSignature):
            public_key.verify(signature=_decode_b64url(signature), data=b"n" * 32)

    def test_tampered_nonce_and_signature_are_invalid(self):
        signature = self.identity.sign_challenge(self.nonce)
        tampered_signature = bytearray(_decode_b64url(signature))
        tampered_signature[0] ^= 1

        wrong_nonce = self._verify(
            nonce=_b64url(b"x" * 32),
            signature=signature,
        )
        wrong_signature = self._verify(
            signature=_b64url(bytes(tampered_signature))
        )

        self.assertEqual(wrong_nonce.status, STATUS_INVALID)
        self.assertEqual(wrong_signature.status, STATUS_INVALID)

    def test_wrong_fingerprint_and_wrong_public_key_are_invalid(self):
        signature = self.identity.sign_challenge(self.nonce)

        wrong_fingerprint = self._verify(
            fingerprint=self.other_identity.fingerprint,
            signature=signature,
        )
        wrong_key = self._verify(
            identity=self.other_identity,
            signature=signature,
        )

        self.assertEqual(wrong_fingerprint.status, STATUS_INVALID)
        self.assertEqual(wrong_key.status, STATUS_INVALID)

    def test_nonce_bounds_are_enforced_by_signer_and_verifier(self):
        signature = self.identity.sign_challenge(self.nonce)
        invalid_nonces = (
            _b64url(b"a" * (CHALLENGE_NONCE_MIN_BYTES - 1)),
            _b64url(b"b" * (CHALLENGE_NONCE_MAX_BYTES + 1)),
        )

        for nonce in invalid_nonces:
            with self.subTest(decoded_length=len(_decode_b64url(nonce))):
                with self.assertRaises(ValueError):
                    self.identity.sign_challenge(nonce)
                self.assertEqual(
                    self._verify(nonce=nonce, signature=signature).status,
                    STATUS_INVALID,
                )

    def test_strict_unpadded_base64url_is_required(self):
        signature = self.identity.sign_challenge(self.nonce)
        invalid_nonces = ("", self.nonce + "=", "not+base64")

        for nonce in invalid_nonces:
            with self.subTest(nonce=nonce):
                with self.assertRaises(ValueError):
                    self.identity.sign_challenge(nonce)
        self.assertEqual(
            self._verify(signature=signature + "=").status,
            STATUS_INVALID,
        )
        result = verify_device_challenge(
            public_key_base64=self.identity.public_key_base64 + "=",
            device_key_fingerprint=self.identity.fingerprint,
            challenge_nonce=self.nonce,
            signature_base64=signature,
        )
        self.assertEqual(result.status, STATUS_INVALID)

    def test_oversized_public_inputs_fail_closed_before_decode(self):
        oversized = "A" * 100_000
        result = verify_device_challenge(
            public_key_base64=oversized,
            device_key_fingerprint=self.identity.fingerprint,
            challenge_nonce=self.nonce,
            signature_base64=self.identity.sign_challenge(self.nonce),
        )

        self.assertEqual(result.status, STATUS_INVALID)
        with self.assertRaises(ValueError):
            self.identity.sign_challenge(oversized)

    def test_server_verifier_reports_crypto_unavailable_honestly(self):
        signature = self.identity.sign_challenge(self.nonce)
        with mock.patch("core.device_identity._ED25519_AVAILABLE", False):
            result = self._verify(signature=signature)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()

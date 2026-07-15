from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.payment_request_store import (
    DEFAULT_ENVELOPE_ACCOUNT,
    DEFAULT_ENVELOPE_SERVICE,
    STATUS_CORRUPT,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    DurablePaymentRequestStore,
    PaymentRequestEnvelope,
    PaymentRequestKind,
    PaymentRequestState,
)
from core.secure_store import (
    STATUS_FAILED as STORE_FAILED,
    STATUS_NOT_FOUND as STORE_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
    UnsupportedSecureStore,
)


SCREENSHOT = b"sanitized-private-evidence-bytes" * 8
PURCHASE_ID = "purchase_" + ("1" * 32)
SUBMISSION_ID = "purchase_" + ("2" * 32)


class _MemorySecureStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service: str, account: str) -> SecureStoreResult:
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STORE_NOT_FOUND)
        return SecureStoreResult(STORE_SUCCESS, value=value)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS)


class _FaultingSecureStore(_MemorySecureStore):
    def __init__(self) -> None:
        super().__init__()
        self.set_failure: str | None = None
        self.delete_fails = False

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        if self.set_failure == "return":
            return SecureStoreResult(STORE_FAILED)
        if self.set_failure == "raise":
            raise OSError("simulated secure-store failure")
        if self.set_failure == "commit_then_fail":
            self.values[(service, account)] = secret
            return SecureStoreResult(STORE_FAILED)
        return super()._set(service, account, secret)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        if self.delete_fails:
            return SecureStoreResult(STORE_FAILED)
        return super()._delete(service, account)


def _envelope(
    *,
    screenshot: bytes = SCREENSHOT,
    state: PaymentRequestState = PaymentRequestState.PENDING,
) -> PaymentRequestEnvelope:
    return PaymentRequestEnvelope(
        idempotency_key=SUBMISSION_ID,
        kind=PaymentRequestKind.INITIAL,
        release_id="rel_store_001",
        device_fingerprint="sha256:" + ("a" * 64),
        proof_sha256=hashlib.sha256(screenshot).hexdigest(),
        content_type="image/png",
        paid_at="2026-07-14T03:59:00Z",
        version="1.0.0",
        state=state,
        created_at="2026-07-14T04:00:00Z",
        updated_at="2026-07-14T04:00:00Z",
        purchase_id=PURCHASE_ID,
    )


def _active_blob_path(root: Path, secure: _MemorySecureStore) -> Path:
    secret = secure.values[(DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)]
    metadata = json.loads(secret)
    return root / "payments" / metadata["cipher"]["blob_name"]


class DurablePaymentRequestStoreTests(unittest.TestCase):
    def _store(self, root: Path, secure=None) -> DurablePaymentRequestStore:
        return DurablePaymentRequestStore(
            _MemorySecureStore() if secure is None else secure,
            root / "payments",
        )

    def test_save_load_roundtrip_keeps_evidence_encrypted_at_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)

            loaded = store.load()
            self.assertEqual(loaded.status, STATUS_SUCCESS)
            self.assertTrue(loaded.pending)
            self.assertEqual(loaded.screenshot, SCREENSHOT)
            assert loaded.envelope is not None
            self.assertEqual(loaded.envelope.idempotency_key, SUBMISSION_ID)
            self.assertEqual(loaded.envelope.purchase_id, PURCHASE_ID)

            # The on-disk blob is ciphertext -- the plaintext never appears.
            blob_path = _active_blob_path(root, secure)
            blob = blob_path.read_bytes()
            self.assertNotIn(SCREENSHOT, blob)
            self.assertNotEqual(blob, SCREENSHOT)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(blob_path.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE((root / "payments").stat().st_mode), 0o700
                )

            # The secure-store secret is metadata only; no screenshot bytes.
            secret = secure.values[(DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)]
            self.assertNotIn(SCREENSHOT, secret.encode("utf-8"))
            self.assertNotIn("sanitized-private-evidence", secret)

    def test_clear_shreds_secret_and_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            self.assertTrue(blob.exists())
            self.assertTrue(store.clear().ok)
            self.assertFalse(blob.exists())
            self.assertEqual(store.load().status, STATUS_NOT_FOUND)

    def test_failed_state_is_stored_separately_from_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            updated = store.update_state(
                state=PaymentRequestState.FAILED,
                updated_at="2026-07-14T04:05:00Z",
            )
            self.assertTrue(updated.ok)
            loaded = store.load()
            self.assertEqual(loaded.status, STATUS_SUCCESS)
            self.assertFalse(loaded.pending)
            assert loaded.envelope is not None
            self.assertIs(loaded.envelope.state, PaymentRequestState.FAILED)
            self.assertEqual(store.peek().envelope.state, PaymentRequestState.FAILED)

    def test_submitted_state_records_server_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            self.assertTrue(
                store.update_state(
                    state=PaymentRequestState.SUBMITTED,
                    payment_id="pay_store_001",
                    license_id="license_store_001",
                    updated_at="2026-07-14T04:06:00Z",
                ).ok
            )
            envelope = store.peek().envelope
            assert envelope is not None
            self.assertEqual(envelope.payment_id, "pay_store_001")
            self.assertEqual(envelope.license_id, "license_store_001")
            self.assertIs(envelope.state, PaymentRequestState.SUBMITTED)

    def test_evidence_that_does_not_match_the_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            result = store.save(_envelope(), b"different-bytes-entirely")
            self.assertEqual(result.status, STATUS_INVALID)
            self.assertEqual(store.load().status, STATUS_NOT_FOUND)

    def test_corrupt_blob_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            raw = bytearray(blob.read_bytes())
            raw[0] ^= 0xFF
            blob.write_bytes(bytes(raw))
            loaded = store.load()
            self.assertEqual(loaded.status, STATUS_CORRUPT)
            self.assertIsNone(loaded.screenshot)

    def test_truncated_blob_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            blob.write_bytes(b"")
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    def test_failed_secure_store_replacement_preserves_existing_request(self) -> None:
        replacement = b"different-sanitized-evidence" * 9
        for failure in ("return", "raise"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                secure = _FaultingSecureStore()
                store = self._store(root, secure)
                self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
                previous_secret = secure.values[
                    (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
                ]
                previous_blob = _active_blob_path(root, secure)

                secure.set_failure = failure
                result = store.save(_envelope(screenshot=replacement), replacement)

                self.assertEqual(result.status, STORE_FAILED)
                self.assertEqual(
                    secure.values[
                        (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
                    ],
                    previous_secret,
                )
                self.assertTrue(previous_blob.is_file())
                loaded = store.load()
                self.assertEqual(loaded.status, STATUS_SUCCESS)
                self.assertEqual(loaded.screenshot, SCREENSHOT)
                self.assertEqual(list((root / "payments").glob("*.enc")), [previous_blob])

    def test_ambiguous_set_failure_that_committed_is_recovered_as_success(self) -> None:
        replacement = b"committed-before-error-evidence" * 9
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _FaultingSecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            previous_blob = _active_blob_path(root, secure)

            secure.set_failure = "commit_then_fail"
            result = store.save(_envelope(screenshot=replacement), replacement)

            self.assertEqual(result.status, STATUS_SUCCESS)
            self.assertFalse(previous_blob.exists())
            self.assertEqual(store.load().screenshot, replacement)

    def test_failed_secure_store_delete_preserves_existing_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _FaultingSecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)

            secure.delete_fails = True
            self.assertEqual(store.clear().status, STORE_FAILED)
            self.assertTrue(blob.is_file())
            self.assertEqual(store.load().screenshot, SCREENSHOT)

    def test_failed_state_update_preserves_previous_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secure = _FaultingSecureStore()
            store = self._store(Path(temp), secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            secure.set_failure = "raise"

            result = store.update_state(
                state=PaymentRequestState.FAILED,
                updated_at="2026-07-14T04:07:00Z",
            )

            self.assertEqual(result.status, STORE_FAILED)
            secure.set_failure = None
            loaded = store.load()
            self.assertEqual(loaded.status, STATUS_SUCCESS)
            assert loaded.envelope is not None
            self.assertIs(loaded.envelope.state, PaymentRequestState.PENDING)

    def test_ambiguous_state_update_is_confirmed_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secure = _FaultingSecureStore()
            store = self._store(Path(temp), secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            secure.set_failure = "commit_then_fail"

            result = store.update_state(
                state=PaymentRequestState.FAILED,
                updated_at="2026-07-14T04:08:00Z",
            )

            self.assertEqual(result.status, STATUS_SUCCESS)
            assert store.peek().envelope is not None
            self.assertIs(store.peek().envelope.state, PaymentRequestState.FAILED)

    @unittest.skipIf(os.name == "nt", "POSIX private-mode semantics")
    def test_blob_with_public_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            _active_blob_path(root, secure).chmod(0o644)
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    @unittest.skipIf(os.name == "nt", "POSIX hard-link semantics")
    def test_hard_linked_blob_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            os.link(blob, root / "duplicate.enc")
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_symlink_blob_is_rejected_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            target = root / "outside"
            target.write_bytes(b"must-not-be-read-or-changed")
            blob.unlink()
            blob.symlink_to(target)

            self.assertEqual(store.load().status, STATUS_CORRUPT)
            self.assertEqual(target.read_bytes(), b"must-not-be-read-or-changed")

    def test_non_regular_blob_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            blob = _active_blob_path(root, secure)
            blob.unlink()
            blob.mkdir()
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership semantics")
    def test_owner_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            with mock.patch(
                "core.payment_request_store.os.getuid", return_value=os.getuid() + 1
            ):
                self.assertEqual(store.load().status, STATUS_CORRUPT)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_symlink_storage_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "external"
            external.mkdir()
            (root / "payments").symlink_to(external, target_is_directory=True)
            store = self._store(root)

            self.assertEqual(store.save(_envelope(), SCREENSHOT).status, STORE_FAILED)
            self.assertEqual(list(external.iterdir()), [])

    def test_metadata_cannot_redirect_blob_outside_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            key = (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
            metadata = json.loads(secure.values[key])
            metadata["cipher"]["blob_name"] = "../../outside.enc"
            secure.values[key] = json.dumps(metadata, separators=(",", ":"))
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    def test_legacy_fixed_blob_without_pointer_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            key = (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
            metadata = json.loads(secure.values[key])
            generated = _active_blob_path(root, secure)
            legacy = root / "payments" / f"{DEFAULT_ENVELOPE_ACCOUNT}.enc"
            generated.replace(legacy)
            metadata["cipher"].pop("blob_name")
            secure.values[key] = json.dumps(metadata, separators=(",", ":"))

            loaded = store.load()
            self.assertEqual(loaded.status, STATUS_SUCCESS)
            self.assertEqual(loaded.screenshot, SCREENSHOT)

    def test_tampered_metadata_binding_fails_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secure = _MemorySecureStore()
            store = self._store(root, secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            key = (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
            metadata = json.loads(secure.values[key])
            # Rebind the envelope to a different idempotency key while leaving the
            # ciphertext untouched: the AES-GCM associated data no longer matches,
            # so decryption must fail closed rather than yield swapped evidence.
            metadata["idempotency_key"] = "purchase_" + ("9" * 32)
            secure.values[key] = json.dumps(metadata, separators=(",", ":"))
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    def test_corrupt_metadata_schema_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secure = _MemorySecureStore()
            store = self._store(Path(temp), secure)
            self.assertTrue(store.save(_envelope(), SCREENSHOT).ok)
            secure.values[
                (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT)
            ] = "{not valid json"
            self.assertEqual(store.load().status, STATUS_CORRUPT)

    def test_secure_store_unavailable_is_reported_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp), UnsupportedSecureStore())
            self.assertEqual(
                store.save(_envelope(), SCREENSHOT).status, STATUS_NOT_AVAILABLE
            )
            self.assertEqual(store.load().status, STATUS_NOT_AVAILABLE)
            self.assertEqual(store.clear().status, STATUS_NOT_AVAILABLE)

    def test_missing_request_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            self.assertEqual(store.load().status, STATUS_NOT_FOUND)
            self.assertEqual(store.peek().status, STATUS_NOT_FOUND)
            self.assertTrue(store.clear().ok)

    def test_envelope_repr_hides_context(self) -> None:
        envelope = _envelope()
        rendered = repr(envelope)
        self.assertNotIn(PURCHASE_ID, rendered)
        self.assertNotIn(envelope.proof_sha256, rendered)


if __name__ == "__main__":
    unittest.main()

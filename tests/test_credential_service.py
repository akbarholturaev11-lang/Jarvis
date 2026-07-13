from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import core.credential_service as credential_service
from core.credential_service import (
    MIGRATION_COMPLETED,
    MIGRATION_FAILED,
    delete_gemini_api_key,
    load_gemini_api_key,
    store_gemini_api_key,
)
from core.secure_store import (
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


SECRET = "legacy-secret-value"


class FakeStore(SecureStore):
    def __init__(
        self,
        *,
        value: str | None = None,
        get_status: str | None = None,
        set_status: str = STATUS_SUCCESS,
        delete_status: str | None = None,
    ) -> None:
        self.value = value
        self.get_status = get_status
        self.set_status = set_status
        self.delete_status = delete_status
        self.writes: list[str] = []
        self.deletes = 0
        self.raise_on: str | None = None

    def _get(self, service: str, account: str) -> SecureStoreResult:
        if self.raise_on == "get":
            raise RuntimeError(f"backend exposed {self.value}")
        status = self.get_status
        if status is None:
            status = STATUS_SUCCESS if self.value is not None else STATUS_NOT_FOUND
        return SecureStoreResult(
            status,
            value=self.value if status == STATUS_SUCCESS else None,
        )

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        if self.raise_on == "set":
            raise RuntimeError(f"backend exposed {secret}")
        self.writes.append(secret)
        if self.set_status == STATUS_SUCCESS:
            self.value = secret
            self.get_status = None
        return SecureStoreResult(self.set_status)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        if self.raise_on == "delete":
            raise RuntimeError(f"backend exposed {self.value}")
        self.deletes += 1
        status = self.delete_status
        if status is None:
            status = STATUS_SUCCESS if self.value is not None else STATUS_NOT_FOUND
        if status == STATUS_SUCCESS:
            self.value = None
            self.get_status = None
        return SecureStoreResult(status)


class CredentialServiceTests(unittest.TestCase):
    def test_secure_store_wins_and_repr_redacts(self) -> None:
        result = load_gemini_api_key(
            store=FakeStore(value=SECRET),
            legacy_path=Path("/missing"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.value, SECRET)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, result.message)

    def test_store_verifies_readback_update_and_restart_persistence(self) -> None:
        persistent = FakeStore()
        stored = store_gemini_api_key("first-secret", store=persistent)
        self.assertTrue(stored.ok)
        restarted = load_gemini_api_key(
            store=persistent, legacy_path=Path("/missing")
        )
        self.assertEqual(restarted.value, "first-secret")
        updated = store_gemini_api_key("second-secret", store=persistent)
        self.assertTrue(updated.ok)
        self.assertEqual(persistent.value, "second-secret")
        self.assertEqual(persistent.writes, ["first-secret", "second-secret"])

    def test_store_readback_mismatch_fails_without_leak(self) -> None:
        store = FakeStore()

        def mismatched_get(_service: str, _account: str) -> SecureStoreResult:
            return SecureStoreResult(STATUS_SUCCESS, value="different-secret")

        store._get = mismatched_get  # type: ignore[method-assign]
        result = store_gemini_api_key(SECRET, store=store)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, repr(result))

    def test_legacy_migration_is_verified_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text(
                json.dumps(
                    {
                        "gemini_api_key": SECRET,
                        "preserved_setting": "keep-me",
                    }
                ),
                encoding="utf-8",
            )
            store = FakeStore()
            migrated = load_gemini_api_key(store=store, legacy_path=path)

            self.assertTrue(migrated.ok)
            self.assertEqual(migrated.value, SECRET)
            self.assertEqual(migrated.migration_status, MIGRATION_COMPLETED)
            self.assertEqual(store.value, SECRET)
            sanitized = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("gemini_api_key", sanitized)
            self.assertEqual(sanitized["preserved_setting"], "keep-me")
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

            second = load_gemini_api_key(store=store, legacy_path=path)
            self.assertTrue(second.ok)
            self.assertEqual(second.value, SECRET)
            self.assertEqual(store.writes, [SECRET])

    def test_existing_secure_key_scrubs_stale_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text(
                json.dumps({"gemini_api_key": "stale-secret", "other": 1}),
                encoding="utf-8",
            )
            store = FakeStore(value="authoritative-secret")
            result = load_gemini_api_key(store=store, legacy_path=path)

            self.assertTrue(result.ok)
            self.assertEqual(result.value, "authoritative-secret")
            self.assertEqual(result.migration_status, MIGRATION_COMPLETED)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"other": 1})
            self.assertEqual(store.writes, [])

    def test_storage_failure_never_falls_back_to_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            original = json.dumps({"gemini_api_key": SECRET})
            path.write_text(original, encoding="utf-8")
            store = FakeStore(get_status=STATUS_NOT_FOUND, set_status=STATUS_NOT_AVAILABLE)
            result = load_gemini_api_key(store=store, legacy_path=path)

            self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
            self.assertIsNone(result.value)
            self.assertEqual(result.migration_status, MIGRATION_FAILED)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_cleanup_failure_is_fail_closed_and_retriable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            original = json.dumps({"gemini_api_key": SECRET})
            path.write_text(original, encoding="utf-8")
            store = FakeStore()
            with mock.patch(
                "core.credential_service._remove_legacy_key", return_value=False
            ):
                result = load_gemini_api_key(store=store, legacy_path=path)

            self.assertEqual(result.status, STATUS_FAILED)
            self.assertEqual(result.migration_status, MIGRATION_FAILED)
            self.assertEqual(store.value, SECRET)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            retried = load_gemini_api_key(store=store, legacy_path=path)
            self.assertTrue(retried.ok)
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_concurrent_legacy_replacement_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text(
                json.dumps({"gemini_api_key": SECRET, "owner": "original"}),
                encoding="utf-8",
            )
            replacement = Path(temp_dir) / "replacement.json"
            concurrent = {
                "gemini_api_key": "concurrent-secret",
                "owner": "concurrent-writer",
            }
            real_rename = os.rename
            real_replace = os.replace

            def race_before_rename(source, destination):
                replacement.write_text(json.dumps(concurrent), encoding="utf-8")
                real_replace(replacement, source)
                return real_rename(source, destination)

            with mock.patch(
                "core.credential_service.os.rename", side_effect=race_before_rename
            ):
                result = load_gemini_api_key(
                    store=FakeStore(), legacy_path=path
                )

            self.assertEqual(result.status, STATUS_FAILED)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), concurrent
            )

    def test_windows_failed_restore_never_retains_plaintext_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "api_keys.json"
            quarantine = Path(temp_dir) / "api_keys.json.quarantine"
            original.write_text('{"owner":"concurrent"}', encoding="utf-8")
            quarantine.write_text(
                json.dumps({"gemini_api_key": SECRET}), encoding="utf-8"
            )

            with (
                mock.patch.object(credential_service.os, "name", "nt"),
                mock.patch.object(
                    credential_service,
                    "_rename_windows_no_clobber",
                    side_effect=FileExistsError,
                ),
            ):
                credential_service._restore_quarantine(quarantine, original)

            self.assertEqual(original.read_text(encoding="utf-8"), '{"owner":"concurrent"}')
            self.assertFalse(quarantine.exists())

    def test_windows_hardlink_failure_restores_missing_original_by_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "api_keys.json"
            quarantine = Path(temp_dir) / "api_keys.json.quarantine"
            legacy = json.dumps({"gemini_api_key": SECRET, "keep": True})
            quarantine.write_text(legacy, encoding="utf-8")

            def restore(source, target):
                os.rename(source, target)

            with (
                mock.patch.object(credential_service.os, "name", "nt"),
                mock.patch.object(
                    credential_service,
                    "_link_regular_no_clobber",
                    side_effect=OSError,
                ),
                mock.patch.object(
                    credential_service,
                    "_rename_windows_no_clobber",
                    side_effect=restore,
                ),
            ):
                credential_service._restore_quarantine(quarantine, original)

            self.assertEqual(original.read_text(encoding="utf-8"), legacy)
            self.assertFalse(quarantine.exists())

    def test_in_place_rewrite_is_detected_before_plaintext_source_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text(
                json.dumps({"gemini_api_key": SECRET, "keep": True}),
                encoding="utf-8",
            )
            real_verify = credential_service._verify_installed_payload
            calls = 0

            def rewrite_before_final_check(target, expected, payload):
                nonlocal calls
                calls += 1
                if calls == 3:
                    Path(target).write_text(
                        json.dumps({"gemini_api_key": "concurrent-secret"}),
                        encoding="utf-8",
                    )
                return real_verify(target, expected, payload)

            with mock.patch.object(
                credential_service,
                "_verify_installed_payload",
                side_effect=rewrite_before_final_check,
            ):
                result = load_gemini_api_key(store=FakeStore(), legacy_path=path)

            self.assertEqual(result.status, STATUS_FAILED)
            self.assertGreaterEqual(calls, 3)

    def test_symlink_oversize_and_malformed_legacy_sources_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_text(json.dumps({"gemini_api_key": SECRET}), encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            self.assertEqual(
                load_gemini_api_key(store=FakeStore(), legacy_path=link).status,
                STATUS_FAILED,
            )
            target.write_bytes(b"x" * (128 * 1024 + 1))
            self.assertEqual(
                load_gemini_api_key(store=FakeStore(), legacy_path=target).status,
                STATUS_FAILED,
            )
            target.write_text("{not-json", encoding="utf-8")
            self.assertEqual(
                load_gemini_api_key(store=FakeStore(), legacy_path=target).status,
                STATUS_FAILED,
            )
            target.write_text('{"gemini_api_key": null}', encoding="utf-8")
            self.assertEqual(
                load_gemini_api_key(store=FakeStore(), legacy_path=target).status,
                STATUS_FAILED,
            )

    def test_malformed_legacy_does_not_block_authoritative_secure_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text("{not-json", encoding="utf-8")
            result = load_gemini_api_key(
                store=FakeStore(value=SECRET), legacy_path=path
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.value, SECRET)
            self.assertEqual(result.migration_status, MIGRATION_FAILED)
            self.assertNotIn(SECRET, repr(result))

    def test_delete_success_missing_failure_and_exception_are_honest(self) -> None:
        store = FakeStore(value=SECRET)
        self.assertTrue(delete_gemini_api_key(store=store).ok)
        self.assertEqual(
            delete_gemini_api_key(store=store).status, STATUS_NOT_FOUND
        )
        failed = FakeStore(value=SECRET, delete_status=STATUS_FAILED)
        self.assertEqual(delete_gemini_api_key(store=failed).status, STATUS_FAILED)
        failed.raise_on = "delete"
        result = delete_gemini_api_key(store=failed)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, repr(result))

    def test_backend_exception_does_not_escape_or_leak_secret(self) -> None:
        store = FakeStore(value=SECRET)
        store.raise_on = "set"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = store_gemini_api_key(SECRET, store=store)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

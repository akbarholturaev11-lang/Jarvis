from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.credential_service import load_gemini_api_key, store_gemini_api_key
from core.secure_store import (
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    SecureStore,
    SecureStoreResult,
)


class FakeStore(SecureStore):
    def __init__(self, status: str, value: str | None = None) -> None:
        self.status = status
        self.value = value
        self.writes: list[str] = []

    def _get(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(self.status, value=self.value)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.writes.append(secret)
        return SecureStoreResult(self.status)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)


class CredentialServiceTests(unittest.TestCase):
    def test_secure_store_wins_and_repr_redacts(self) -> None:
        result = load_gemini_api_key(
            store=FakeStore(STATUS_SUCCESS, "secret-value"),
            legacy_path=Path("/missing"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "secret-value")
        self.assertNotIn("secret-value", repr(result))

    def test_legacy_fallback_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            original = json.dumps({"gemini_api_key": "legacy-secret"})
            path.write_text(original, encoding="utf-8")
            result = load_gemini_api_key(
                store=FakeStore(STATUS_NOT_FOUND), legacy_path=path
            )
            self.assertEqual(result.source, "legacy_read_only")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_symlink_and_oversize_legacy_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_text('{"gemini_api_key":"x"}', encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            self.assertFalse(
                load_gemini_api_key(
                    store=FakeStore(STATUS_NOT_FOUND), legacy_path=link
                ).ok
            )
            target.write_bytes(b"x" * (128 * 1024 + 1))
            self.assertFalse(
                load_gemini_api_key(
                    store=FakeStore(STATUS_NOT_FOUND), legacy_path=target
                ).ok
            )

    def test_store_is_secure_only_and_fail_closed(self) -> None:
        for status in (STATUS_SUCCESS, STATUS_NOT_AVAILABLE, STATUS_FAILED):
            with self.subTest(status=status):
                store = FakeStore(status)
                result = store_gemini_api_key("new-secret", store=store)
                self.assertEqual(store.writes, ["new-secret"])
                self.assertEqual(result.ok, status == STATUS_SUCCESS)
                self.assertNotIn("new-secret", repr(result))


if __name__ == "__main__":
    unittest.main()

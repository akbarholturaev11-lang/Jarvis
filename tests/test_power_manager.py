from __future__ import annotations

import unittest
from unittest import mock

from core.power_manager import KeepAwakeManager


class _FakeAdapter:
    def __init__(self, token=None, status="ok"):
        self._token = token
        self._status = status
        self.released = []

    def prevent_sleep(self, reason=""):
        return self._token, self._status

    def release_sleep(self, token):
        self.released.append(token)


class TestKeepAwakeManager(unittest.TestCase):
    def test_unsupported_is_honest(self):
        fake = _FakeAdapter(token=None, status="unsupported")
        with mock.patch("core.power_manager.select_platform_adapter", return_value=fake):
            km = KeepAwakeManager()
            ok, status = km.acquire()
            self.assertFalse(ok)
            self.assertFalse(km.active)
            self.assertIn("unsupported", status)

    def test_acquire_release_and_idempotent(self):
        fake = _FakeAdapter(token="TOK", status="on")
        with mock.patch("core.power_manager.select_platform_adapter", return_value=fake):
            km = KeepAwakeManager()
            ok, _ = km.acquire()
            self.assertTrue(ok)
            self.assertTrue(km.active)
            # second acquire is a no-op
            ok2, _ = km.acquire()
            self.assertTrue(ok2)
            km.release()
            self.assertFalse(km.active)
            self.assertEqual(fake.released, ["TOK"])
            # release again is a no-op
            ok3, _ = km.release()
            self.assertTrue(ok3)

    def test_prevent_sleep_exception_does_not_crash(self):
        fake = _FakeAdapter()
        fake.prevent_sleep = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch("core.power_manager.select_platform_adapter", return_value=fake):
            km = KeepAwakeManager()
            ok, status = km.acquire()
            self.assertFalse(ok)
            self.assertFalse(km.active)


if __name__ == "__main__":
    unittest.main()

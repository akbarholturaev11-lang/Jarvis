from __future__ import annotations

import unittest
from unittest import mock

import actions.send_message as sm


class TestSendMessagePreflight(unittest.TestCase):
    def _params(self, **kw):
        p = {
            "receiver": "Ali",
            "message_text": "salom",
            "platform": "telegram",
            "confirmed": "true",
        }
        p.update(kw)
        return p

    def test_denied_accessibility_blocks_with_honest_message(self):
        with mock.patch.object(sm, "_PYAUTOGUI", True), \
             mock.patch.object(sm, "_get_os", return_value="mac"), \
             mock.patch("core.permissions_manager.permission_status", return_value="denied"):
            res = sm.send_message(self._params())
        self.assertIn("Accessibility", res)
        self.assertIn("Cannot send", res)

    def test_granted_proceeds_to_handler(self):
        fake_handler = mock.Mock(return_value="Message drafted ... not verified.")
        with mock.patch.object(sm, "_PYAUTOGUI", True), \
             mock.patch.object(sm, "_get_os", return_value="mac"), \
             mock.patch("core.permissions_manager.permission_status", return_value="granted"), \
             mock.patch.object(sm, "_resolve_platform", return_value=fake_handler):
            res = sm.send_message(self._params())
        fake_handler.assert_called_once()
        self.assertIn("not verified", res)

    def test_unknown_accessibility_still_proceeds(self):
        # Undetectable permission must not block — proceed and let the send report
        # its own honest unverified status.
        fake_handler = mock.Mock(return_value="attempt completed ... not verified.")
        with mock.patch.object(sm, "_PYAUTOGUI", True), \
             mock.patch.object(sm, "_get_os", return_value="mac"), \
             mock.patch("core.permissions_manager.permission_status", return_value="unknown"), \
             mock.patch.object(sm, "_resolve_platform", return_value=fake_handler):
            sm.send_message(self._params())
        fake_handler.assert_called_once()

    def test_missing_receiver_is_rejected(self):
        with mock.patch.object(sm, "_PYAUTOGUI", True):
            self.assertIn("recipient", sm.send_message(self._params(receiver="")).lower())

    def test_missing_message_is_rejected(self):
        with mock.patch.object(sm, "_PYAUTOGUI", True):
            self.assertIn("message", sm.send_message(self._params(message_text="")).lower())


if __name__ == "__main__":
    unittest.main()

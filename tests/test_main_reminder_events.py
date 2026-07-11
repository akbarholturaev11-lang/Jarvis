from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from core.reminder_events import ReminderEvent


class _FakeUI:
    muted = False

    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


class _BrokenOutputStream:
    def start(self):
        pass

    def write(self, chunk):
        raise RuntimeError("output device failed")

    def stop(self):
        pass

    def close(self):
        pass


class _FakeSession:
    def __init__(self, fail: bool = False, playback_failed: bool = False):
        self.fail = fail
        self.playback_failed = playback_failed
        self.turns = []
        self.jarvis = None

    async def send_client_content(self, **payload):
        if self.fail:
            raise RuntimeError("session unavailable")
        self.turns.append(payload)
        if self.jarvis is not None:
            self.jarvis._reminder_audio_received = True
            self.jarvis._reminder_playback_failed = self.playback_failed
            self.jarvis._reminder_turn_complete = True
            self.jarvis._client_turn_pending = False
            self.jarvis._reminder_playback_event.set()


class _YieldingSession(_FakeSession):
    async def send_client_content(self, **payload):
        await asyncio.sleep(0.01)
        await super().send_client_content(**payload)


class MainReminderEventTests(unittest.IsolatedAsyncioTestCase):
    def _jarvis(self, session: _FakeSession):
        jarvis = object.__new__(main.JarvisLive)
        jarvis.session = session
        jarvis._session_generation = 4
        jarvis.device_profile = {"platform": {"os": "macos"}}
        jarvis.ui = _FakeUI()
        jarvis._speaking_lock = threading.Lock()
        jarvis._is_speaking = False
        jarvis._active_user_text = ""
        jarvis.audio_in_queue = None
        jarvis.out_queue = None
        session.jarvis = jarvis
        return jarvis

    def _event(self, root: Path) -> ReminderEvent:
        event_id = "JARVISReminder_20260711_120000_a1b2c3"
        path = root / f"{event_id}.json"
        path.write_text(
            json.dumps(
                {
                    "source": "jarvis_reminder",
                    "event_id": event_id,
                    "message": "Dori ichishni eslat",
                }
            ),
            encoding="utf-8",
        )
        return ReminderEvent(event_id, "Dori ichishni eslat", path)

    async def test_active_live_session_gets_data_only_spoken_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession()
            jarvis = self._jarvis(session)
            event = self._event(Path(tmp))

            await jarvis._dispatch_reminder_event(event)

            self.assertEqual(len(session.turns), 1)
            prompt = session.turns[0]["turns"]["parts"][0]["text"]
            self.assertIn("[SCHEDULED_REMINDER_EVENT]", prompt)
            self.assertIn("do not call tools", prompt)
            self.assertIn("Dori ichishni eslat", prompt)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_live_send_failure_uses_system_voice_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession(fail=True)
            jarvis = self._jarvis(session)
            event = self._event(Path(tmp))

            with patch.object(
                main,
                "speak_reminder_fallback",
                return_value=(True, "System speech command completed."),
            ) as fallback:
                await jarvis._dispatch_reminder_event(event)

            fallback.assert_called_once_with("Dori ichishni eslat", "macos")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_reminder_turn_cannot_execute_model_requested_tools(self):
        session = _FakeSession()
        jarvis = self._jarvis(session)
        jarvis._reminder_turn_active = True
        function_call = SimpleNamespace(
            id="blocked-call",
            name="computer_settings",
            args={"action": "shutdown"},
        )

        response = await jarvis._execute_tool(
            function_call,
            user_text="[scheduled reminder data]",
        )

        self.assertEqual(response.response["result_status"], "failed")
        self.assertFalse(response.response["verified"])
        self.assertIn("not allowed to execute tools", response.response["result"])

    async def test_late_reminder_tool_call_stays_blocked_until_turn_complete(self):
        session = _FakeSession()
        jarvis = self._jarvis(session)
        jarvis._reminder_turn_active = False
        jarvis._reminder_tool_blocked_until_turn_complete = True
        function_call = SimpleNamespace(
            id="late-blocked-call",
            name="computer_settings",
            args={"action": "shutdown"},
        )

        response = await jarvis._execute_tool(function_call)

        self.assertEqual(response.response["result_status"], "failed")

    async def test_pending_event_is_claimed_before_existing_speech_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession()
            jarvis = self._jarvis(session)
            jarvis._client_turn_pending = True
            event = self._event(Path(tmp))

            task = asyncio.create_task(jarvis._dispatch_reminder_event(event))
            await asyncio.sleep(0.01)

            self.assertFalse(event.path.exists())
            self.assertTrue(event.path.with_suffix(".app").exists())

            jarvis._client_turn_pending = False
            await task
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_stuck_idle_gate_times_out_to_system_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession()
            jarvis = self._jarvis(session)
            jarvis._client_turn_pending = True
            event = self._event(Path(tmp))

            with (
                patch.object(main, "REMINDER_IDLE_WAIT_SECONDS", 0.0),
                patch.object(
                    main,
                    "speak_reminder_fallback",
                    return_value=(True, "System speech command completed."),
                ) as fallback,
            ):
                await jarvis._dispatch_reminder_event(event)

            fallback.assert_called_once_with("Dori ichishni eslat", "macos")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_playback_failure_uses_system_voice_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession(playback_failed=True)
            jarvis = self._jarvis(session)
            event = self._event(Path(tmp))

            with patch.object(
                main,
                "speak_reminder_fallback",
                return_value=(True, "System speech command completed."),
            ) as fallback:
                await jarvis._dispatch_reminder_event(event)

            fallback.assert_called_once_with("Dori ichishni eslat", "macos")

    async def test_failed_live_and_system_speech_retains_bounded_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeSession(fail=True)
            jarvis = self._jarvis(session)
            event = self._event(Path(tmp))

            with patch.object(
                main,
                "speak_reminder_fallback",
                return_value=(False, "System speech unavailable."),
            ):
                await jarvis._dispatch_reminder_event(event)

            retained = event.path.with_suffix(".app")
            self.assertTrue(retained.exists())
            payload = json.loads(retained.read_text(encoding="utf-8"))
            self.assertEqual(payload["delivery_attempts"], 1)

    async def test_raw_output_write_failure_marks_reminder_playback_failed(self):
        session = _FakeSession()
        jarvis = self._jarvis(session)
        jarvis.audio_in_queue = asyncio.Queue()
        jarvis.audio_in_queue.put_nowait(b"audio")
        jarvis._turn_done_event = asyncio.Event()
        jarvis._reminder_turn_active = True
        jarvis._reminder_turn_complete = True
        jarvis._reminder_playback_event = asyncio.Event()

        with patch.object(main.sd, "RawOutputStream", return_value=_BrokenOutputStream()):
            await jarvis._play_audio(4)

        self.assertTrue(jarvis._reminder_playback_failed)
        self.assertTrue(jarvis._reminder_playback_event.is_set())

    async def test_lease_loss_during_live_turn_releases_runtime_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _YieldingSession()
            jarvis = self._jarvis(session)
            event = self._event(Path(tmp))

            with (
                patch.object(main, "REMINDER_CLAIM_HEARTBEAT_SECONDS", 0.0),
                patch.object(main, "REMINDER_CLAIM_RETRY_SECONDS", 0.0),
                patch.object(
                    main,
                    "renew_reminder_claim",
                    side_effect=[True, False, False],
                ),
            ):
                await jarvis._dispatch_reminder_event(event)

            self.assertFalse(jarvis._reminder_turn_active)
            self.assertFalse(jarvis._client_turn_pending)
            self.assertIsNone(jarvis._reminder_playback_event)
            self.assertTrue(jarvis._reminder_cleared_event.is_set())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main
from core.session_context import SessionContext


class _FakeUI:
    muted = False
    current_file = None

    def __init__(self):
        self.states = []
        self.content = []
        self.logs = []

    def set_state(self, state):
        self.states.append(state)

    def show_content(self, title, content):
        self.content.append((title, content))

    def write_log(self, message):
        self.logs.append(message)


class _FakeSession:
    def __init__(self):
        self.turns = []

    async def send_client_content(self, **payload):
        self.turns.append(payload)


class _SingleTurnReceiveSession:
    def __init__(self, jarvis):
        self.jarvis = jarvis

    async def receive(self):
        server_content = SimpleNamespace(
            output_transcription=None,
            input_transcription=None,
            turn_complete=True,
        )
        yield SimpleNamespace(
            data=None,
            server_content=server_content,
            tool_call=None,
        )
        self.jarvis._session_generation += 1


class MainBriefingDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.jarvis = object.__new__(main.JarvisLive)
        self.jarvis.ui = _FakeUI()
        self.jarvis.session_context = SessionContext()
        self.jarvis.device_profile = {}
        self.jarvis._active_user_text = ""
        self.jarvis._interrupted = False
        self.jarvis._turn_done_event = None
        self.jarvis._dashboard = None
        self.jarvis._pending_vision = None
        self.jarvis._vision_close_pending = False
        self.jarvis.speak_error = Mock()

    @staticmethod
    def _function_call(name, args):
        return SimpleNamespace(id="test-call", name=name, args=args)

    async def test_men_uydaman_reroutes_wrong_news_call_on_real_dispatch_path(self):
        report = (
            "[PERSONAL_OPERATIONS_BRIEFING]\n"
            "status=success\nFoyda: verified progress\n"
            "Zarar: verified risk\nNext action: run checks"
        )
        personal_action = Mock(return_value=report)
        web_action = Mock(return_value="world news must not run")

        with (
            patch.object(main, "personal_briefing_action", personal_action),
            patch.object(main, "web_search_action", web_action),
            patch.object(main, "detect_active_app", return_value=""),
        ):
            response = await self.jarvis._execute_tool(
                self._function_call(
                    "web_search",
                    {"mode": "news", "query": "top world news today"},
                ),
                user_text="men uydaman",
            )

        personal_action.assert_called_once()
        web_action.assert_not_called()
        self.assertEqual(response.response["actual_tool_executed"], "personal_briefing")
        self.assertTrue(response.response["verified"])
        self.assertIn("web_search -> personal_briefing", response.response["context_applied"])

    async def test_world_news_stays_on_existing_news_action(self):
        personal_action = Mock(return_value="personal must not run")
        web_action = Mock(return_value="Latest news: top world news today\n1. Verified headline")

        with (
            patch.object(main, "personal_briefing_action", personal_action),
            patch.object(main, "web_search_action", web_action),
            patch.object(main, "detect_active_app", return_value=""),
        ):
            response = await self.jarvis._execute_tool(
                self._function_call(
                    "personal_briefing",
                    {"sources": ["local_projects"]},
                ),
                user_text="dunyo yangiliklarini ayt",
            )

        personal_action.assert_not_called()
        web_action.assert_called_once()
        called_parameters = web_action.call_args.kwargs["parameters"]
        self.assertEqual(called_parameters["mode"], "news")
        self.assertEqual(called_parameters["query"], "top world news today")
        self.assertEqual(response.response["actual_tool_executed"], "web_search")

    async def test_external_stats_reroute_to_named_not_configured_source(self):
        report = (
            "[PERSONAL_OPERATIONS_BRIEFING]\nstatus=success\n"
            "telegram: status=not_configured; statistics=None"
        )
        personal_action = Mock(return_value=report)

        with (
            patch.object(main, "personal_briefing_action", personal_action),
            patch.object(main, "detect_active_app", return_value=""),
        ):
            response = await self.jarvis._execute_tool(
                self._function_call(
                    "web_search",
                    {"mode": "search", "query": "Telegram statistics"},
                ),
                user_text="Telegram kanalim statistikasi qanday?",
            )

        called_parameters = personal_action.call_args.kwargs["parameters"]
        self.assertEqual(called_parameters["sources"], ["telegram", "zerno"])
        self.assertEqual(called_parameters["scope"], "statistics")
        self.assertEqual(response.response["actual_tool_executed"], "personal_briefing")

    async def test_startup_phase_collects_personal_data_without_world_news(self):
        self.jarvis.session = _FakeSession()
        self.jarvis._session_generation = 7
        report = (
            "[PERSONAL_OPERATIONS_BRIEFING]\nstatus=available\n"
            "Foyda: local evidence\nZarar: external not_configured\n"
            "Next action: verify route"
        )
        personal_action = Mock(return_value=report)
        web_action = Mock(return_value="world news must not run")

        with (
            patch.object(main, "personal_briefing_action", personal_action),
            patch.object(main, "web_search_action", web_action),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            await self.jarvis._briefing_personal_phase("", 7)

        personal_action.assert_called_once()
        web_action.assert_not_called()
        self.assertEqual(len(self.jarvis.session.turns), 1)
        payload = self.jarvis.session.turns[0]["turns"]["parts"][0]["text"]
        self.assertIn("[STARTUP_BRIEFING]", payload)
        self.assertIn(report, payload)
        self.assertNotIn("top world news today", payload)
        self.assertEqual(
            self.jarvis.session_context.actions[-1].tool_name,
            "personal_briefing",
        )

    async def test_startup_greeting_promises_personal_briefing_not_news(self):
        self.jarvis.session = _FakeSession()
        self.jarvis._session_generation = 9
        self.jarvis._briefing_personal_phase = AsyncMock()

        with (
            patch.object(main, "load_memory", return_value={}),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            await self.jarvis._send_startup_briefing(9)

        greeting = self.jarvis.session.turns[0]["turns"]["parts"][0]["text"].lower()
        self.assertIn("personal operations briefing", greeting)
        self.assertNotIn("news", greeting)
        self.jarvis._briefing_personal_phase.assert_awaited_once_with("", 9)

    async def test_completed_turn_clears_active_text_before_next_voice_command(self):
        self.jarvis._session_generation = 12
        self.jarvis._active_user_text = "men uydaman"
        self.jarvis.session = _SingleTurnReceiveSession(self.jarvis)

        await self.jarvis._receive_audio(12)

        self.assertEqual(self.jarvis._active_user_text, "")

    async def test_reminder_dispatch_uses_device_profile(self):
        self.jarvis.device_profile = {"platform": {"os": "macos"}}
        reminder_action = Mock(return_value="Reminder set for July 12 at 12:00 PM.")

        with (
            patch.object(main, "reminder", reminder_action),
            patch.object(main, "detect_active_app", return_value=""),
        ):
            response = await self.jarvis._execute_tool(
                self._function_call(
                    "reminder",
                    {
                        "date": "2026-07-12",
                        "time": "12:00",
                        "message": "Dori ich",
                    },
                ),
                user_text="Ertaga dori ichishni eslat",
            )

        self.assertTrue(response.response["verified"])
        self.assertIs(
            reminder_action.call_args.kwargs["device_profile"],
            self.jarvis.device_profile,
        )


if __name__ == "__main__":
    unittest.main()

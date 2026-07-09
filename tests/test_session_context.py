import unittest

from core.session_context import (
    SessionContext,
    infer_result_status,
    truthful_claim,
)


class SessionContextTests(unittest.TestCase):
    def test_records_only_last_five_actions(self):
        ctx = SessionContext(max_actions=5)

        for idx in range(7):
            ctx.record_action(
                user_text=f"request {idx}",
                assistant_intent="open app",
                tool_name="open_app",
                tool_parameters={"app_name": f"App {idx}"},
                execution_method="open_app",
                result=f"Opened App {idx}.",
            )

        records = ctx.to_dicts()
        self.assertEqual(len(records), 5)
        self.assertEqual(records[0]["user_text"], "request 2")
        self.assertEqual(records[-1]["user_text"], "request 6")

    def test_vague_follow_up_resolves_from_previous_browser_context(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="YouTube'da piano music och",
            assistant_intent="search in browser",
            tool_name="browser_control",
            tool_parameters={
                "action": "search",
                "browser": "chrome",
                "query": "piano music",
            },
            execution_method="browser_control",
            result="Opened: https://www.google.com/search?q=piano+music",
        )

        resolution = ctx.resolve_follow_up("o'chir")

        self.assertFalse(resolution["needs_confirmation"])
        self.assertEqual(resolution["suggested_tool"], "browser_control")
        self.assertEqual(resolution["parameter_hints"]["browser"], "chrome")

    def test_opened_browser_app_is_tracked_as_browser_context(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Chrome och",
            assistant_intent="open app",
            tool_name="open_app",
            tool_parameters={"app_name": "Google Chrome"},
            execution_method="open_app",
            result="Opened Google Chrome.",
        )

        resolution = ctx.resolve_follow_up("to'xtat")

        self.assertEqual(ctx.last_browser_used, "chrome")
        self.assertEqual(resolution["suggested_tool"], "browser_control")
        self.assertEqual(resolution["parameter_hints"]["browser"], "chrome")

    def test_uncertain_result_does_not_produce_completed_claim(self):
        status, verified = infer_result_status(
            "send_message",
            "Message send attempt completed for Akbar via Telegram, but recipient/chat was not verified.",
        )

        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)
        self.assertEqual(truthful_claim(status, verified), "Aniq tasdiqlay olmadim.")

    def test_user_correction_attaches_to_previous_action(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Telegramdan xabar yubor",
            assistant_intent="send message",
            tool_name="send_message",
            tool_parameters={
                "platform": "Telegram",
                "receiver": "Ali",
                "message_text": "private",
            },
            execution_method="send_message",
            result="Message send attempt completed for Ali via Telegram, but recipient/chat was not verified.",
        )

        attached = ctx.observe_user_text("yo'q, noto'g'ri")

        self.assertTrue(attached)
        self.assertEqual(ctx.to_dicts()[-1]["user_correction"], "yo'q, noto'g'ri")


if __name__ == "__main__":
    unittest.main()

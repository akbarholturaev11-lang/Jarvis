import unittest

from core.session_context import (
    SessionContext,
    infer_result_status,
    resolve_followup_intent,
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

    def test_youtube_media_play_then_stop_resolves_to_media_pause(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="YouTube'dan relaxing music qo'y",
            assistant_intent="youtube play: relaxing music",
            tool_name="youtube_video",
            tool_parameters={
                "action": "play",
                "query": "relaxing music",
            },
            execution_method="youtube_video",
            result="Playing: relaxing music",
        )

        resolution = resolve_followup_intent("to'xtat", ctx)

        self.assertFalse(resolution["needs_confirmation"])
        self.assertEqual(resolution["resolved_intent"], "media_pause")
        self.assertEqual(resolution["suggested_tool"], "media_control")
        self.assertNotEqual(resolution["suggested_tool"], "browser_control")

    def test_youtube_media_stop_preserves_chatgpt_atlas_target(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="GPT Atlas'da YouTube relaxing music qo'y",
            assistant_intent="youtube play: relaxing music",
            tool_name="youtube_video",
            tool_parameters={
                "action": "play",
                "query": "relaxing music",
                "target_app": "ChatGPT Atlas",
            },
            execution_method="youtube_video",
            result="Playing: relaxing music",
        )

        resolution = ctx.resolve_follow_up("to'xtat")

        self.assertEqual(resolution["resolved_intent"], "media_pause")
        self.assertEqual(resolution["parameter_hints"]["target_app"], "ChatGPT Atlas")

    def test_browser_page_then_yop_resolves_to_browser_close(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Chrome'da example.com och",
            assistant_intent="browser go_to: example.com",
            tool_name="browser_control",
            tool_parameters={
                "action": "go_to",
                "browser": "chrome",
                "url": "https://example.com",
            },
            execution_method="browser_control",
            result="Opened: https://example.com",
        )

        resolution = ctx.resolve_follow_up("yop")

        self.assertFalse(resolution["needs_confirmation"])
        self.assertEqual(resolution["resolved_intent"], "browser_close")
        self.assertEqual(resolution["suggested_tool"], "browser_control")
        self.assertEqual(resolution["suggested_action"], "close_tab")
        self.assertEqual(resolution["parameter_hints"]["browser"], "chrome")

    def test_unknown_context_stop_asks_for_clarification(self):
        ctx = SessionContext()

        resolution = ctx.resolve_follow_up("to'xtat")

        self.assertTrue(resolution["needs_confirmation"])
        self.assertEqual(resolution["confidence"], "low")
        self.assertEqual(resolution["resolved_intent"], "clarify")

    def test_message_draft_then_yubor_resolves_to_confirm_send_path(self):
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
            result="Message drafted for Ali via Telegram, but recipient/chat was not verified and it was not sent.",
        )

        resolution = ctx.resolve_follow_up("yubor")

        self.assertEqual(resolution["resolved_intent"], "message_send_confirm")
        self.assertEqual(resolution["suggested_tool"], "send_message")
        self.assertTrue(resolution["needs_confirmation"])
        self.assertEqual(resolution["parameter_hints"]["platform"], "Telegram")
        self.assertEqual(resolution["parameter_hints"]["receiver"], "Ali")

    def test_where_sent_followup_reports_message_status_not_send_confirm(self):
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
            result="Message drafted for Ali via Telegram, but recipient/chat was not verified and it was not sent.",
        )

        resolution = ctx.resolve_follow_up("qayerga yubording?")

        self.assertEqual(resolution["resolved_intent"], "message_status")
        self.assertFalse(resolution["needs_confirmation"])

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

    def test_gpt_atlas_correction_updates_previous_media_target(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="YouTube'dan relaxing music qo'y",
            assistant_intent="youtube play: relaxing music",
            tool_name="youtube_video",
            tool_parameters={
                "action": "play",
                "query": "relaxing music",
                "target_app": "Safari",
            },
            execution_method="youtube_video",
            result="Playing: relaxing music",
        )

        attached = ctx.observe_user_text("GPT Atlas’da")
        resolution = ctx.resolve_follow_up("to‘xtat")

        self.assertTrue(attached)
        self.assertEqual(ctx.to_dicts()[-1]["target_app"], "ChatGPT Atlas")
        self.assertEqual(resolution["parameter_hints"]["target_app"], "ChatGPT Atlas")

    def test_still_playing_correction_marks_media_stop_failed_and_sets_stronger_fallback(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="to'xtat",
            assistant_intent="media pause: ChatGPT Atlas",
            tool_name="media_control",
            tool_parameters={
                "action": "pause",
                "target_app": "ChatGPT Atlas",
                "target_context": "YouTube playback",
            },
            execution_method="media_control",
            result="Mac media pause command sent for ChatGPT Atlas, but playback status was not verified.",
        )

        attached = ctx.observe_user_text("hali ham o‘ynayapti")
        resolution = ctx.resolve_follow_up("hali ham o‘ynayapti")
        record = ctx.to_dicts()[-1]

        self.assertTrue(attached)
        self.assertEqual(record["result_status"], "failed")
        self.assertFalse(record["verified"])
        self.assertEqual(resolution["resolved_intent"], "media_pause")
        self.assertEqual(resolution["parameter_hints"]["fallback_level"], "stronger")

    def test_unverified_media_stop_claim_is_uncertain(self):
        status, verified = infer_result_status(
            "media_control",
            "Mac media pause command sent for ChatGPT Atlas, but playback status was not verified.",
        )

        self.assertEqual(status, "uncertain")
        self.assertFalse(verified)
        self.assertEqual(truthful_claim(status, verified), "Aniq tasdiqlay olmadim.")


if __name__ == "__main__":
    unittest.main()

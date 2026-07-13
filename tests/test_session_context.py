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


class UniversalActionContextTests(unittest.TestCase):
    def _open_app(self, ctx, app, result=None):
        ctx.record_action(
            user_text=f"{app}ni och",
            assistant_intent=f"open app: {app}",
            tool_name="open_app",
            tool_parameters={"app_name": app},
            execution_method="open_app",
            result=result or f"Opened {app}.",
        )

    def test_open_app_then_yop_resolves_to_app_close(self):
        ctx = SessionContext()
        self._open_app(ctx, "Telegram")

        resolution = ctx.resolve_follow_up("endi yop")

        self.assertEqual(resolution["resolved_intent"], "app_close")
        self.assertEqual(resolution["suggested_tool"], "close_app")
        self.assertEqual(resolution["parameter_hints"]["app_name"], "Telegram")
        # We opened it and the open was verified → close directly, no confirm.
        self.assertFalse(resolution["needs_confirmation"])

    def test_unverified_open_then_close_requires_confirmation(self):
        ctx = SessionContext()
        self._open_app(
            ctx,
            "Telegram",
            result="Could not confirm that Telegram launched. It may still be loading.",
        )

        resolution = ctx.resolve_follow_up("yop")

        self.assertEqual(resolution["resolved_intent"], "app_close")
        self.assertTrue(resolution["needs_confirmation"])

    def test_unrelated_action_does_not_lose_app_close_target(self):
        ctx = SessionContext()
        self._open_app(ctx, "WhatsApp")
        ctx.record_action(
            user_text="statistikani ayt",
            assistant_intent="personal operations briefing",
            tool_name="personal_briefing",
            tool_parameters={"sources": ["local_projects"]},
            execution_method="personal_briefing",
            result="[PERSONAL_OPERATIONS_BRIEFING]\nstatus=ok",
        )

        resolution = ctx.resolve_follow_up("WhatsAppni yop")

        self.assertEqual(resolution["resolved_intent"], "app_close")
        self.assertEqual(resolution["parameter_hints"]["app_name"], "WhatsApp")

    def test_unrelated_action_does_not_lose_media_target(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="GPT Atlas'da YouTube relaxing music qo'y",
            assistant_intent="youtube play",
            tool_name="youtube_video",
            tool_parameters={"action": "play", "query": "relaxing music", "target_app": "ChatGPT Atlas"},
            execution_method="youtube_video",
            result="Playing: relaxing music",
        )
        ctx.record_action(
            user_text="statistikani ayt",
            assistant_intent="personal operations briefing",
            tool_name="personal_briefing",
            tool_parameters={"sources": ["local_projects"]},
            execution_method="personal_briefing",
            result="[PERSONAL_OPERATIONS_BRIEFING]\nstatus=ok",
        )

        resolution = ctx.resolve_follow_up("musiqani to'xtat")

        self.assertEqual(resolution["resolved_intent"], "media_pause")
        self.assertEqual(resolution["parameter_hints"]["target_app"], "ChatGPT Atlas")

    def test_media_pause_then_davom_ettir_resolves_to_resume(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="to'xtat",
            assistant_intent="media pause",
            tool_name="media_control",
            tool_parameters={"action": "pause", "target_app": "ChatGPT Atlas"},
            execution_method="media_control",
            result="Media paused and verified for ChatGPT Atlas.",
        )

        resolution = ctx.resolve_follow_up("davom ettir")

        self.assertEqual(resolution["resolved_intent"], "media_resume")
        self.assertEqual(resolution["suggested_tool"], "media_control")
        self.assertEqual(resolution["suggested_action"], "play_pause")
        self.assertEqual(resolution["parameter_hints"]["target_app"], "ChatGPT Atlas")

    def test_browser_page_then_orqaga_qayt_resolves_to_back(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Chrome'da example.com och",
            assistant_intent="browser go_to",
            tool_name="browser_control",
            tool_parameters={"action": "go_to", "browser": "chrome", "url": "https://example.com"},
            execution_method="browser_control",
            result="Opened: https://example.com",
        )

        resolution = ctx.resolve_follow_up("orqaga qayt")

        self.assertEqual(resolution["resolved_intent"], "browser_back")
        self.assertEqual(resolution["suggested_tool"], "browser_control")
        self.assertEqual(resolution["suggested_action"], "back")
        self.assertEqual(resolution["parameter_hints"]["browser"], "chrome")

    def test_file_open_then_tahrir_qil_targets_same_file(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="NEXT_STEPS.md'ni och",
            assistant_intent="file read",
            tool_name="file_controller",
            tool_parameters={"action": "read", "path": "/proj/NEXT_STEPS.md"},
            execution_method="file_controller",
            result="File read: /proj/NEXT_STEPS.md (contents)",
        )

        resolution = ctx.resolve_follow_up("endi tahrir qil")

        self.assertEqual(resolution["resolved_intent"], "file_edit_target")
        self.assertTrue(resolution["parameter_hints"]["path"].endswith("NEXT_STEPS.md"))

    def test_reminder_then_vaqtini_ozgartir_finds_reminder(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="ertaga 9 da eslat",
            assistant_intent="reminder create",
            tool_name="reminder",
            tool_parameters={"date": "2026-07-14", "time": "09:00", "message": "Doctor"},
            execution_method="reminder",
            result="Reminder set for 2026-07-14 09:00.",
        )

        resolution = ctx.resolve_follow_up("vaqtini o'zgartir")

        self.assertEqual(resolution["resolved_intent"], "reminder_reschedule")
        self.assertEqual(resolution["parameter_hints"]["message"], "Doctor")
        self.assertTrue(resolution.get("reschedule_recreates"))

    def test_yana_qil_repeats_safe_action_without_confirmation(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Toshkent ob-havosi",
            assistant_intent="web search",
            tool_name="web_search",
            tool_parameters={"query": "Tashkent weather", "mode": "search"},
            execution_method="web_search",
            result="Search results: ...",
        )

        resolution = ctx.resolve_follow_up("yana qil")

        self.assertEqual(resolution["resolved_intent"], "repeat")
        self.assertFalse(resolution["needs_confirmation"])
        self.assertTrue(resolution["repeat_safe"])

    def test_yana_qil_on_dangerous_action_requires_confirmation(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Aliga yozib yubor",
            assistant_intent="send message",
            tool_name="send_message",
            tool_parameters={"platform": "Telegram", "receiver": "Ali", "message_text": "hi"},
            execution_method="send_message",
            result="verified sent to Ali via Telegram",
        )

        resolution = ctx.resolve_follow_up("yana qil")

        self.assertEqual(resolution["resolved_intent"], "repeat")
        self.assertTrue(resolution["needs_confirmation"])
        self.assertFalse(resolution["repeat_safe"])

    def test_undo_open_app_maps_to_close_app(self):
        ctx = SessionContext()
        self._open_app(ctx, "Telegram")

        resolution = ctx.resolve_follow_up("orqaga qaytar")

        self.assertEqual(resolution["resolved_intent"], "undo")
        self.assertEqual(resolution["undo_action"]["tool"], "close_app")
        self.assertEqual(resolution["undo_action"]["args"]["app_name"], "Telegram")

    def test_undo_after_send_message_is_honest_unsupported(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="Aliga yozib yubor",
            assistant_intent="send message",
            tool_name="send_message",
            tool_parameters={"platform": "Telegram", "receiver": "Ali", "message_text": "hi"},
            execution_method="send_message",
            result="verified sent to Ali via Telegram",
        )

        resolution = ctx.resolve_follow_up("orqaga qaytar")

        # A sent message has no truthful undo → cancel path or unsupported, never a fake reversal.
        self.assertIn(resolution["resolved_intent"], {"undo_unsupported", "message_cancel"})
        self.assertIsNone(resolution.get("undo_action"))

    def test_no_context_close_asks_for_clarification(self):
        ctx = SessionContext()

        resolution = ctx.resolve_follow_up("endi yop")

        self.assertEqual(resolution["resolved_intent"], "clarify")
        self.assertTrue(resolution["needs_confirmation"])

    def test_correction_bu_emas_attaches_to_previous_action(self):
        ctx = SessionContext()
        ctx.record_action(
            user_text="YouTube'dan music qo'y",
            assistant_intent="youtube play",
            tool_name="youtube_video",
            tool_parameters={"action": "play", "query": "music", "target_app": "Safari"},
            execution_method="youtube_video",
            result="Playing: music",
        )

        attached = ctx.observe_user_text("bu emas, GPT Atlas'da")

        self.assertTrue(attached)
        self.assertEqual(ctx.to_dicts()[-1]["target_app"], "ChatGPT Atlas")

    def test_stale_media_context_after_eviction_is_not_forced(self):
        ctx = SessionContext(max_actions=5)
        ctx.record_action(
            user_text="YouTube music",
            assistant_intent="youtube play",
            tool_name="youtube_video",
            tool_parameters={"action": "play", "query": "music", "target_app": "ChatGPT Atlas"},
            execution_method="youtube_video",
            result="Playing: music",
        )
        for idx in range(5):
            self._open_app(ctx, f"Notes{idx}")

        resolution = ctx.resolve_follow_up("to'xtat")

        # The media action was evicted from the 5-deep deque; do not blindly reuse it.
        self.assertEqual(resolution["resolved_intent"], "clarify_media_target")
        self.assertTrue(resolution["needs_confirmation"])

    def test_universal_fields_recorded(self):
        ctx = SessionContext()
        self._open_app(ctx, "Telegram")
        record = ctx.to_dicts()[-1]

        self.assertEqual(record["action_category"], "app_lifecycle")
        self.assertEqual(record["action_name"], "open_app")
        self.assertTrue(record["action_id"])
        self.assertTrue(record["reversible"])
        self.assertIn("close", record["available_followups"])
        self.assertEqual(record["undo_action"]["tool"], "close_app")


if __name__ == "__main__":
    unittest.main()

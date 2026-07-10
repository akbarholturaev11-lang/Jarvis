import unittest

from core.briefing_routing import (
    DEFAULT_PERSONAL_SOURCES,
    apply_briefing_route,
    build_briefing_route_hint,
    resolve_briefing_route,
)


class BriefingRoutingTests(unittest.TestCase):
    def test_personal_triggers_use_default_sources(self):
        for text in (
            "men uydaman",
            "Uydaman.",
            "ishga qaytdim",
            "loyihalarimni tekshir",
            "statistikani ayt",
            "personal briefing",
        ):
            with self.subTest(text=text):
                route = resolve_briefing_route(text)
                self.assertEqual(route["tool_name"], "personal_briefing")
                self.assertEqual(
                    route["arguments"],
                    {"sources": list(DEFAULT_PERSONAL_SOURCES)},
                )

    def test_external_statistics_requests_use_only_named_source(self):
        cases = {
            "Telegram kanalim statistikasi qanday?": "telegram",
            "Instagram analyticsni ayt": "instagram",
            "Messenger stats": "messenger",
            "Zerno statistikasi": "zerno",
        }
        for text, source in cases.items():
            with self.subTest(text=text):
                route = resolve_briefing_route(text)
                self.assertEqual(route["tool_name"], "personal_briefing")
                self.assertEqual(
                    route["arguments"],
                    {"sources": [source], "scope": "statistics"},
                )

    def test_explicit_world_news_routes_to_news_search(self):
        for text in (
            "dunyo yangiliklarini ayt",
            "world news",
            "latest news please",
            "мировые новости",
        ):
            with self.subTest(text=text):
                route = resolve_briefing_route(text)
                self.assertEqual(route["tool_name"], "web_search")
                self.assertEqual(
                    route["arguments"],
                    {"mode": "news", "query": "top world news today"},
                )

    def test_unrelated_text_has_no_briefing_route(self):
        self.assertEqual(resolve_briefing_route("Safari och"), {})

    def test_wrong_news_selection_is_rerouted_to_personal_briefing(self):
        tool, args, note = apply_briefing_route(
            "men uydaman",
            "web_search",
            {"mode": "news", "query": "top world news today"},
        )
        self.assertEqual(tool, "personal_briefing")
        self.assertEqual(args, {"sources": list(DEFAULT_PERSONAL_SOURCES)})
        self.assertIn("web_search -> personal_briefing", note)

    def test_world_news_selection_overrides_personal_briefing(self):
        tool, args, note = apply_briefing_route(
            "dunyo yangiliklari",
            "personal_briefing",
            {"sources": list(DEFAULT_PERSONAL_SOURCES)},
        )
        self.assertEqual(tool, "web_search")
        self.assertEqual(
            args,
            {"mode": "news", "query": "top world news today"},
        )
        self.assertIn("personal_briefing -> web_search", note)

    def test_unrelated_route_preserves_tool_and_copies_arguments(self):
        original = {"app_name": "Safari"}
        tool, args, note = apply_briefing_route("Safari och", "open_app", original)
        self.assertEqual(tool, "open_app")
        self.assertEqual(args, original)
        self.assertIsNot(args, original)
        self.assertEqual(note, "")

    def test_implicit_generic_world_news_is_not_allowed(self):
        tool, args, note = apply_briefing_route(
            "What should I focus on now?",
            "web_search",
            {"mode": "news", "query": "top headlines today"},
        )
        self.assertEqual(tool, "web_search")
        self.assertEqual(args["mode"], "search")
        self.assertEqual(args["query"], "What should I focus on now?")
        self.assertIn("implicit generic world news", note)

    def test_explicit_topical_news_keeps_news_mode(self):
        tool, args, note = apply_briefing_route(
            "OpenAI news please",
            "web_search",
            {"mode": "news", "query": "OpenAI"},
        )
        self.assertEqual(tool, "web_search")
        self.assertEqual(args, {"mode": "news", "query": "OpenAI"})
        self.assertEqual(note, "")

    def test_multi_command_preserves_independent_open_app_call(self):
        tool, args, note = apply_briefing_route(
            "men uydaman va Safari och",
            "open_app",
            {"app_name": "Safari"},
        )
        self.assertEqual(tool, "open_app")
        self.assertEqual(args, {"app_name": "Safari"})
        self.assertEqual(note, "")

        tool, args, note = apply_briefing_route(
            "dunyo yangiliklarini ayt va Telegramni och",
            "open_app",
            {"app_name": "Telegram"},
        )
        self.assertEqual(tool, "open_app")
        self.assertEqual(args, {"app_name": "Telegram"})
        self.assertEqual(note, "")

    def test_multi_command_preserves_independent_web_search_calls(self):
        cases = (
            (
                "men uydaman va OpenAI newsni top",
                {"mode": "news", "query": "OpenAI"},
            ),
            (
                "men uydaman va Python hujjatlarini qidir",
                {"mode": "search", "query": "Python documentation"},
            ),
            (
                "Telegram statistikasi va OpenAI yangiliklarini top",
                {"mode": "news", "query": "OpenAI"},
            ),
        )
        for text, original_args in cases:
            with self.subTest(text=text):
                tool, args, note = apply_briefing_route(text, "web_search", original_args)
                self.assertEqual(tool, "web_search")
                self.assertEqual(args, original_args)
                self.assertEqual(note, "")

    def test_compound_route_still_corrects_the_conflicting_news_call(self):
        tool, args, note = apply_briefing_route(
            "men uydaman va keyin ishlarni ko'rsat",
            "web_search",
            {"mode": "news", "query": "top world news today"},
        )
        self.assertEqual(tool, "personal_briefing")
        self.assertEqual(args, {"sources": list(DEFAULT_PERSONAL_SOURCES)})
        self.assertIn("web_search -> personal_briefing", note)

    def test_route_hint_is_internal_and_preserves_payload(self):
        payload = "men uydaman"
        hinted = build_briefing_route_hint(payload, payload)
        self.assertIn("[BRIEFING_ROUTE - internal, do not read aloud]", hinted)
        self.assertIn('"tool_name": "personal_briefing"', hinted)
        self.assertTrue(hinted.endswith(payload))
        self.assertEqual(
            build_briefing_route_hint("Safari och", "original payload"),
            "original payload",
        )


if __name__ == "__main__":
    unittest.main()

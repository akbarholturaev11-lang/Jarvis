"""Named external statistics fall back to the connected Zerno hub.

Instagram/Telegram/Messenger/channels/bots/posts requests must reuse the
connected Zerno hub when their standalone adapter is ``not_configured`` — without
inventing statistics and without letting unrelated Zerno data (for example a
generic ``posts`` group) become fake Instagram numbers.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from actions.personal_briefing import (
    build_source_registry,
    collect_personal_briefing,
    format_personal_briefing,
)
from actions.zerno_stats import normalize_zerno_payload
from core.briefing_routing import resolve_briefing_route


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _connected_zerno(payload: dict) -> "callable":
    report = normalize_zerno_payload(payload)
    assert report["status"] == "connected"
    return lambda parameters=None: report


class ZernoFallbackTests(unittest.TestCase):
    def _registry(self, root: Path, **overrides):
        registry = build_source_registry(root)
        registry.update(overrides)
        return registry

    def test_instagram_falls_back_to_connected_zerno(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(
                root,
                zerno=_connected_zerno({"instagram": {"followers": 144, "posts_count": 12}}),
            )
            report = collect_personal_briefing(
                {"sources": ["instagram"], "scope": "statistics"},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "connected")
        self.assertEqual(instagram["backing_source"], "zerno")
        self.assertFalse(instagram["configured"])
        self.assertEqual(
            instagram["statistics"]["metric_groups"]["instagram"]["followers"], 144
        )
        # The Zerno hub is queried once even though it was not explicitly requested.
        self.assertEqual(set(report["sources"]), {"instagram"})
        rendered = format_personal_briefing(report)
        self.assertIn("instagram: connected", rendered)
        self.assertIn("144", rendered)

    def test_direct_adapter_wins_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(
                root,
                instagram=lambda parameters=None: {
                    "source": "instagram",
                    "status": "connected",
                    "configured": True,
                    "statistics": {"direct_adapter": True},
                    "foyda": ["Direct Instagram adapter data"],
                },
                zerno=_connected_zerno({"instagram": {"followers": 999}}),
            )
            report = collect_personal_briefing(
                {"sources": ["instagram"]},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "connected")
        self.assertTrue(instagram["configured"])
        self.assertNotIn("backing_source", instagram)
        self.assertEqual(instagram["statistics"], {"direct_adapter": True})
        self.assertNotIn("999", json.dumps(instagram, ensure_ascii=False))

    def test_no_zerno_returns_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Real Zerno adapter, no config in tmp -> not_configured, no network.
            registry = self._registry(root)
            report = collect_personal_briefing(
                {"sources": ["instagram"]},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "not_configured")
        self.assertIsNone(instagram["statistics"])
        self.assertFalse(instagram["configured"])
        self.assertFalse(instagram["network_attempted"])
        self.assertRegex(instagram["reason"].lower(), r"api/token/config")

    def test_zerno_posts_without_platform_metadata_do_not_become_fake_instagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(
                root,
                zerno=_connected_zerno({"posts": [{"views": 5}], "users": {"total": 10}}),
            )
            report = collect_personal_briefing(
                {"sources": ["instagram"], "scope": "statistics"},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "not_available")
        self.assertEqual(instagram["backing_source"], "zerno")
        self.assertIsNone(instagram["statistics"])
        self.assertIn("maxsus metrikalar mavjud emas", instagram["reason"])
        rendered = json.dumps(instagram, ensure_ascii=False)
        # No unrelated Zerno numbers or post/user fields leak into Instagram.
        self.assertNotIn("views", rendered)
        self.assertNotIn("total", rendered)
        self.assertNotIn("5", rendered)
        self.assertNotIn("10", rendered)

    def test_same_fallback_applies_to_every_named_source(self):
        cases = {
            "telegram": ({"telegram_channel": {"subscribers": 42}}, "subscribers=42"),
            "messenger": ({"messenger": {"conversations": 7}}, "conversations=7"),
            "channels": ({"telegram_channel": {"subscribers": 21}}, "subscribers=21"),
            "bots": ({"telegram_bot": {"users": 8}}, "users=8"),
            "posts": ({"posts": [{"views": 3}]}, "views=3"),
        }
        for source, (payload, needle) in cases.items():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = self._registry(root, zerno=_connected_zerno(payload))
                report = collect_personal_briefing(
                    {"sources": [source], "scope": "statistics"},
                    project_root=root,
                    registry=registry,
                )
                source_report = report["sources"][source]
                self.assertEqual(source_report["status"], "connected")
                self.assertEqual(source_report["backing_source"], "zerno")
                self.assertIn(needle, json.dumps(source_report, ensure_ascii=False))

    def test_platform_tagged_accounts_and_posts_are_summarized(self):
        # Mirrors the real Zerno contract: accounts/posts carry a `platform` field.
        real_shape = {
            "overview": {"totalPosts": 2},
            "accounts": [
                {"platform": "telegram", "username": "aibotsakbar", "followersCount": None},
                {"platform": "instagram", "username": "hskai_bot", "followersCount": 23},
            ],
            "posts": [
                {"platform": "instagram", "content": "a", "analytics": {"impressions": 34, "reach": 31, "likes": 5, "comments": 4}},
                {"platform": "instagram", "content": "b", "analytics": {"impressions": 10, "reach": 8, "likes": 2, "comments": 1}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(root, zerno=_connected_zerno(real_shape))
            report = collect_personal_briefing(
                {"sources": ["instagram"], "scope": "statistics"},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "connected")
        self.assertEqual(instagram["backing_source"], "zerno")
        entry = instagram["statistics"]["platform_breakdown"]["instagram"]
        self.assertEqual(entry["post_count"], 2)
        self.assertEqual(entry["analytics_totals"]["impressions"], 44)
        self.assertEqual(entry["analytics_totals"]["likes"], 7)
        self.assertEqual(entry["accounts"][0]["followers"], 23)
        rendered = format_personal_briefing(report)
        self.assertIn("hskai_bot: 23 follower", rendered)
        # The Telegram account must not leak into an Instagram request.
        self.assertNotIn("aibotsakbar", rendered)

    def test_telegram_account_without_follower_count_stays_honest(self):
        real_shape = {
            "accounts": [
                {"platform": "telegram", "username": "aibotsakbar", "followersCount": None},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(root, zerno=_connected_zerno(real_shape))
            report = collect_personal_briefing(
                {"sources": ["telegram"], "scope": "statistics"},
                project_root=root,
                registry=registry,
            )

        telegram = report["sources"]["telegram"]
        self.assertEqual(telegram["status"], "connected")
        rendered = format_personal_briefing(report)
        self.assertIn("aibotsakbar", rendered)
        self.assertIn("ko'rsatilmagan", rendered)

    def test_wrong_platform_group_is_not_borrowed_across_sources(self):
        # A Telegram-only Zerno payload must not answer an Instagram request.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._registry(
                root,
                zerno=_connected_zerno({"telegram_channel": {"subscribers": 500}}),
            )
            report = collect_personal_briefing(
                {"sources": ["instagram"], "scope": "statistics"},
                project_root=root,
                registry=registry,
            )

        instagram = report["sources"]["instagram"]
        self.assertEqual(instagram["status"], "not_available")
        self.assertNotIn("500", json.dumps(instagram, ensure_ascii=False))

    def test_named_statistics_requests_route_through_personal_briefing(self):
        cases = {
            "Instagram statistikasi": ["instagram", "zerno"],
            "kanal statistikasi": ["channels", "zerno"],
            "bot statistikasi": ["bots", "zerno"],
            "post statistikasi": ["posts", "zerno"],
        }
        for text, expected_sources in cases.items():
            with self.subTest(text=text):
                route = resolve_briefing_route(text)
                self.assertEqual(route["tool_name"], "personal_briefing")
                self.assertEqual(
                    route["arguments"],
                    {"sources": expected_sources, "scope": "statistics"},
                )

    def test_zerno_config_and_token_files_remain_gitignored(self):
        for relative_path in ("config/briefing_sources.json", "config/local_env.zsh"):
            with self.subTest(relative_path=relative_path):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", relative_path],
                    cwd=PROJECT_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{relative_path} must remain ignored by Git",
                )


if __name__ == "__main__":
    unittest.main()

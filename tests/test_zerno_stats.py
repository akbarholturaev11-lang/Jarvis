from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from actions.personal_briefing import (
    collect_personal_briefing,
    format_personal_briefing,
)
from actions.zerno_stats import (
    _NoRedirectHandler,
    collect_zerno_source,
    normalize_zerno_payload,
)
from core.briefing_routing import resolve_briefing_route
from scripts import check_zerno_stats


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "unit-test-zerno-token-never-real"


class _JsonResponse:
    def __init__(self, payload, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.closed = False

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class ZernoStatsTests(unittest.TestCase):
    @staticmethod
    def _write_config(root: Path, api_url: str) -> None:
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "briefing_sources.json").write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "Zerno Operations Hub",
                            "type": "zerno",
                            "enabled": True,
                            "api_base_url": api_url,
                            "token_env": "ZERNO_API_TOKEN",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_local_project(root: Path) -> None:
        (root / "PROJECT_MEMORY.md").write_text(
            "# Purpose\nTruthful local operations briefing.\n",
            encoding="utf-8",
        )
        (root / "PROJECT_MAP.md").write_text(
            "# Architecture\n- Existing personal briefing adapter.\n",
            encoding="utf-8",
        )
        (root / "NEXT_STEPS.md").write_text(
            "# Next Steps\n1. Verify local project evidence.\n",
            encoding="utf-8",
        )

    def test_no_config_is_not_configured_without_network(self):
        opener_called = False

        def opener(*_args, **_kwargs):
            nonlocal opener_called
            opener_called = True
            raise AssertionError("network must not be attempted")

        with tempfile.TemporaryDirectory() as tmp:
            report = collect_zerno_source(
                Path(tmp), environ={}, opener=opener
            )

        self.assertEqual(report["status"], "not_configured")
        self.assertFalse(report["configured"])
        self.assertFalse(report["network_attempted"])
        self.assertIsNone(report["statistics"])
        self.assertFalse(opener_called)

    def test_placeholder_api_url_is_not_configured_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "PASTE_ZERNO_API_URL_HERE")

            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: self.fail("network attempted"),
            )

        self.assertEqual(report["status"], "not_configured")
        self.assertFalse(report["network_attempted"])
        self.assertEqual(report["metrics"], {})

    def test_missing_token_is_not_configured_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")

            report = collect_zerno_source(
                root,
                environ={},
                opener=lambda *_args, **_kwargs: self.fail("network attempted"),
            )

        self.assertEqual(report["status"], "not_configured")
        self.assertFalse(report["network_attempted"])
        self.assertIn("local_env.zsh", report["reason"])
        self.assertIn("ZERNO_API_TOKEN", report["reason"])

    def test_api_failure_is_failed_and_contains_no_fake_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")

            def failing_opener(*_args, **_kwargs):
                raise URLError("temporary DNS failure")

            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=failing_opener,
            )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["network_attempted"])
        self.assertEqual(report["metrics"], {})
        self.assertIsNone(report["statistics"])
        self.assertEqual(report["latest_updates"], [])
        self.assertNotIn(FAKE_TOKEN, json.dumps(report, ensure_ascii=False))
        self.assertIn("temporary DNS failure", report["reason"])

    def test_mocked_dict_json_is_connected_and_metrics_are_parsed(self):
        payload = {
            "status": "healthy",
            "telegram_channel": {"subscribers": 321, "growth_delta": 7},
            "telegram_bot": {"users": 88},
            "instagram": {"followers": 144},
            "messenger": {"conversations": 12},
            "leads": 9,
            "payments": {"count": 4},
            "revenue": {"amount": 125.5, "currency": "USD"},
            "foyda": ["Verified conversion growth"],
            "zarar": ["Verified delivery risk"],
            "next_action": "Review the verified lead funnel.",
            "top_priorities": ["Check payments", "Review bot usage"],
        }
        response = _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")
            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: response,
            )

        self.assertEqual(report["status"], "connected")
        self.assertEqual(report["overall_status"], "healthy")
        self.assertEqual(report["metrics"]["telegram_channel"]["subscribers"], 321)
        self.assertEqual(report["metrics"]["revenue"]["amount"], 125.5)
        self.assertIn("telegram_channel", report["metric_groups"])
        self.assertIn("revenue", report["metric_groups"])
        self.assertGreater(report["metric_count"], 0)
        self.assertEqual(report["next_action"], "Review the verified lead funnel.")
        self.assertTrue(response.closed)

    def test_mocked_list_json_is_connected_and_metrics_are_parsed(self):
        payload = [
            {"telegram_channel": {"subscribers": 12}},
            {"users": {"total": 34}, "growth": {"change": 2}},
            {"custom_metric": 56},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")
            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: _JsonResponse(payload),
            )

        self.assertEqual(report["status"], "connected")
        self.assertEqual(report["metrics"]["telegram_channel"]["subscribers"], 12)
        self.assertEqual(report["metrics"]["users"]["total"], 34)
        self.assertIn("items.2.custom_metric", report["metrics"]["additional_fields"])

    def test_missing_optional_fields_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")
            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: _JsonResponse({}),
            )

        self.assertEqual(report["status"], "connected")
        self.assertEqual(report["metrics"], {})
        self.assertEqual(report["latest_updates"], [])
        self.assertEqual(report["foyda"], [])
        self.assertEqual(report["zarar"], [])
        self.assertTrue(report["next_action"])
        self.assertIn("last_checked_at", report)

    def test_personal_briefing_includes_connected_zerno_source(self):
        payload = {
            "overall_status": "operational",
            "telegram_channel": {"subscribers": 42},
            "leads": {"new": 3},
            "next_action": "Follow up verified leads.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")
            with (
                patch.dict(os.environ, {"ZERNO_API_TOKEN": FAKE_TOKEN}),
                patch(
                    "actions.zerno_stats.urlopen",
                    side_effect=lambda *_args, **_kwargs: _JsonResponse(payload),
                ),
            ):
                report = collect_personal_briefing(
                    {"sources": ["zerno"], "scope": "statistics"},
                    project_root=root,
                )

        self.assertEqual(report["sources"]["zerno"]["status"], "connected")
        self.assertEqual(report["status"], "available")
        rendered = format_personal_briefing(report)
        self.assertIn("zerno: connected", rendered)
        self.assertIn("Overall status: operational", rendered)
        self.assertIn("Telegram channel: subscribers=42", rendered)
        self.assertIn("Leads: new=3", rendered)

    def test_missing_social_groups_are_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://zerno.invalid/api/stats")
            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: _JsonResponse(
                    {"users": {"total": 10}}
                ),
            )

        self.assertEqual(report["status"], "connected")
        for absent_group in ("telegram_channel", "telegram_bot", "instagram", "messenger"):
            self.assertNotIn(absent_group, report["metrics"])
            self.assertNotIn(absent_group, report["metric_groups"])
        rendered = format_personal_briefing(
            {
                "status": "available",
                "sources": {"zerno": report},
                "foyda": [],
                "zarar": [],
                "next_action": report["next_action"],
            }
        )
        self.assertNotIn("Telegram channel:", rendered)
        self.assertNotIn("Telegram bot:", rendered)
        self.assertNotIn("Instagram:", rendered)
        self.assertNotIn("Messenger:", rendered)
        self.assertIn("Users: total=10", rendered)

    def test_real_zerno_config_and_local_env_files_are_gitignored(self):
        for relative_path in (
            "config/briefing_sources.json",
            "config/local_env.zsh",
            "config/.briefing_sources.json.fixture",
            "config/.local_env.zsh.fixture",
        ):
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

    def test_setup_script_writes_token_only_to_restricted_local_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "config").mkdir()
            shutil.copy2(
                PROJECT_ROOT / "scripts" / "setup_zerno_stats.sh",
                root / "scripts" / "setup_zerno_stats.sh",
            )
            (root / ".gitignore").write_text(
                "config/briefing_sources.json\n"
                "config/local_env.zsh\n"
                "config/.briefing_sources.json.*\n"
                "config/.local_env.zsh.*\n",
                encoding="utf-8",
            )
            (root / "config" / "briefing_sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {"name": "Local fixture", "type": "local_project"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "config" / "local_env.zsh").write_text(
                "export SAFE_FIXTURE=1\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                ["bash", "-x", str(root / "scripts" / "setup_zerno_stats.sh")],
                cwd=root,
                input=f"https://zerno.invalid/api/stats\n{FAKE_TOKEN}\n",
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
            config_text = (root / "config" / "briefing_sources.json").read_text(
                encoding="utf-8"
            )
            env_text = (root / "config" / "local_env.zsh").read_text(encoding="utf-8")
            self.assertNotIn(FAKE_TOKEN, config_text)
            self.assertIn('"type": "local_project"', config_text)
            self.assertIn('"type": "zerno"', config_text)
            self.assertIn("export SAFE_FIXTURE=1", env_text)
            self.assertIn("export ZERNO_API_TOKEN=", env_text)
            self.assertIn(FAKE_TOKEN, env_text)
            self.assertEqual(
                stat.S_IMODE((root / "config" / "local_env.zsh").stat().st_mode),
                0o600,
            )

    def test_check_script_never_prints_full_token(self):
        def collector(**_kwargs):
            return {
                "status": "failed",
                "configured": True,
                "reason": f"Bearer {FAKE_TOKEN} rejected token={FAKE_TOKEN}",
                "metric_groups": [],
                "metric_count": 0,
            }

        output = io.StringIO()
        with (
            patch.dict(os.environ, {"ZERNO_API_TOKEN": FAKE_TOKEN}),
            contextlib.redirect_stdout(output),
        ):
            exit_code = check_zerno_stats.main([], collector=collector)

        rendered = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_check_script_debug_output_still_redacts_full_token(self):
        secondary_secret = "secondary-secret-must-not-print"

        def collector(**_kwargs):
            return {
                "status": "connected",
                "configured": True,
                "reason": "connected",
                "metric_groups": [],
                "metric_count": 1,
                "debug_payload": {
                    FAKE_TOKEN: f"echo={FAKE_TOKEN}",
                    "apiKey": secondary_secret,
                },
            }

        output = io.StringIO()
        with (
            patch.dict(os.environ, {"ZERNO_API_TOKEN": FAKE_TOKEN}),
            contextlib.redirect_stdout(output),
        ):
            exit_code = check_zerno_stats.main(["--debug"], collector=collector)

        self.assertEqual(exit_code, 0)
        self.assertNotIn(FAKE_TOKEN, output.getvalue())
        self.assertNotIn(secondary_secret, output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_secret_like_camel_case_fields_are_removed(self):
        report = normalize_zerno_payload(
            {
                "apiKey": "foreign-api-key",
                "accessToken": "foreign-access-token",
                "clientSecret": "foreign-client-secret",
                "custom_metric": 7,
            },
            token=FAKE_TOKEN,
            include_debug=True,
        )
        rendered = json.dumps(report, ensure_ascii=False)

        self.assertNotIn("foreign-api-key", rendered)
        self.assertNotIn("foreign-access-token", rendered)
        self.assertNotIn("foreign-client-secret", rendered)
        self.assertEqual(report["metrics"]["additional_fields"]["custom_metric"], 7)

    def test_malformed_api_url_fails_without_crashing_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, "https://[::1")
            report = collect_zerno_source(
                root,
                environ={"ZERNO_API_TOKEN": FAKE_TOKEN},
                opener=lambda *_args, **_kwargs: self.fail("network attempted"),
            )

        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["network_attempted"])

    def test_nested_group_fields_are_not_promoted_or_duplicated(self):
        report = normalize_zerno_payload(
            {
                "telegram_bot": {"users": 88, "errors": 2},
                "latest_updates": ["Bot checked"],
                "top_priorities": ["Review bot"],
            }
        )

        self.assertEqual(report["metric_groups"], ["telegram_bot"])
        self.assertNotIn("users", report["metrics"])
        self.assertNotIn("errors", report["metrics"])
        self.assertNotIn("latest_updates.0", report["metrics"].get("additional_fields", {}))
        self.assertNotIn("top_priorities.0", report["metrics"].get("additional_fields", {}))

    def test_camel_case_groups_and_control_fields_are_normalized(self):
        report = normalize_zerno_payload(
            {
                "telegramChannel": {"followers": 21},
                "activeUsers": 8,
                "latestUpdates": ["Verified update"],
                "nextAction": "Review verified activity.",
                "topPriorities": ["Check retention"],
            }
        )

        self.assertEqual(report["metrics"]["telegram_channel"]["followers"], 21)
        self.assertEqual(report["metrics"]["active_users"], 8)
        self.assertEqual(report["latest_updates"], ["Verified update"])
        self.assertEqual(report["next_action"], "Review verified activity.")
        self.assertIn("Check retention", report["top_priorities"])

    def test_repeated_list_groups_keep_consistent_occurrences(self):
        report = normalize_zerno_payload(
            [
                {"posts": [{"views": 1}]},
                {"posts": [{"views": 2}]},
            ]
        )

        self.assertEqual(
            report["metrics"]["posts"],
            [[{"views": 1}], [{"views": 2}]],
        )

    def test_numeric_zero_confidence_is_preserved(self):
        report = normalize_zerno_payload({"confidence": 0})

        self.assertEqual(report["confidence"], "0")

    def test_camel_case_overall_status_is_preserved(self):
        report = normalize_zerno_payload({"data": {"overallStatus": "healthy"}})

        self.assertEqual(report["overall_status"], "healthy")

    def test_decrease_metrics_are_classified_by_metric_direction(self):
        report = normalize_zerno_payload(
            {
                "users": {"users_decrease": 12},
                "errors": {"errors_decrease": 7},
            }
        )

        self.assertIn("users.users_decrease=12", report["zarar"])
        self.assertIn("errors.errors_decrease=7", report["foyda"])

    def test_default_http_handler_rejects_redirects(self):
        handler = _NoRedirectHandler()

        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://other.test")
        )

    def test_existing_local_project_briefing_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_project(root)
            report = collect_personal_briefing(
                {"sources": ["local_projects"]},
                project_root=root,
            )

        local = report["sources"]["local_projects"]
        self.assertEqual(local["status"], "available")
        self.assertEqual(
            local["documents_read"],
            ["PROJECT_MEMORY.md", "PROJECT_MAP.md", "NEXT_STEPS.md"],
        )
        self.assertIn("Verify local project evidence", report["next_action"])

    def test_men_uydaman_still_routes_to_personal_briefing(self):
        route = resolve_briefing_route("men uydaman")
        self.assertEqual(route["tool_name"], "personal_briefing")

    def test_world_news_still_routes_to_news_web_search(self):
        route = resolve_briefing_route("dunyo yangiliklari")
        self.assertEqual(route["tool_name"], "web_search")
        self.assertEqual(route["arguments"]["mode"], "news")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from actions.personal_briefing import (
    build_source_registry,
    collect_personal_briefing,
    format_personal_briefing,
    personal_briefing,
)


class PersonalBriefingTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "PROJECT_MEMORY.md").write_text(
            "# Project purpose\nBuild a truthful local operations briefing.\n",
            encoding="utf-8",
        )
        (root / "PROJECT_MAP.md").write_text(
            "# Architecture\n- Existing action dispatcher\n",
            encoding="utf-8",
        )
        (root / "NEXT_STEPS.md").write_text(
            "# Next Steps\n## Immediate Next Steps\n1. Verify the Personal Briefing route.\n",
            encoding="utf-8",
        )

    def test_external_sources_are_explicitly_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)

            report = collect_personal_briefing(project_root=root)

            for source in ("telegram", "instagram", "messenger", "zerno"):
                source_report = report["sources"][source]
                self.assertEqual(source_report["status"], "not_configured")
                self.assertIsNone(source_report["statistics"])
                self.assertFalse(source_report["configured"])
                self.assertFalse(source_report["network_attempted"])
                self.assertRegex(source_report["reason"].lower(), r"api/token/config")

            output = format_personal_briefing(report)
            self.assertTrue(output.startswith("[PERSONAL_OPERATIONS_BRIEFING]"))
            self.assertIn("Foyda:", output)
            self.assertIn("Zarar:", output)
            self.assertIn("Next action:", output)
            self.assertIn("not_configured", output)

    def test_reads_only_allowlisted_docs_and_never_private_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            (root / "config").mkdir()
            (root / "memory").mkdir()
            (root / "config" / "api_keys.json").write_text(
                json.dumps({"token": "TOP_SECRET_TOKEN_918273"}),
                encoding="utf-8",
            )
            (root / "memory" / "long_term.json").write_text(
                json.dumps({"private": "PRIVATE_MEMORY_564738"}),
                encoding="utf-8",
            )
            (root / "PRIVATE_NOTES.md").write_text(
                "NON_ALLOWLISTED_CONTENT_102938",
                encoding="utf-8",
            )

            report = collect_personal_briefing(project_root=root)
            rendered = json.dumps(report, ensure_ascii=False) + format_personal_briefing(report)

            self.assertNotIn("TOP_SECRET_TOKEN_918273", rendered)
            self.assertNotIn("PRIVATE_MEMORY_564738", rendered)
            self.assertNotIn("NON_ALLOWLISTED_CONTENT_102938", rendered)
            documents = report["sources"]["local_projects"]["documents_read"]
            self.assertEqual(
                documents,
                ["PROJECT_MEMORY.md", "PROJECT_MAP.md", "NEXT_STEPS.md"],
            )
            self.assertIn("Verify the Personal Briefing route", report["next_action"])

    def test_allowlisted_symlink_cannot_escape_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            outside = base / "outside-secret.txt"
            outside.write_text("SYMLINK_SECRET_112233", encoding="utf-8")
            (root / "PROJECT_MEMORY.md").symlink_to(outside)
            (root / "NEXT_STEPS.md").write_text(
                "# Next Steps\n1. Add verified evidence.\n",
                encoding="utf-8",
            )

            report = collect_personal_briefing(project_root=root)
            rendered = json.dumps(report, ensure_ascii=False)

            self.assertNotIn("SYMLINK_SECRET_112233", rendered)
            self.assertNotIn(
                "PROJECT_MEMORY.md",
                report["sources"]["local_projects"]["documents_read"],
            )

    def test_git_is_read_only_counts_only_and_does_not_expose_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )
            sensitive_name = "private-token-filename.txt"
            (root / sensitive_name).write_text("not read", encoding="utf-8")

            report = collect_personal_briefing(project_root=root)
            git = report["sources"]["local_projects"]["git"]
            output = format_personal_briefing(report)

            self.assertEqual(git["status"], "available")
            self.assertGreaterEqual(git["changed_entry_count"], 1)
            self.assertNotIn(sensitive_name, json.dumps(report, ensure_ascii=False))
            self.assertNotIn(str(root), output)

    def test_no_fake_numeric_external_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            report = collect_personal_briefing(project_root=root)
            output = format_personal_briefing(report).lower()

            for source in ("telegram", "instagram", "messenger", "zerno"):
                self.assertIsNone(report["sources"][source]["statistics"])
                line = next(line for line in output.splitlines() if line.startswith(f"- {source}:"))
                self.assertIsNone(re.search(r"\b\d+(?:[.,]\d+)?\b", line))

    def test_single_source_statistics_scope_stays_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = collect_personal_briefing(
                {"sources": ["telegram"], "scope": "statistics"},
                project_root=root,
            )

            self.assertEqual(set(report["sources"]), {"telegram"})
            self.assertEqual(report["sources"]["telegram"]["status"], "not_configured")
            self.assertIsNone(report["sources"]["telegram"]["statistics"])
            self.assertEqual(report["scope"], "statistics")
            self.assertNotIn("Local loyiha dalillari", " ".join(report["zarar"]))
            self.assertIn("telegram", report["next_action"])
            self.assertIn("API/token/config", report["next_action"])

    def test_registry_and_action_wrapper_use_expected_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            registry = build_source_registry(root)

            self.assertEqual(
                set(registry),
                {"local_projects", "telegram", "instagram", "messenger", "zerno"},
            )
            output = personal_briefing(
                {"sources": ["local_projects", "zerno"]},
                project_root=root,
                registry=registry,
            )
            self.assertIn("Personal Operations Briefing", output)
            self.assertIn("local_projects: available", output)
            self.assertIn("zerno: not_configured", output)


if __name__ == "__main__":
    unittest.main()

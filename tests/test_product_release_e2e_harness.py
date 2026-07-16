from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from tests.product_release_e2e_support import (
    ALLOWED_STATUSES,
    EVIDENCE_GROUPS,
    EXTERNAL_GATES,
    SCENARIOS,
    EvidenceResult,
    EvidenceRunner,
    build_report,
    completed_process,
    evidence_groups_for,
    not_run_results,
    select_scenarios,
    validate_catalog,
    write_reports,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)


def _results(status: str = "pass") -> tuple[EvidenceResult, ...]:
    return tuple(
        EvidenceResult(
            group.group_id,
            status,  # type: ignore[arg-type]
            ("python", "-m", "pytest", "-q", "-ra", *group.node_ids),
            0 if status == "pass" else 1 if status == "fail" else None,
            1,
            "",
            "fixture",
        )
        for group in EVIDENCE_GROUPS
    )


class ProductReleaseE2EHarnessTests(unittest.TestCase):
    def test_catalog_maps_every_scenario_exactly_once_to_real_evidence(self) -> None:
        self.assertEqual(validate_catalog(PROJECT_ROOT), ())
        self.assertEqual([scenario.scenario_id for scenario in SCENARIOS], list(range(1, 31)))
        self.assertEqual(len({scenario.scenario_id for scenario in SCENARIOS}), 30)
        self.assertTrue(all(scenario.evidence_groups for scenario in SCENARIOS))

    def test_group_selection_deduplicates_reused_evidence(self) -> None:
        scenarios = select_scenarios((3, 4, 5, 6, 8, 9, 10, 12))
        groups = evidence_groups_for(scenarios)
        group_ids = [group.group_id for group in groups]
        self.assertEqual(len(group_ids), len(set(group_ids)))
        self.assertEqual(group_ids.count("initial_purchase"), 1)

    def test_runner_uses_fixed_argv_without_a_shell(self) -> None:
        executor = Mock(
            return_value=completed_process(returncode=0, stdout="1 passed")
        )
        runner = EvidenceRunner(PROJECT_ROOT, executor=executor)
        with patch.dict(
            os.environ,
            {
                "PATH": os.environ.get("PATH", ""),
                "JARVIS_ADMIN_SESSION_SECRET_B64URL": "must-not-be-inherited",
                "PYTEST_ADDOPTS": "--ignore=required-evidence",
            },
            clear=True,
        ):
            result = runner.run_group(EVIDENCE_GROUPS[0])

        self.assertEqual(result.status, "pass")
        positional, keywords = executor.call_args
        command = positional[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[1:4], ["-m", "pytest", "-q"])
        self.assertIs(keywords["shell"], False)
        self.assertIs(keywords["check"], False)
        self.assertEqual(keywords["cwd"], PROJECT_ROOT)
        self.assertTrue(all(isinstance(argument, str) for argument in command))
        self.assertFalse(any(argument in {"sh", "bash", "zsh", "cmd.exe"} for argument in command))
        self.assertEqual(keywords["env"], {"PATH": os.environ.get("PATH", "")})

    def test_runner_reports_all_skipped_evidence_as_not_available(self) -> None:
        executor = Mock(
            return_value=completed_process(
                returncode=0,
                stdout="1 skipped in 0.01s",
            )
        )
        result = EvidenceRunner(PROJECT_ROOT, executor=executor).run_group(
            EVIDENCE_GROUPS[0]
        )
        self.assertEqual(result.status, "not_available")
        self.assertIn("skipped", result.reason)

    def test_runner_never_passes_partial_or_expected_failed_evidence(self) -> None:
        fixtures = (
            ("1 passed, 1 skipped in 0.01s", "not_available"),
            ("1 passed, 1 xfailed in 0.01s", "not_available"),
            ("1 passed, 1 deselected in 0.01s", "not_available"),
            ("1 xpassed in 0.01s", "fail"),
            ("1 warning in 0.01s", "fail"),
        )
        for stdout, expected in fixtures:
            with self.subTest(stdout=stdout):
                executor = Mock(
                    return_value=completed_process(returncode=0, stdout=stdout)
                )
                result = EvidenceRunner(
                    PROJECT_ROOT,
                    executor=executor,
                ).run_group(EVIDENCE_GROUPS[0])
                self.assertEqual(result.status, expected)

    def test_runner_never_persists_captured_subprocess_output(self) -> None:
        executor = Mock(
            return_value=completed_process(
                returncode=1,
                stderr=(
                    'license_key="shortvalue"\n'
                    "X-Purchase-Grant: shortvalue\n"
                    'password: "two word secret"\nassertion failed'
                ),
            )
        )
        result = EvidenceRunner(PROJECT_ROOT, executor=executor).run_group(
            EVIDENCE_GROUPS[0]
        )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.output, "")

    def test_passing_local_evidence_never_clears_external_or_internal_gates(self) -> None:
        report = build_report(SCENARIOS, _results(), generated_at=NOW)
        scenario_by_id = {item["id"]: item for item in report["scenarios"]}

        self.assertFalse(report["production_ready"])
        self.assertFalse(report["production_verified"])
        self.assertEqual(
            {item["status"] for item in report["external_gates"]},
            {"not_available"},
        )
        self.assertEqual(len(report["external_gates"]), len(EXTERNAL_GATES))
        for scenario_id in (1, 7, 15, 23, 24, 25, 26, 27, 30):
            self.assertEqual(scenario_by_id[scenario_id]["status"], "not_available")
            self.assertEqual(scenario_by_id[scenario_id]["local_evidence_status"], "pass")
            self.assertFalse(scenario_by_id[scenario_id]["production_verified"])
        self.assertEqual(scenario_by_id[2]["status"], "pass")

    def test_failed_evidence_takes_precedence_over_not_available_blocker(self) -> None:
        results = list(_results())
        updater_index = next(
            index
            for index, result in enumerate(results)
            if result.group_id == "updater_transaction"
        )
        results[updater_index] = EvidenceResult(
            "updater_transaction",
            "fail",
            ("python", "-m", "pytest"),
            1,
            2,
            "sanitized",
            "fixture failure",
        )
        report = build_report(SCENARIOS, results, generated_at=NOW)
        scenario_by_id = {item["id"]: item for item in report["scenarios"]}
        self.assertEqual(scenario_by_id[23]["status"], "fail")
        self.assertEqual(scenario_by_id[27]["status"], "fail")

    def test_plan_only_results_are_not_run_and_never_pass(self) -> None:
        groups = evidence_groups_for(select_scenarios((1, 2, 3)))
        report = build_report(
            select_scenarios((1, 2, 3)),
            not_run_results(groups),
            generated_at=NOW,
        )
        self.assertEqual({item["status"] for item in report["scenarios"]}, {"not_run"})

    def test_reports_are_private_atomic_json_and_markdown(self) -> None:
        report = build_report(SCENARIOS, _results(), generated_at=NOW)
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "report"
            json_path, markdown_path = write_reports(report, output_dir)
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertEqual(loaded["schema"], "jarvis-product-release-e2e/v1")
            self.assertEqual(len(loaded["scenarios"]), 30)
            self.assertTrue(
                all(
                    "sanitized_output" not in group
                    for group in loaded["evidence_groups"]
                )
            )
            self.assertIn("| 30 | Unified product audit log is complete", markdown)
            self.assertIn("production_ready=false", markdown)
            if json_path.stat().st_mode & 0o777:
                self.assertEqual(json_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(markdown_path.stat().st_mode & 0o777, 0o600)

    def test_every_emitted_status_uses_the_closed_vocabulary(self) -> None:
        report = build_report(SCENARIOS, _results(), generated_at=NOW)
        emitted = {item["status"] for item in report["scenarios"]}
        emitted.update(item["local_evidence_status"] for item in report["scenarios"])
        emitted.update(item["status"] for item in report["evidence_groups"])
        emitted.update(item["status"] for item in report["external_gates"])
        self.assertLessEqual(emitted, ALLOWED_STATUSES)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run the deterministic local product-release E2E evidence catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.product_release_e2e_support import (  # noqa: E402
    EvidenceRunner,
    build_report,
    evidence_groups_for,
    not_run_results,
    select_scenarios,
    validate_catalog,
    write_reports,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run fixed local pytest evidence for the 30-step JARVIS product "
            "release scenario and write JSON/Markdown reports."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "build" / "e2e-product-release",
        help="Ignored local report directory (default: build/e2e-product-release).",
    )
    parser.add_argument(
        "--scenario",
        type=int,
        action="append",
        dest="scenario_ids",
        help="Run one scenario ID; repeat to select more than one (default: all 30).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Per-evidence-group pytest timeout (default: 900).",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Write a not_run report without executing pytest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    catalog_errors = validate_catalog(PROJECT_ROOT)
    if catalog_errors:
        for error in catalog_errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    try:
        scenarios = select_scenarios(args.scenario_ids)
        groups = evidence_groups_for(scenarios)
        if args.plan_only:
            evidence_results = not_run_results(groups)
        else:
            runner = EvidenceRunner(
                PROJECT_ROOT,
                timeout_seconds=args.timeout_seconds,
            )
            evidence_results = runner.run_groups(groups)
        report = build_report(scenarios, evidence_results)
        json_path, markdown_path = write_reports(report, args.output_dir)
    except (OSError, ValueError) as exc:
        print(
            f"E2E harness failed safely ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2

    summary = report["summary"]
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "summary": summary,
                "production_ready": False,
                "production_verified": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if args.plan_only:
        return 0
    return 1 if any(result.status == "fail" for result in evidence_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

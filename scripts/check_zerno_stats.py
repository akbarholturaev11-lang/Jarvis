#!/usr/bin/env python3
"""Safely check the configured Zerno statistics connection."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from actions.zerno_stats import (  # noqa: E402
    ZERNO_TOKEN_ENV,
    collect_zerno_source,
    redact_sensitive_text,
)


def _safe_output(value: Any, token: str) -> str:
    text = str(value or "")
    if token:
        text = text.replace(token, "[REDACTED]")
    text = re.sub(r"\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return redact_sensitive_text(text, token)


def main(
    argv: list[str] | None = None,
    *,
    project_root: str | Path | None = None,
    collector: Callable[..., dict[str, Any]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Zerno statistics adapterini tekshirish")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="bounded va redacted normalized JSONni ko‘rsatish",
    )
    args = parser.parse_args(argv)

    token = os.environ.get(ZERNO_TOKEN_ENV, "")
    collect = collector or collect_zerno_source
    report = collect(
        project_root=project_root or ROOT_DIR,
        include_debug=args.debug,
    )

    status = _safe_output(report.get("status") or "failed", token)
    configured = "true" if report.get("configured") else "false"
    summary = report.get("reason") or report.get("overall_status") or "Natija mavjud emas."
    groups = [str(group) for group in report.get("metric_groups") or []]

    print(f"configured: {configured}")
    print(f"status: {status}")
    print(f"summary: {_safe_output(summary, token)}")
    print(f"detected metric groups: {_safe_output(', '.join(groups) or '(none)', token)}")
    print(f"metric count: {int(report.get('metric_count') or 0)}")

    if args.debug:
        debug_value = report.get("debug_payload", report.get("metrics") or {})
        serialized = json.dumps(debug_value, ensure_ascii=False, sort_keys=True)
        print(f"debug (redacted, bounded): {_safe_output(serialized, token)}")

    return 0 if status == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Evidence-based Personal Operations Briefing sources and formatting.

Local project evidence stays allowlisted. Standalone social adapters remain
offline placeholders, while the Zerno adapter uses only its dedicated ignored
config and environment token and never substitutes guessed statistics.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.briefing_routing import DEFAULT_PERSONAL_SOURCES
from actions.zerno_stats import ZERNO_METRIC_GROUPS, collect_zerno_source


_ALLOWED_LOCAL_DOCUMENTS = (
    "PROJECT_MEMORY.md",
    "PROJECT_MAP.md",
    "NEXT_STEPS.md",
    "CHANGELOG_AKBAR.md",
    "AI_RULES.md",
)
_EXTERNAL_SOURCES = {"telegram", "instagram", "messenger", "zerno"}
_MAX_DOCUMENT_CHARS = 120_000
_MAX_HIGHLIGHTS_PER_DOCUMENT = 4

# Named external statistics that fall back to the connected Zerno hub when their
# own standalone adapter is ``not_configured``. Each maps only to the Zerno metric
# groups that legitimately belong to that platform, so unrelated Zerno data (for
# example a generic ``posts`` group) never becomes fabricated Instagram/Telegram
# statistics.
_ZERNO_FALLBACK_GROUPS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram",),
    "telegram": ("telegram_channel", "telegram_bot"),
    "messenger": ("messenger",),
    "channels": ("telegram_channel",),
    "bots": ("telegram_bot", "bot_usage"),
    "posts": ("posts", "content"),
}

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|bot[_ -]?token|password|secret|"
        r"credential|database_url|bearer)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\s)(?:/Users/|/home/|~/|[A-Za-z]:[\\/])"),
    re.compile(r"\b(?:config/api_keys\.json|memory/long_term\.json)\b", re.IGNORECASE),
    re.compile(r"(?:https?|file)://", re.IGNORECASE),
)


def _project_root(project_root: str | Path | None = None) -> Path:
    return Path(project_root or Path(__file__).resolve().parents[1]).expanduser().resolve()


def _clean_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str("" if value is None else value)).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _safe_document_line(line: str) -> str:
    text = _clean_text(line)
    if not text or text.startswith("```"):
        return ""
    if any(pattern.search(text) for pattern in _SENSITIVE_TEXT_PATTERNS):
        return ""
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", text)
    return _clean_text(text)


def _read_allowlisted_document(root: Path, filename: str) -> dict[str, Any] | None:
    if filename not in _ALLOWED_LOCAL_DOCUMENTS:
        return None

    candidate = root / filename
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None

    # A same-name symlink must not turn the allowlist into an arbitrary-file read.
    if resolved.parent != root or not resolved.is_file():
        return None

    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(_MAX_DOCUMENT_CHARS + 1)
    except OSError:
        return None

    truncated = len(content) > _MAX_DOCUMENT_CHARS
    if truncated:
        content = content[:_MAX_DOCUMENT_CHARS]

    highlights: list[str] = []
    first_action = ""
    for raw_line in content.splitlines():
        safe_line = _safe_document_line(raw_line)
        if (
            filename == "NEXT_STEPS.md"
            and not first_action
            and re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", raw_line)
        ):
            first_action = safe_line
        if safe_line and safe_line not in highlights:
            highlights.append(safe_line)
        if len(highlights) >= _MAX_HIGHLIGHTS_PER_DOCUMENT:
            break

    return {
        "name": filename,
        "status": "available",
        "highlights": highlights,
        "first_action": first_action,
        "truncated": truncated,
    }


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _collect_git_metadata(root: Path) -> dict[str, Any]:
    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "status": "unavailable",
            "reason": "Git metadata is unavailable for this project root.",
        }

    branch_result = _run_git(root, "branch", "--show-current")
    commit_result = _run_git(root, "rev-parse", "--short", "HEAD")
    status_result = _run_git(root, "status", "--porcelain", "--untracked-files=normal")

    branch = _clean_text(branch_result.stdout, 100) if branch_result and branch_result.returncode == 0 else ""
    commit = _clean_text(commit_result.stdout, 40) if commit_result and commit_result.returncode == 0 else ""
    status_lines = []
    if status_result and status_result.returncode == 0:
        status_lines = [line for line in status_result.stdout.splitlines() if line.strip()]

    staged = sum(1 for line in status_lines if len(line) >= 2 and line[0] not in {" ", "?"})
    unstaged = sum(1 for line in status_lines if len(line) >= 2 and line[1] not in {" ", "?"})
    untracked = sum(1 for line in status_lines if line.startswith("??"))

    # Only counts and safe Git identifiers are returned; file paths are discarded.
    return {
        "status": "available",
        "branch": branch or None,
        "commit": commit or None,
        "clean": not status_lines,
        "changed_entry_count": len(status_lines),
        "staged_entry_count": staged,
        "unstaged_entry_count": unstaged,
        "untracked_entry_count": untracked,
    }


def _extract_next_action(documents: list[dict[str, Any]]) -> str:
    for document in documents:
        if document.get("name") != "NEXT_STEPS.md":
            continue
        first_action = _clean_text(document.get("first_action"))
        if first_action:
            return first_action
        for highlight in document.get("highlights", []):
            lowered = highlight.casefold()
            if lowered in {"next steps", "immediate next steps", "current next steps"}:
                continue
            return _clean_text(highlight)
    return ""


def _collect_local_projects(root: Path, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    del parameters
    documents = [
        document
        for filename in _ALLOWED_LOCAL_DOCUMENTS
        if (document := _read_allowlisted_document(root, filename)) is not None
    ]
    git = _collect_git_metadata(root)
    available = bool(documents) or git.get("status") == "available"
    document_names = [document["name"] for document in documents]

    return {
        "source": "local_projects",
        "status": "available" if available else "unavailable",
        "statistics": {
            "documents_read_count": len(document_names),
            "git_changed_entry_count": git.get("changed_entry_count")
            if git.get("status") == "available"
            else None,
        },
        "documents_read": document_names,
        "document_evidence": documents,
        "git": git,
        "next_action": _extract_next_action(documents),
        "reason": (
            "Allowlisted local project documents and read-only Git metadata were inspected."
            if available
            else "No allowlisted local project evidence is available."
        ),
    }


def _not_configured_external(source: str) -> dict[str, Any]:
    display_name = source.capitalize()
    if source == "zerno":
        reason = (
            "Zerno adapter is a network-disabled skeleton: no API/token/config is configured. "
            "No network request was made and no statistics were invented."
        )
    else:
        reason = (
            f"{display_name} has no configured API/token/config adapter. "
            "No statistics were fetched or invented."
        )
    return {
        "source": source,
        "status": "not_configured",
        "configured": False,
        "statistics": None,
        "network_attempted": False,
        "reason": reason,
    }


def build_source_registry(
    project_root: str | Path | None = None,
) -> dict[str, Callable[[Mapping[str, Any] | None], dict[str, Any]]]:
    """Build the local, placeholder, and configured Zerno source adapters."""

    root = _project_root(project_root)
    return {
        "local_projects": lambda parameters=None: _collect_local_projects(root, parameters),
        "telegram": lambda parameters=None: _not_configured_external("telegram"),
        "instagram": lambda parameters=None: _not_configured_external("instagram"),
        "messenger": lambda parameters=None: _not_configured_external("messenger"),
        "zerno": lambda parameters=None: collect_zerno_source(root),
    }


def _requested_sources(parameters: Mapping[str, Any] | None) -> list[str]:
    raw_sources: Any = (parameters or {}).get("sources") or DEFAULT_PERSONAL_SOURCES
    if isinstance(raw_sources, str):
        raw_sources = [part for part in raw_sources.split(",") if part.strip()]
    if not isinstance(raw_sources, (list, tuple, set)):
        raw_sources = DEFAULT_PERSONAL_SOURCES

    sources: list[str] = []
    for raw_source in raw_sources:
        source = _clean_text(raw_source, 60).casefold().replace(" ", "_")
        if source == "local_project":
            source = "local_projects"
        if source and source not in sources:
            sources.append(source)
    return sources or list(DEFAULT_PERSONAL_SOURCES)


def _collect_source(
    source: str,
    adapter: Any,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if adapter is None:
        return {
            "source": source,
            "status": "not_configured",
            "configured": False,
            "statistics": None,
            "network_attempted": False,
            "reason": "No source adapter is registered; no statistics were fetched or invented.",
        }

    try:
        if isinstance(adapter, Mapping):
            result = dict(adapter)
        elif hasattr(adapter, "collect"):
            result = adapter.collect(parameters)
        else:
            result = adapter(parameters)
    except Exception:
        return {
            "source": source,
            "status": "failed",
            "statistics": None,
            "reason": "The source adapter failed without a verified result.",
        }

    if not isinstance(result, Mapping):
        return {
            "source": source,
            "status": "failed",
            "statistics": None,
            "reason": "The source adapter returned no structured evidence.",
        }

    report = dict(result)
    report.setdefault("source", source)
    report.setdefault("status", "uncertain")
    report.setdefault("statistics", None)
    if source in _EXTERNAL_SOURCES and report.get("status") == "not_configured":
        report["statistics"] = None
        report.setdefault("configured", False)
        report.setdefault("network_attempted", False)
    return report


def _has_metric_content(value: Any, depth: int = 0) -> bool:
    """Return True only if a metric group carries a real scalar value."""

    if depth > 6:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_metric_content(child, depth + 1) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_metric_content(child, depth + 1) for child in value)
    return False


def _zerno_backed_source(source: str, zerno_report: Mapping[str, Any]) -> dict[str, Any]:
    """Back a named external statistics request with the connected Zerno hub.

    Only the Zerno metric groups that genuinely belong to ``source`` are surfaced,
    so unrelated Zerno data never becomes fabricated platform statistics.
    """

    groups = _ZERNO_FALLBACK_GROUPS.get(source, ())
    display = source.replace("_", " ").capitalize()
    report = zerno_report if isinstance(zerno_report, Mapping) else {}
    status = str(report.get("status") or "not_configured")

    if status == "connected":
        metrics = report.get("metrics") or {}
        present: dict[str, Any] = {}
        if isinstance(metrics, Mapping):
            for group in groups:
                value = metrics.get(group)
                if group in metrics and _has_metric_content(value):
                    present[group] = value
        if present:
            summaries = [
                f"{_ZERNO_GROUP_LABELS.get(group, group.replace('_', ' ').title())}: "
                f"{_numeric_metric_summary(value)}"
                for group, value in present.items()
            ]
            summary_text = "; ".join(summaries)
            return {
                "source": source,
                "status": "connected",
                "backing_source": "zerno",
                "configured": False,
                "statistics": present,
                "metric_groups": list(present),
                "foyda": [f"Zerno hub orqali {display} ko'rsatkichlari: {summary_text}."],
                "zarar": [],
                "next_action": "",
                "reason": (
                    f"{display} standalone adapteri not_configured; Zerno hub ulangan va "
                    f"{display} metrikalari topildi ({summary_text})."
                ),
            }
        return {
            "source": source,
            "status": "not_available",
            "backing_source": "zerno",
            "configured": False,
            "statistics": None,
            "reason": (
                f"Zerno hub ulangan, lekin {display} uchun maxsus metrikalar mavjud emas. "
                f"Soxta {display} statistikasi ko'rsatilmadi."
            ),
        }

    if status == "not_configured":
        return {
            "source": source,
            "status": "not_configured",
            "configured": False,
            "statistics": None,
            "network_attempted": bool(report.get("network_attempted", False)),
            "reason": (
                f"{display} standalone adapteri va Zerno hub ham sozlanmagan: haqiqiy "
                "API/token/config yo'q; statistika olinmadi va ixtiro qilinmadi."
            ),
        }

    # Zerno is configured but failed or uncertain: report it honestly, never invent.
    zerno_reason = _clean_text(report.get("reason"), 240)
    return {
        "source": source,
        "status": status,
        "backing_source": "zerno",
        "configured": False,
        "statistics": None,
        "network_attempted": bool(report.get("network_attempted", False)),
        "reason": (
            f"{display} standalone adapteri not_configured; Zerno hub holati: {status}."
            + (f" {zerno_reason}" if zerno_reason else "")
        ),
    }


def _apply_zerno_fallback(
    source_reports: dict[str, dict[str, Any]],
    source_registry: Mapping[str, Any],
    params: Mapping[str, Any],
) -> None:
    """Route named external stats to the Zerno hub when their adapter is offline.

    A standalone adapter that is actually configured (``available``/``connected``/
    ``failed``) wins; only a ``not_configured`` result triggers the Zerno fallback.
    """

    pending = [
        source
        for source in source_reports
        if source in _ZERNO_FALLBACK_GROUPS
        and source_reports[source].get("status") == "not_configured"
    ]
    if not pending:
        return

    # Collect Zerno at most once, reusing an already-requested Zerno report.
    zerno_report = source_reports.get("zerno")
    if not isinstance(zerno_report, Mapping):
        zerno_report = _collect_source("zerno", source_registry.get("zerno"), params)

    for source in pending:
        source_reports[source] = _zerno_backed_source(source, zerno_report)


def collect_personal_briefing(
    parameters: Mapping[str, Any] | None = None,
    project_root: str | Path | None = None,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect an evidence-based briefing without consulting private memory/config."""

    params = dict(parameters or {})
    source_registry = dict(
        build_source_registry(project_root) if registry is None else registry
    )
    source_reports: dict[str, dict[str, Any]] = {}
    for source in _requested_sources(params):
        source_reports[source] = _collect_source(source, source_registry.get(source), params)

    _apply_zerno_fallback(source_reports, source_registry, params)

    foyda: list[str] = []
    zarar: list[str] = []
    top_priorities: list[str] = []
    next_action = ""
    successful_source = False

    local = source_reports.get("local_projects")
    if local and local.get("status") == "available":
        successful_source = True
        documents = list(local.get("documents_read") or [])
        if documents:
            foyda.append(
                "Allowlistdagi loyiha hujjatlari o'qildi: " + ", ".join(documents) + "."
            )
        git = local.get("git") or {}
        if git.get("status") == "available":
            changed_count = int(git.get("changed_entry_count") or 0)
            if changed_count:
                zarar.append(
                    f"Git ishchi daraxtida {changed_count} ta o'zgarish yozuvi bor; "
                    "commitdan oldin ularni tekshirish kerak."
                )
            else:
                foyda.append("Git ishchi daraxti read-only tekshiruvda toza.")
        next_action = _clean_text(local.get("next_action"))
    elif "local_projects" in source_reports:
        zarar.append("Local loyiha dalillari mavjud emas yoki o'qib bo'lmadi.")

    for source, source_report in source_reports.items():
        if source == "local_projects":
            continue
        status = source_report.get("status", "uncertain")
        if status in {"available", "connected"}:
            successful_source = True
            for item in source_report.get("foyda") or []:
                evidence = _clean_text(item)
                if evidence:
                    foyda.append(f"{source}: {evidence}")
            for item in source_report.get("zarar") or []:
                evidence = _clean_text(item)
                if evidence:
                    zarar.append(f"{source}: {evidence}")
            source_next_action = _clean_text(source_report.get("next_action"))
            if source_next_action and (not next_action or params.get("scope") == "statistics"):
                next_action = source_next_action
            for item in source_report.get("top_priorities") or []:
                priority = _clean_text(item)
                if priority and priority not in top_priorities:
                    top_priorities.append(priority)
        elif status == "not_configured":
            reason = _clean_text(source_report.get("reason"), 320)
            if reason:
                zarar.append(reason)
            else:
                zarar.append(
                    f"{source}: not_configured — real API/token/config yo'q; "
                    "statistika olinmadi va ixtiro qilinmadi."
                )
            source_next_action = _clean_text(source_report.get("next_action"))
            if source_next_action and not next_action:
                next_action = source_next_action
        else:
            reason = _clean_text(source_report.get("reason"), 320)
            zarar.append(
                reason or f"{source}: {status} — tasdiqlangan statistika mavjud emas."
            )
            source_next_action = _clean_text(source_report.get("next_action"))
            if source_next_action and not next_action:
                next_action = source_next_action

    if not foyda:
        foyda.append("Tasdiqlangan operatsion foyda aniqlanmadi; dalillar yetarli emas.")
    if not zarar:
        zarar.append("Tasdiqlangan zarar yoki cheklov aniqlanmadi.")
    if not next_action:
        missing_external = next(
            (
                source
                for source, source_report in source_reports.items()
                if source_report.get("status") == "not_configured"
            ),
            "",
        )
        if missing_external:
            next_action = (
                f"{missing_external} uchun haqiqiy API/token/config adapterni sozlang; "
                "ungacha statistika bermang."
            )
        else:
            next_action = "NEXT_STEPS.md ga bitta aniq va tekshiriladigan keyingi vazifa qo'shing."

    if next_action and next_action not in top_priorities:
        top_priorities.append(next_action)

    return {
        "briefing_type": "personal_operations",
        "status": "available" if successful_source else "partial",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": _clean_text(params.get("scope") or "operations", 60),
        "sources": source_reports,
        "foyda": foyda,
        "zarar": zarar,
        "next_action": next_action,
        "top_priorities": top_priorities[:3],
    }


_ZERNO_GROUP_LABELS = {
    "telegram_channel": "Telegram channel",
    "telegram_bot": "Telegram bot",
    "instagram": "Instagram",
    "messenger": "Messenger",
    "leads": "Leads",
    "payments": "Payments",
    "subscriptions": "Subscriptions",
    "revenue": "Revenue",
    "users": "Users",
    "active_users": "Active users",
    "errors": "Errors",
    "engagement": "Engagement",
    "growth": "Growth",
    "posts": "Posts",
    "content": "Content",
    "bot_usage": "Bot usage",
}


def _numeric_metric_summary(value: Any, limit: int = 8) -> str:
    if value is None:
        return "ma’lumot bo‘sh"
    if isinstance(value, (Mapping, list)) and not value:
        return "ko‘rsatkich yo‘q"
    leaves: list[str] = []

    def walk(item: Any, path: str = "", depth: int = 0) -> None:
        if depth > 5 or len(leaves) >= limit:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                clean_key = _clean_text(key, 60)
                child_path = f"{path}.{clean_key}" if path else clean_key
                walk(child, child_path, depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item[:20]):
                child_path = f"{path}[{index}]" if path else f"[{index}]"
                walk(child, child_path, depth + 1)
        elif isinstance(item, bool):
            leaves.append(f"{path or 'value'}={str(item).lower()}")
        elif isinstance(item, (int, float)):
            leaves.append(f"{path or 'value'}={item}")

    walk(value)
    return ", ".join(leaves) or "mavjud; raqamli ko‘rsatkich topilmadi"


def _format_zerno_source(source_report: Mapping[str, Any]) -> list[str]:
    status = _clean_text(source_report.get("status") or "uncertain", 60)
    reason = _clean_text(source_report.get("reason") or "", 320)
    suffix = f" — {reason}" if reason else ""
    lines = [f"- zerno: {status}{suffix}"]
    if status != "connected":
        return lines

    overall_status = _clean_text(
        source_report.get("overall_status") or "connected",
        100,
    )
    lines.append(f"  Overall status: {overall_status}")

    latest_updates = list(source_report.get("latest_updates") or [])
    if latest_updates:
        lines.append("  Latest updates:")
        lines.extend(f"    - {_clean_text(item)}" for item in latest_updates[:5])

    metrics = source_report.get("metrics") or {}
    if isinstance(metrics, Mapping):
        for group in ZERNO_METRIC_GROUPS:
            if group not in metrics:
                continue
            label = _ZERNO_GROUP_LABELS.get(group, group.replace("_", " ").title())
            lines.append(f"  {label}: {_numeric_metric_summary(metrics[group])}")
        additional = metrics.get("additional_fields")
        if additional:
            lines.append(f"  Other metrics: {_numeric_metric_summary(additional)}")

    benefits = list(source_report.get("foyda") or [])
    risks = list(source_report.get("zarar") or [])
    if benefits:
        lines.append("  Foyda:")
        lines.extend(f"    - {_clean_text(item)}" for item in benefits[:5])
    if risks:
        lines.append("  Zarar/xavf:")
        lines.extend(f"    - {_clean_text(item)}" for item in risks[:5])

    next_action = _clean_text(source_report.get("next_action"))
    if next_action:
        lines.append(f"  Next action: {next_action}")
    priorities = list(source_report.get("top_priorities") or [])
    if priorities:
        lines.append("  Top 3 priorities:")
        lines.extend(
            f"    {index}. {_clean_text(item)}"
            for index, item in enumerate(priorities[:3], start=1)
        )

    confidence = _clean_text(source_report.get("confidence") or "api_response", 80)
    last_checked_at = _clean_text(source_report.get("last_checked_at"), 80)
    lines.append(f"  Confidence: {confidence}")
    if last_checked_at:
        lines.append(f"  Last checked: {last_checked_at}")
    return lines


def format_personal_briefing(report: Mapping[str, Any]) -> str:
    """Format a concise Uzbek briefing while preserving explicit source status."""

    foyda = list(report.get("foyda") or ["Tasdiqlangan foyda ma'lumoti yo'q."])
    zarar = list(report.get("zarar") or ["Tasdiqlangan zarar ma'lumoti yo'q."])
    next_action = _clean_text(report.get("next_action") or "Keyingi harakat aniqlanmagan.")
    sources = report.get("sources") or {}

    status = _clean_text(report.get("status") or "partial", 40)
    lines = [
        "[PERSONAL_OPERATIONS_BRIEFING]",
        f"status={status}",
        "Personal Operations Briefing",
        "",
        "Foyda:",
    ]
    lines.extend(f"- {_clean_text(item)}" for item in foyda)
    lines.extend(("", "Zarar:"))
    lines.extend(f"- {_clean_text(item)}" for item in zarar)
    lines.extend(("", f"Next action: {next_action}"))
    priorities = list(report.get("top_priorities") or [])
    if priorities:
        lines.extend(("", "Top 3 priorities:"))
        lines.extend(
            f"{index}. {_clean_text(item)}"
            for index, item in enumerate(priorities[:3], start=1)
        )
    lines.extend(("", "Manbalar:"))

    for source, source_report in sources.items():
        if source == "zerno":
            lines.extend(_format_zerno_source(source_report))
            continue
        status = _clean_text(source_report.get("status") or "uncertain", 60)
        reason = _clean_text(source_report.get("reason") or "", 280)
        suffix = f" — {reason}" if reason else ""
        lines.append(f"- {source}: {status}{suffix}")

    return "\n".join(lines).strip()


def personal_briefing(
    parameters: Mapping[str, Any] | None,
    response=None,
    player=None,
    session_memory=None,
    project_root: str | Path | None = None,
    registry: Mapping[str, Any] | None = None,
) -> str:
    """Action entry point used by the existing tool dispatcher."""

    del response, session_memory
    report = collect_personal_briefing(parameters, project_root, registry)
    output = format_personal_briefing(report)
    return output

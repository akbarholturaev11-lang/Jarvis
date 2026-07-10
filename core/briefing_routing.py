"""Narrow routing policy for personal briefings and explicit world news."""

from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_PERSONAL_SOURCES = (
    "local_projects",
    "telegram",
    "instagram",
    "messenger",
    "zerno",
)

_PERSONAL_PATTERNS = (
    r"\bmen\s+uydaman\b",
    r"\buydaman\b",
    r"\bishga\s+qaytdim\b",
    r"\bloyihalarimni\s+tekshir\b",
    r"\bstatistikani\s+ayt\b",
    r"\bpersonal\s+briefing\b",
)

_WORLD_NEWS_PATTERNS = (
    r"\bdunyo\s+yangiliklari(?:ni)?\b",
    r"\bworld\s+news\b",
    r"\blatest\s+news\b",
    r"\bмировые\s+новости\b",
    r"\bновости\s+мира\b",
    r"\bпоследние\s+новости\b",
    r"\bсвежие\s+новости\b",
)

_SOURCE_ALIASES = {
    "telegram": ("telegram",),
    "instagram": ("instagram", "insta"),
    "messenger": ("messenger", "facebook messenger"),
    "zerno": ("zerno",),
}

_STATISTICS_PATTERN = re.compile(
    r"\b(?:statistika\w*|statistic\w*|stats\w*|analytic\w*|metrik\w*|"
    r"статистик\w*|аналитик\w*|показател\w*)\b",
    re.IGNORECASE,
)

_EXPLICIT_NEWS_TERM_PATTERN = re.compile(
    r"\b(?:news\w*|headlines?\w*|yangilik\w*|новост\w*)\b",
    re.IGNORECASE,
)

_GENERIC_WORLD_NEWS_QUERY_PATTERN = re.compile(
    r"\b(?:top\s+world\s+news|world\s+news|latest\s+news|top\s+headlines|"
    r"global\s+headlines|dunyo\s+yangilik\w*)\b",
    re.IGNORECASE,
)

_COMPOUND_COMMAND_PATTERN = re.compile(r"\b(?:va|and|и)\b", re.IGNORECASE)

_BRIEFING_ANSWER_TOOLS = {
    "personal_briefing",
    "web_search",
}


def _normalize(text: Any) -> str:
    value = str(text or "").casefold()
    value = value.translate(str.maketrans({"‘": "'", "’": "'", "`": "'"}))
    value = re.sub(r"[^\w'\s]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _requested_statistics_source(text: str) -> str:
    if not _STATISTICS_PATTERN.search(text):
        return ""
    for source, aliases in _SOURCE_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases):
            return source
    return ""


def _web_query_matches_personal_route(query: str, route: dict[str, Any]) -> bool:
    if _GENERIC_WORLD_NEWS_QUERY_PATTERN.search(query):
        return True
    if _matches_any(query, _PERSONAL_PATTERNS):
        return True
    sources = route.get("arguments", {}).get("sources") or []
    external_sources = [source for source in sources if source in _SOURCE_ALIASES]
    return bool(
        external_sources
        and _STATISTICS_PATTERN.search(query)
        and any(source in query for source in external_sources)
    )


def resolve_briefing_route(user_text: str) -> dict[str, Any]:
    """Resolve only Personal Operations Briefing and explicit world-news intents."""

    normalized = _normalize(user_text)
    if not normalized:
        return {}

    source = _requested_statistics_source(normalized)
    if source:
        return {
            "tool_name": "personal_briefing",
            "arguments": {"sources": [source], "scope": "statistics"},
            "reason": f"Explicit {source} statistics request.",
        }

    if _matches_any(normalized, _WORLD_NEWS_PATTERNS):
        return {
            "tool_name": "web_search",
            "arguments": {"mode": "news", "query": "top world news today"},
            "reason": "Explicit world-news request.",
        }

    if _matches_any(normalized, _PERSONAL_PATTERNS):
        return {
            "tool_name": "personal_briefing",
            "arguments": {"sources": list(DEFAULT_PERSONAL_SOURCES)},
            "reason": "Explicit Personal Operations Briefing request.",
        }

    return {}


def apply_briefing_route(
    user_text: str,
    selected_tool: str,
    selected_args: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], str]:
    """Apply the narrow briefing route after Gemini has selected a tool."""

    original_args = dict(selected_args or {})
    route = resolve_briefing_route(user_text)
    if not route:
        implicit_generic_news = (
            selected_tool == "web_search"
            and str(original_args.get("mode") or "").casefold() == "news"
            and not _EXPLICIT_NEWS_TERM_PATTERN.search(_normalize(user_text))
        )
        if implicit_generic_news:
            original_args["mode"] = "search"
            original_args["query"] = str(user_text or original_args.get("query") or "").strip()
            return (
                selected_tool,
                original_args,
                "briefing route guard: implicit generic world news changed to normal search",
            )
        return selected_tool, original_args, ""

    routed_tool = str(route["tool_name"])
    routed_args = dict(route["arguments"])
    if selected_tool not in _BRIEFING_ANSWER_TOOLS:
        # Preserve independent tools in multi-command turns. Gemini can issue
        # personal_briefing/web_search and open_app as separate calls.
        return selected_tool, original_args, ""
    normalized_user = _normalize(user_text)
    compound_command = bool(_COMPOUND_COMMAND_PATTERN.search(normalized_user))
    selected_query = _normalize(original_args.get("query"))

    if routed_tool == "personal_briefing" and selected_tool == "web_search":
        if compound_command and not _web_query_matches_personal_route(selected_query, route):
            return selected_tool, original_args, ""

    if routed_tool == "web_search" and selected_tool == "web_search":
        if compound_command and not _GENERIC_WORLD_NEWS_QUERY_PATTERN.search(selected_query):
            return selected_tool, original_args, ""

    if routed_tool == "web_search" and selected_tool == "personal_briefing":
        if compound_command and _matches_any(normalized_user, _PERSONAL_PATTERNS):
            return selected_tool, original_args, ""

    if selected_tool == routed_tool and original_args == routed_args:
        note = f"briefing route confirmed: {route['reason']}"
    else:
        prior = selected_tool or "no tool"
        note = f"briefing route override: {prior} -> {routed_tool}; {route['reason']}"
    return routed_tool, routed_args, note


def build_briefing_route_hint(user_text: str, payload: str) -> str:
    """Prepend a Gemini-internal route hint while preserving the original payload."""

    route = resolve_briefing_route(user_text)
    if not route:
        return payload

    route_json = json.dumps(route, ensure_ascii=False, sort_keys=True)
    return (
        "[BRIEFING_ROUTE - internal, do not read aloud]\n"
        f"Required tool route: {route_json}\n"
        "Use this exact route for the briefing/news portion; do not substitute personal briefing "
        "and world news. Preserve independent commands as separate tool calls.\n"
        "[ORIGINAL_USER_PAYLOAD]\n"
        f"{payload}"
    )

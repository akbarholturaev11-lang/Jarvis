"""Configured Zerno statistics adapter for Personal Operations Briefing.

The adapter reads only ``config/briefing_sources.json`` and the
``ZERNO_API_TOKEN`` environment variable. It uses a bounded stdlib HTTP GET,
normalizes variable JSON shapes, and never substitutes guessed statistics.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ZERNO_TOKEN_ENV = "ZERNO_API_TOKEN"
ZERNO_CONFIG_RELATIVE_PATH = Path("config/briefing_sources.json")
ZERNO_METRIC_GROUPS = (
    "telegram_channel",
    "telegram_bot",
    "instagram",
    "messenger",
    "leads",
    "payments",
    "subscriptions",
    "revenue",
    "users",
    "active_users",
    "errors",
    "engagement",
    "growth",
    "posts",
    "content",
    "bot_usage",
)

_URL_PLACEHOLDERS = {
    "paste_zerno_api_url_here",
    "https://paste_zerno_api_url_here",
    "http://paste_zerno_api_url_here",
}
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:token|secret|password|passwd|credential|authorization|cookie|"
    r"api_key|access_key|private_key)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_KEY_TERMS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "apikey",
    "accesskey",
    "privatekey",
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_INLINE_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|password|"
    r"secret|authorization|auth|jwt|signature)\s*[:=]\s*[^\s,;]+"
)
_JSON_SECRET_PATTERN = re.compile(
    r'(?i)"(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|password|'
    r'secret|authorization|auth|jwt|signature)"\s*:\s*(?:"[^"]*"|[^,}\]]+)',
)
_CONTROL_FIELDS = {
    "status",
    "overall_status",
    "state",
    "health",
    "latest_updates",
    "recent_updates",
    "updates",
    "foyda",
    "benefits",
    "wins",
    "positive",
    "zarar",
    "risks",
    "losses",
    "warnings",
    "alerts",
    "next_action",
    "next_step",
    "recommendation",
    "recommended_action",
    "top_priorities",
    "priorities",
    "priority",
    "confidence",
}
_CHANGE_TERMS = ("change", "delta", "growth", "increase", "decrease", "drop")
_NEGATIVE_TERMS = ("error", "failed", "failure", "loss", "risk", "churn", "zarar")
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_DEPTH = 7
_MAX_MAPPING_ITEMS = 80
_MAX_LIST_ITEMS = 60
_MAX_TEXT_CHARS = 500
_MAX_ADDITIONAL_FIELDS = 120
_DEFAULT_TIMEOUT_SECONDS = 10.0


class _NoRedirectHandler(HTTPRedirectHandler):
    """Do not forward the Zerno bearer token through HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def urlopen(request: Request, timeout: float):
    """Compatibility seam for tests, backed by a redirect-blocking opener."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _project_root(project_root: str | Path | None = None) -> Path:
    return Path(project_root or Path(__file__).resolve().parents[1]).expanduser().resolve()


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str("" if value is None else value)).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _normalize_key(value: Any) -> str:
    raw = str(value or "")
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    key = re.sub(r"[^\w]+", "_", raw.casefold(), flags=re.UNICODE)
    return key.strip("_")[:120]


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalize_key(value)
    compact = normalized.replace("_", "")
    segments = set(normalized.split("_"))
    return bool(_SENSITIVE_KEY_PATTERN.search(normalized)) or any(
        term in compact for term in _SENSITIVE_KEY_TERMS
    ) or bool(segments & {"auth", "authentication", "oauth", "jwt", "signature", "sig"})


def redact_sensitive_text(value: Any, token: str = "") -> str:
    """Return bounded text with exact and generic credential patterns removed."""

    text = _clean_text(value, _MAX_TEXT_CHARS)
    if token:
        text = text.replace(token, "[REDACTED]")
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _JSON_SECRET_PATTERN.sub('"[REDACTED]": "[REDACTED]"', text)
    text = _INLINE_SECRET_PATTERN.sub("[REDACTED]", text)
    return text


def _safe_json(value: Any, token: str, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_sensitive_text(value, token)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= _MAX_MAPPING_ITEMS or _is_sensitive_key(raw_key):
                continue
            key = redact_sensitive_text(raw_key, token)[:120]
            if not key:
                continue
            result[key] = _safe_json(raw_value, token, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_json(item, token, depth + 1)
            for item in list(value)[:_MAX_LIST_ITEMS]
        ]
    return redact_sensitive_text(value, token)


def _base_report(
    *,
    status: str,
    source_name: str = "Zerno Operations Hub",
    configured: bool = False,
    network_attempted: bool = False,
    reason: str,
) -> dict[str, Any]:
    return {
        "source": "zerno",
        "source_name": source_name,
        "source_type": "zerno",
        "status": status,
        "configured": configured,
        "network_attempted": network_attempted,
        "latest_updates": [],
        "metrics": {},
        "statistics": None,
        "metric_groups": [],
        "metric_count": 0,
        "foyda": [],
        "zarar": [],
        "next_action": "",
        "top_priorities": [],
        "confidence": "none",
        "last_checked_at": _checked_at(),
        "reason": reason,
    }


def _not_configured_report(
    reason: str,
    source_name: str = "Zerno Operations Hub",
    next_action: str = "bash scripts/setup_zerno_stats.sh",
) -> dict[str, Any]:
    report = _base_report(
        status="not_configured",
        source_name=source_name,
        configured=False,
        network_attempted=False,
        reason=reason,
    )
    report["next_action"] = next_action
    report["top_priorities"] = [next_action] if next_action else []
    return report


def _failed_report(
    short_reason: Any,
    *,
    source_name: str = "Zerno Operations Hub",
    configured: bool = True,
    network_attempted: bool = False,
    token: str = "",
) -> dict[str, Any]:
    detail = redact_sensitive_text(short_reason, token) or "noma'lum xato"
    report = _base_report(
        status="failed",
        source_name=source_name,
        configured=configured,
        network_attempted=network_attempted,
        reason=f"Zerno API’dan ma’lumot olib bo‘lmadi: {detail}.",
    )
    report["next_action"] = "python scripts/check_zerno_stats.py orqali ulanishni tekshiring."
    report["top_priorities"] = [report["next_action"]]
    return report


def _load_zerno_config(root: Path) -> tuple[dict[str, Any] | None, str]:
    config_path = root / ZERNO_CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        return None, "missing"

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"invalid:{exc.__class__.__name__}"

    if not isinstance(document, Mapping):
        return None, "invalid:root_must_be_object"
    sources = document.get("sources")
    if not isinstance(sources, list):
        return None, "invalid:sources_must_be_list"

    zerno_sources = [
        dict(source)
        for source in sources
        if isinstance(source, Mapping)
        and _normalize_key(source.get("type")) == "zerno"
    ]
    if not zerno_sources:
        return None, "missing"

    for source in zerno_sources:
        if source.get("enabled", True) is not False:
            return source, "ok"
    return None, "disabled"


def _validate_api_url(value: Any) -> tuple[str, str]:
    url = str(value or "").strip()
    if not url or _normalize_key(url) in _URL_PLACEHOLDERS or "paste_zerno_api_url_here" in url.casefold():
        return "", "missing"

    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").casefold()
    except ValueError:
        return "", "API URL formati noto‘g‘ri"
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return "", "API URL to‘liq http(s) manzil bo‘lishi kerak"
    if parts.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        return "", "internet API uchun HTTPS URL kerak"
    if parts.username or parts.password:
        return "", "API URL ichida credential saqlamang"
    if any(_is_sensitive_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return "", "API tokenni URL query ichiga qo‘ymang"
    return url, "ok"


def _merge_group(groups: dict[str, list[Any]], group: str, value: Any) -> None:
    occurrences = groups.setdefault(group, [])
    if value not in occurrences:
        occurrences.append(value)


def _collect_known_groups(
    value: Any,
    groups: dict[str, list[Any]],
    depth: int = 0,
) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalize_key(key)
            if normalized in ZERNO_METRIC_GROUPS:
                _merge_group(groups, normalized, child)
                continue
            _collect_known_groups(child, groups, depth + 1)
    elif isinstance(value, list):
        for child in value[:_MAX_LIST_ITEMS]:
            _collect_known_groups(child, groups, depth + 1)


def _collect_additional_fields(value: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    def walk(child: Any, path: list[str], excluded: bool, depth: int) -> None:
        if excluded or depth > _MAX_DEPTH or len(fields) >= _MAX_ADDITIONAL_FIELDS:
            return
        if isinstance(child, Mapping):
            for raw_key, nested in child.items():
                normalized = _normalize_key(raw_key)
                if not normalized or _is_sensitive_key(normalized):
                    continue
                walk(
                    nested,
                    [*path, normalized],
                    normalized in ZERNO_METRIC_GROUPS or normalized in _CONTROL_FIELDS,
                    depth + 1,
                )
            return
        if isinstance(child, list):
            for index, nested in enumerate(child[:_MAX_LIST_ITEMS]):
                walk(nested, [*path, str(index)], False, depth + 1)
            return
        if not path:
            return
        fields[".".join(path)] = child

    walk(value, ["items"] if isinstance(value, list) else [], False, 0)
    return fields


def _flatten_scalars(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{_normalize_key(key)}" if prefix else _normalize_key(key)
            leaves.extend(_flatten_scalars(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:_MAX_LIST_ITEMS]):
            path = f"{prefix}.{index}" if prefix else str(index)
            leaves.extend(_flatten_scalars(child, path))
    else:
        leaves.append((prefix or "value", value))
    return leaves


def _find_first(value: Any, keys: set[str], depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, Mapping):
        normalized_items = [(_normalize_key(key), child) for key, child in value.items()]
        for key, child in normalized_items:
            if key in keys:
                return child
        for _, child in normalized_items:
            found = _find_first(child, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value[:_MAX_LIST_ITEMS]:
            found = _find_first(child, keys, depth + 1)
            if found is not None:
                return found
    return None


def _text_items(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[str] = []
    for item in raw_items:
        if isinstance(item, Mapping):
            candidate = item.get("text") or item.get("message") or item.get("title")
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            candidate = item
        else:
            candidate = None
        text = _clean_text(candidate, 240)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _derive_change_evidence(metrics: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    updates: list[str] = []
    benefits: list[str] = []
    risks: list[str] = []
    for path, value in _flatten_scalars(metrics):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        lower_path = path.casefold()
        has_change = any(term in lower_path for term in _CHANGE_TERMS)
        has_negative = any(term in lower_path for term in _NEGATIVE_TERMS)
        has_decrease = "decrease" in lower_path or "drop" in lower_path
        evidence = f"{path}={value}"
        if has_change and evidence not in updates:
            updates.append(evidence)
        if has_negative:
            if has_decrease and value > 0:
                benefits.append(evidence)
            elif has_decrease and value < 0:
                risks.append(evidence)
            elif value > 0:
                risks.append(evidence)
            elif has_change and value < 0:
                benefits.append(evidence)
        elif has_decrease:
            if value > 0:
                risks.append(evidence)
            elif value < 0:
                benefits.append(evidence)
        elif has_change and value > 0:
            benefits.append(evidence)
        elif has_change and value < 0:
            risks.append(evidence)
    return updates[:8], benefits[:5], risks[:5]


def _scalar_count(value: Any) -> int:
    return sum(1 for _, leaf in _flatten_scalars(value) if leaf is not None)


_ADDITIVE_ANALYTICS = (
    "impressions",
    "reach",
    "likes",
    "comments",
    "shares",
    "saves",
    "views",
    "clicks",
    "follows",
)


def _norm_get(record: Any, *candidates: str) -> Any:
    """Return the first value whose normalized key matches a candidate."""

    if not isinstance(record, Mapping):
        return None
    wanted = set(candidates)
    for key, value in record.items():
        if _normalize_key(key) in wanted:
            return value
    return None


def _extract_platform_breakdown(payload: Any) -> dict[str, dict[str, Any]]:
    """Group platform-tagged records (accounts/posts) by their ``platform`` field.

    Real Zerno responses do not use top-level ``instagram``/``telegram`` keys; they
    return ``accounts`` and ``posts`` lists where each record carries a ``platform``
    field. This folds those records into a per-platform breakdown so a named request
    (for example Instagram) can be answered with the real, API-returned numbers only.
    """

    breakdown: dict[str, dict[str, Any]] = {}

    def entry(platform: str) -> dict[str, Any]:
        return breakdown.setdefault(
            platform,
            {"accounts": [], "post_count": 0, "analytics_totals": {}},
        )

    def add_analytics(totals: dict[str, Any], analytics: Any) -> None:
        if not isinstance(analytics, Mapping):
            return
        for key, value in analytics.items():
            normalized = _normalize_key(key)
            if (
                normalized in _ADDITIVE_ANALYTICS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                totals[normalized] = totals.get(normalized, 0) + value

    def classify(record: Mapping[str, Any], platform: str) -> None:
        current = entry(platform)
        analytics = _norm_get(record, "analytics")
        is_post = (
            analytics is not None
            or _norm_get(record, "content") is not None
            or _norm_get(record, "published_at") is not None
        )
        if is_post:
            current["post_count"] += 1
            add_analytics(current["analytics_totals"], analytics)
            return
        followers = _norm_get(record, "followers_count")
        follower_count = (
            int(followers)
            if isinstance(followers, (int, float)) and not isinstance(followers, bool)
            else None
        )
        name = _clean_text(
            _norm_get(record, "username") or _norm_get(record, "display_name"), 80
        )
        if len(current["accounts"]) < 20:
            current["accounts"].append({"name": name or platform, "followers": follower_count})

    def walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_LIST_ITEMS]:
                platform = _norm_get(item, "platform") if isinstance(item, Mapping) else None
                if isinstance(platform, str) and platform.strip():
                    # Classify the tagged record; do not recurse into it so nested
                    # platform arrays cannot inflate account/post counts.
                    classify(item, platform.strip().casefold())
                else:
                    walk(item, depth + 1)

    walk(payload)
    return {
        platform: data
        for platform, data in breakdown.items()
        if data["accounts"] or data["post_count"]
    }


def normalize_zerno_payload(
    payload: Any,
    *,
    source_name: str = "Zerno Operations Hub",
    token: str = "",
    include_debug: bool = False,
) -> dict[str, Any]:
    """Normalize arbitrary JSON without inventing absent metric groups."""

    safe_payload = _safe_json(payload, token)
    groups: dict[str, list[Any]] = {}
    _collect_known_groups(safe_payload, groups)
    ordered_groups = {
        group: groups[group][0] if len(groups[group]) == 1 else groups[group]
        for group in ZERNO_METRIC_GROUPS
        if group in groups
    }
    additional = _collect_additional_fields(safe_payload)
    metrics: dict[str, Any] = dict(ordered_groups)
    if additional:
        metrics["additional_fields"] = additional

    response_updates = _text_items(
        _find_first(safe_payload, {"latest_updates", "recent_updates", "updates"})
    )
    response_benefits = _text_items(
        _find_first(safe_payload, {"foyda", "benefits", "wins", "positive"})
    )
    response_risks = _text_items(
        _find_first(safe_payload, {"zarar", "risks", "losses", "warnings", "alerts"})
    )
    derived_updates, derived_benefits, derived_risks = _derive_change_evidence(metrics)

    latest_updates = list(dict.fromkeys([*response_updates, *derived_updates]))[:8]
    foyda = list(dict.fromkeys([*response_benefits, *derived_benefits]))[:5]
    zarar = list(dict.fromkeys([*response_risks, *derived_risks]))[:5]

    next_action_items = _text_items(
        _find_first(
            safe_payload,
            {"next_action", "next_step", "recommendation", "recommended_action"},
        ),
        limit=1,
    )
    next_action = (
        next_action_items[0]
        if next_action_items
        else "Zerno javobida aniq next action ko‘rsatilmagan; metrikalarni tekshiring."
    )
    priorities = _text_items(
        _find_first(safe_payload, {"top_priorities", "priorities", "priority"}),
        limit=3,
    )
    for item in [next_action, *zarar]:
        if item and item not in priorities:
            priorities.append(item)
        if len(priorities) >= 3:
            break

    overall_status = "connected"
    candidate = _find_first(safe_payload, {"overall_status"})
    if candidate is None and isinstance(safe_payload, Mapping):
        top_level = {_normalize_key(key): child for key, child in safe_payload.items()}
        candidate = next(
            (top_level[key] for key in ("status", "state", "health") if key in top_level),
            None,
        )
    if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
        overall_status = _clean_text(candidate, 100) or "connected"

    confidence_value = _find_first(safe_payload, {"confidence"})
    if isinstance(confidence_value, (str, int, float)) and not isinstance(confidence_value, bool):
        confidence = _clean_text(confidence_value, 80) or "api_response"
    else:
        confidence = "api_response"

    report = _base_report(
        status="connected",
        source_name=source_name,
        configured=True,
        network_attempted=True,
        reason="Zerno API JSON javobi olindi va xavfsiz normallashtirildi.",
    )
    report.update(
        {
            "overall_status": overall_status,
            "latest_updates": latest_updates,
            "metrics": metrics,
            "statistics": metrics,
            "metric_groups": list(ordered_groups),
            "metric_count": _scalar_count(metrics),
            "foyda": foyda,
            "zarar": zarar,
            "next_action": next_action,
            "top_priorities": priorities[:3],
            "confidence": confidence,
        }
    )
    breakdown = _extract_platform_breakdown(safe_payload)
    if breakdown:
        report["platform_breakdown"] = breakdown
    if include_debug:
        report["debug_payload"] = safe_payload
    return report


def _http_error_reason(exc: Exception, token: str) -> str:
    if isinstance(exc, HTTPError):
        reason = redact_sensitive_text(getattr(exc, "reason", ""), token)
        return f"HTTP {exc.code}" + (f" {reason}" if reason else "")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "so‘rov vaqti tugadi"
    if isinstance(exc, URLError):
        underlying = getattr(exc, "reason", "")
        if isinstance(underlying, (TimeoutError, socket.timeout)):
            return "so‘rov vaqti tugadi"
        reason = redact_sensitive_text(underlying, token)
        return f"ulanish xatosi: {reason or 'noma’lum sabab'}"
    return redact_sensitive_text(exc.__class__.__name__, token)


def collect_zerno_source(
    project_root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    include_debug: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Load config, call the configured Zerno JSON endpoint, and normalize it."""

    root = _project_root(project_root)
    config, config_status = _load_zerno_config(root)
    if config_status in {"missing", "disabled"}:
        return _not_configured_report(
            "Zerno statistikasi hali ulanmagan. API URL va ZERNO_API_TOKEN kerak."
        )
    if config_status != "ok" or config is None:
        return _failed_report(
            "briefing_sources.json formati noto‘g‘ri",
            configured=False,
        )

    source_name = _clean_text(config.get("name"), 120) or "Zerno Operations Hub"
    api_url, url_status = _validate_api_url(config.get("api_base_url"))
    if url_status == "missing":
        return _not_configured_report(
            "Zerno statistikasi hali ulanmagan. API URL va ZERNO_API_TOKEN kerak.",
            source_name,
        )
    if url_status != "ok":
        return _failed_report(
            url_status,
            source_name=source_name,
            configured=False,
        )

    token_env = str(config.get("token_env") or ZERNO_TOKEN_ENV).strip()
    if token_env != ZERNO_TOKEN_ENV:
        return _failed_report(
            "token_env faqat ZERNO_API_TOKEN bo‘lishi mumkin",
            source_name=source_name,
            configured=False,
        )

    env = os.environ if environ is None else environ
    token = str(env.get(ZERNO_TOKEN_ENV) or "").strip()
    if not token:
        return _not_configured_report(
            "Zerno token topilmadi. config/local_env.zsh ni source qiling yoki "
            "ZERNO_API_TOKEN ni export qiling.",
            source_name,
            "source config/local_env.zsh",
        )
    source_name = redact_sensitive_text(source_name, token) or "Zerno Operations Hub"

    request = Request(
        api_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Jarvis-AkbarCustom/ZernoStats",
        },
        method="GET",
    )
    open_request = opener or urlopen
    response = None
    try:
        response = open_request(request, timeout=max(1.0, min(float(timeout), 30.0)))
        status_code = getattr(response, "status", None)
        if status_code is None and hasattr(response, "getcode"):
            status_code = response.getcode()
        if isinstance(status_code, int) and status_code >= 400:
            return _failed_report(
                f"HTTP {status_code}",
                source_name=source_name,
                network_attempted=True,
                token=token,
            )
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return _failed_report(
                "JSON javobi ruxsat etilgan hajmdan katta",
                source_name=source_name,
                network_attempted=True,
                token=token,
            )
        payload = json.loads(raw.decode("utf-8-sig"))
    except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
        return _failed_report(
            _http_error_reason(exc, token),
            source_name=source_name,
            network_attempted=True,
            token=token,
        )
    except (UnicodeError, json.JSONDecodeError):
        return _failed_report(
            "javob valid JSON emas",
            source_name=source_name,
            network_attempted=True,
            token=token,
        )
    except Exception as exc:  # Defensive adapter boundary; never expose tracebacks/secrets.
        return _failed_report(
            _http_error_reason(exc, token),
            source_name=source_name,
            network_attempted=True,
            token=token,
        )
    finally:
        if response is not None and hasattr(response, "close"):
            try:
                response.close()
            except Exception:
                pass

    return normalize_zerno_payload(
        payload,
        source_name=source_name,
        token=token,
        include_debug=include_debug,
    )

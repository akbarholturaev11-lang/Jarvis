"""Structured JSON logging, secret redaction, correlation IDs, and metrics.

This module is deliberately dependency-free (standard library only) so it works
unchanged on macOS, Windows, and Linux hosts.  It provides the shared building
blocks used by the operational middleware:

* ``redact_text`` / ``redact_mapping`` scrub secret-looking values before they
  ever reach a log line.
* ``JsonLogFormatter`` / ``configure_json_logging`` emit one structured JSON
  object per log record on a single stream handler.
* ``sanitize_request_id`` / ``new_request_id`` derive a safe correlation ID for
  every request.
* ``InMemoryMetrics`` / ``NullMetrics`` provide a tiny, bounded counter registry
  with a Prometheus text exposition, behind the ``MetricsRegistry`` protocol.

Nothing here has import-time side effects.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final, Protocol, runtime_checkable

_REDACTED: Final = "***"
_MAX_LOG_VALUE_CHARS: Final = 512
_MAX_REDACT_DEPTH: Final = 6

# Field-name hints that mark a value as secret regardless of its contents.
_SECRET_KEY_HINTS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth",
    "cookie",
    "session",
    "csrf",
    "pepper",
    "private_key",
    "privatekey",
    "api_key",
    "apikey",
    "salt",
    "digest",
    "hash",
    "bearer",
    "mfa",
    "otp",
    "totp",
    "recovery",
    "signature",
    "credential",
)

# Inline ``name: value`` / ``name=value`` secrets embedded in free text.
_INLINE_SECRET_RE: Final = re.compile(
    r"(?i)\b("
    r"authorization|bearer|token|password|passwd|secret|cookie|session|csrf|"
    r"pepper|api[-_]?key|private[-_]?key|signature|credential|otp|totp|recovery"
    r")\b\s*[:=]\s*[^\s,;&]+"
)

# Long opaque base64url/base64 runs that look like key material or tokens.
_OPAQUE_TOKEN_RE: Final = re.compile(r"\b[A-Za-z0-9_\-+/]{32,}={0,2}\b")

# A conservative allowlist of correlation-id characters.
_REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9._\-]{8,128}$")

# Structured extras that the access logger is allowed to attach to a record.
_LOG_EXTRA_FIELDS: Final = (
    "request_id",
    "event",
    "method",
    "path",
    "status",
    "client_ip",
    "duration_ms",
    "scheme",
    "reason",
    "host",
)


def _key_is_secret(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def redact_text(value: object) -> str:
    """Return ``value`` as text with inline secrets and opaque tokens masked."""

    text = value if isinstance(value, str) else str(value)
    text = _INLINE_SECRET_RE.sub(
        lambda match: f"{match.group(1)}={_REDACTED}", text
    )
    text = _OPAQUE_TOKEN_RE.sub(_REDACTED, text)
    if len(text) > _MAX_LOG_VALUE_CHARS:
        text = text[:_MAX_LOG_VALUE_CHARS] + "...(truncated)"
    return text


def redact_mapping(value: object, *, _depth: int = 0) -> Any:
    """Recursively copy a structure, masking values under secret-like keys.

    Strings are additionally passed through :func:`redact_text` so an opaque
    token embedded in a non-secret field is still scrubbed.  Depth and value
    length are bounded so a hostile or pathological payload cannot exhaust
    memory while being logged.
    """

    if _depth >= _MAX_REDACT_DEPTH:
        return _REDACTED
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            if _key_is_secret(name):
                result[name] = _REDACTED
            else:
                result[name] = redact_mapping(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(value)


def new_request_id() -> str:
    """Generate a fresh, unguessable correlation identifier."""

    return "req_" + secrets.token_hex(16)


def sanitize_request_id(value: object) -> str | None:
    """Accept a caller-supplied correlation ID only if it is safe to echo."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if _REQUEST_ID_RE.match(candidate) is None:
        return None
    return candidate


def resolve_request_id(value: object) -> str:
    """Return a sanitized inbound correlation ID or a freshly generated one."""

    return sanitize_request_id(value) or new_request_id()


class JsonLogFormatter(logging.Formatter):
    """Render each log record as one redacted JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for field in _LOG_EXTRA_FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                if _key_is_secret(field):
                    payload[field] = _REDACTED
                elif isinstance(value, str):
                    payload[field] = redact_text(value)
                else:
                    payload[field] = value
        if record.exc_info:
            payload["exc_type"] = getattr(
                record.exc_info[0], "__name__", "Exception"
            )
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_json_logging(
    *,
    name: str = "jarvis.backend",
    level: int = logging.INFO,
    stream: Any = None,
) -> logging.Logger:
    """Attach exactly one JSON stream handler to ``name`` (idempotent)."""

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_jarvis_json_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    handler._jarvis_json_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


@runtime_checkable
class MetricsRegistry(Protocol):
    """Minimal counter registry with a text exposition."""

    def increment(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        amount: int = 1,
    ) -> None: ...

    def render_prometheus(self) -> str: ...


class NullMetrics:
    """A metrics sink that records nothing (metrics disabled)."""

    def increment(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        return None

    def render_prometheus(self) -> str:
        return ""


_METRIC_NAME_RE: Final = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_LABEL_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class InMemoryMetrics:
    """Bounded, thread-safe counter registry with Prometheus exposition.

    The series count is capped so untrusted label values cannot grow the
    registry without bound; new series beyond ``max_series`` are dropped rather
    than stored.
    """

    def __init__(self, *, max_series: int = 2048) -> None:
        if not 16 <= max_series <= 65_536:
            raise ValueError("max_series is out of range")
        self._max_series = max_series
        self._counters: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], int]
        self._counters = OrderedDict()
        self._lock = threading.Lock()

    def increment(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        if _METRIC_NAME_RE.match(name) is None or not isinstance(amount, int):
            return
        if amount <= 0:
            return
        label_items: tuple[tuple[str, str], ...] = ()
        if labels:
            cleaned: list[tuple[str, str]] = []
            for key, value in labels.items():
                if (
                    isinstance(key, str)
                    and _LABEL_NAME_RE.match(key) is not None
                    and isinstance(value, str)
                    and len(value) <= 64
                ):
                    cleaned.append((key, value))
            label_items = tuple(sorted(cleaned))
        series = (name, label_items)
        with self._lock:
            if series in self._counters:
                self._counters[series] += amount
            elif len(self._counters) < self._max_series:
                self._counters[series] = amount

    def render_prometheus(self) -> str:
        with self._lock:
            series = list(self._counters.items())
        lines: list[str] = []
        seen_help: set[str] = set()
        for (name, labels), total in series:
            if name not in seen_help:
                lines.append(f"# TYPE {name} counter")
                seen_help.add(name)
            if labels:
                rendered = ",".join(
                    f'{key}="{_escape_label_value(value)}"'
                    for key, value in labels
                )
                lines.append(f"{name}{{{rendered}}} {total}")
            else:
                lines.append(f"{name} {total}")
        return "\n".join(lines) + ("\n" if lines else "")


def record_request_metric(
    metrics: MetricsRegistry,
    *,
    method: str,
    status: int,
) -> None:
    """Increment the standard request counter for one completed response."""

    normalized_method = method.upper() if isinstance(method, str) else "UNKNOWN"
    if normalized_method not in _KNOWN_METHODS:
        normalized_method = "OTHER"
    status_class = f"{max(1, min(5, status // 100))}xx" if isinstance(
        status, int
    ) else "unknown"
    metrics.increment(
        "jarvis_backend_requests_total",
        {"method": normalized_method, "status": status_class},
    )


_KNOWN_METHODS: Final = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)


__all__ = [
    "InMemoryMetrics",
    "JsonLogFormatter",
    "MetricsRegistry",
    "NullMetrics",
    "configure_json_logging",
    "new_request_id",
    "record_request_metric",
    "redact_mapping",
    "redact_text",
    "resolve_request_id",
    "sanitize_request_id",
]

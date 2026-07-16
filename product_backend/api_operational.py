"""Operational ASGI layer: health, readiness, metrics, HTTPS, correlation IDs.

This middleware is installed as the outermost layer so that:

* ``/healthz`` and ``/readyz`` answer even when the ``Host`` header would fail
  ``TrustedHostMiddleware`` (load balancers probe on the pod/instance IP) and
  regardless of the HTTPS policy (a plain-HTTP liveness probe must still work).
* the HTTPS policy is enforced before any application work happens, and the
  forwarded scheme is trusted **only** when the direct peer is a configured
  trusted proxy — never by default.
* every request carries a correlation ID that is echoed in ``X-Request-ID`` and
  attached to the structured access log line.

It has no import-time side effects and depends only on the standard library plus
the observability helpers.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlsplit

from .api_auth import BackendConfigurationError, TrustedProxyConfig
from .observability import (
    MetricsRegistry,
    NullMetrics,
    record_request_metric,
    resolve_request_id,
)

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSEY: Final = frozenset({"0", "false", "no", "off"})
_SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
_DEFAULT_HSTS_MAX_AGE: Final = 63_072_000  # two years
_MIN_HSTS_MAX_AGE: Final = 300
_MAX_HSTS_MAX_AGE: Final = 63_072_000

# A readiness probe returns (ready, details); details are non-secret booleans.
ReadinessProbe = Callable[[], "ReadinessResult"]


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checks: Mapping[str, bool]


def always_ready() -> ReadinessResult:
    return ReadinessResult(True, {})


@dataclass(frozen=True, slots=True)
class OperationalPolicy:
    """Deployment-time operational and HTTPS policy.

    ``require_https`` rejects (or, with ``https_redirect``, 308-redirects safe
    methods) any request whose resolved scheme is not HTTPS.  Health and
    readiness probes are always exempt.  ``metrics_token`` gates the Prometheus
    exposition; when it is empty the ``/metrics`` path is disabled (404).
    """

    require_https: bool = False
    https_redirect: bool = False
    hsts_max_age: int = _DEFAULT_HSTS_MAX_AGE
    metrics_token: str = ""
    allowed_hosts: tuple[str, ...] = ()
    health_path: str = "/healthz"
    readiness_path: str = "/readyz"
    metrics_path: str = "/metrics"

    def __post_init__(self) -> None:
        if type(self.require_https) is not bool:
            raise BackendConfigurationError("require_https must be boolean")
        if type(self.https_redirect) is not bool:
            raise BackendConfigurationError("https_redirect must be boolean")
        if (
            type(self.hsts_max_age) is not int
            or not _MIN_HSTS_MAX_AGE <= self.hsts_max_age <= _MAX_HSTS_MAX_AGE
        ):
            raise BackendConfigurationError("HSTS max-age is invalid")
        if not isinstance(self.metrics_token, str) or len(self.metrics_token) > 512:
            raise BackendConfigurationError("metrics token is invalid")
        if self.metrics_token and len(self.metrics_token) < 16:
            raise BackendConfigurationError("metrics token is too short")
        for label, path in (
            ("health", self.health_path),
            ("readiness", self.readiness_path),
            ("metrics", self.metrics_path),
        ):
            if not isinstance(path, str) or not path.startswith("/") or len(path) > 128:
                raise BackendConfigurationError(f"{label} path is invalid")
        if len({self.health_path, self.readiness_path, self.metrics_path}) != 3:
            raise BackendConfigurationError("operational paths must be distinct")

    @property
    def metrics_enabled(self) -> bool:
        return bool(self.metrics_token)

    @classmethod
    def from_env(
        cls,
        source: Mapping[str, str],
        *,
        allowed_hosts: tuple[str, ...] = (),
    ) -> OperationalPolicy:
        require_https = _env_flag(source, "JARVIS_REQUIRE_HTTPS")
        https_redirect = _env_flag(source, "JARVIS_HTTPS_REDIRECT")
        raw_max_age = source.get("JARVIS_HSTS_MAX_AGE", "").strip()
        if raw_max_age:
            try:
                hsts_max_age = int(raw_max_age)
            except (TypeError, ValueError) as exc:
                raise BackendConfigurationError(
                    "JARVIS_HSTS_MAX_AGE must be an integer"
                ) from exc
        else:
            hsts_max_age = _DEFAULT_HSTS_MAX_AGE
        metrics_token = source.get("JARVIS_METRICS_TOKEN", "").strip()
        return cls(
            require_https=require_https,
            https_redirect=https_redirect,
            hsts_max_age=hsts_max_age,
            metrics_token=metrics_token,
            allowed_hosts=tuple(allowed_hosts),
        )


def _env_flag(source: Mapping[str, str], name: str) -> bool:
    raw = source.get(name, "").strip().lower()
    if not raw:
        return False
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    raise BackendConfigurationError(f"{name} must be a boolean")


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return None
    return None


class OperationalMiddleware:
    """Outermost ASGI middleware for operational endpoints and HTTPS policy."""

    def __init__(
        self,
        app: Any,
        *,
        policy: OperationalPolicy,
        readiness_probe: ReadinessProbe = always_ready,
        metrics: MetricsRegistry | None = None,
        logger: logging.Logger | None = None,
        proxy_config: TrustedProxyConfig | None = None,
    ) -> None:
        if not isinstance(policy, OperationalPolicy):
            raise BackendConfigurationError("operational policy is invalid")
        proxy = TrustedProxyConfig(()) if proxy_config is None else proxy_config
        if not isinstance(proxy, TrustedProxyConfig):
            raise BackendConfigurationError("trusted proxy configuration is invalid")
        self._app = app
        self._policy = policy
        self._readiness_probe = readiness_probe
        self._metrics: MetricsRegistry = metrics or NullMetrics()
        self._logger = logger or logging.getLogger("jarvis.backend.access")
        self._proxy = proxy

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        headers = list(scope.get("headers", ()))
        method = str(scope.get("method", "GET")).upper()
        path = scope.get("path", "")
        request_id = resolve_request_id(_header_value(headers, b"x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id
        client = scope.get("client")
        peer = client[0] if isinstance(client, (tuple, list)) and client else None
        scheme = self._resolved_scheme(scope, headers, peer)
        client_ip = self._proxy.client_ip(
            peer, _header_value(headers, b"x-forwarded-for")
        )

        policy = self._policy
        if path == policy.health_path:
            await self._respond_health(send, request_id)
            self._log(request_id, method, path, 200, client_ip, scheme, 0.0)
            return
        if path == policy.readiness_path:
            status = await self._respond_readiness(send, request_id)
            self._log(request_id, method, path, status, client_ip, scheme, 0.0)
            return
        if policy.require_https and scheme != "https":
            status = await self._reject_insecure(
                send, request_id, method, headers, path, scope
            )
            self._log(
                request_id, method, path, status, client_ip, scheme, 0.0,
                reason="https_required",
            )
            record_request_metric(self._metrics, method=method, status=status)
            return

        if path == policy.metrics_path:
            status = await self._respond_metrics(
                send,
                request_id,
                headers,
                hsts=policy.require_https and scheme == "https",
            )
            self._log(request_id, method, path, status, client_ip, scheme, 0.0)
            record_request_metric(self._metrics, method=method, status=status)
            return

        start = time.monotonic()
        status_holder: dict[str, int] = {"status": 0}
        hsts = policy.require_https and scheme == "https"

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
                raw = list(message.get("headers", ()))
                raw.append((b"x-request-id", request_id.encode("latin-1")))
                if hsts:
                    raw.append((
                        b"strict-transport-security",
                        f"max-age={policy.hsts_max_age}; includeSubDomains".encode(
                            "latin-1"
                        ),
                    ))
                message = {**message, "headers": raw}
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            status = status_holder["status"] or 500
            record_request_metric(self._metrics, method=method, status=status)
            self._log(
                request_id, method, path, status, client_ip, scheme, duration_ms
            )

    def _resolved_scheme(
        self,
        scope: dict[str, Any],
        headers: list[tuple[bytes, bytes]],
        peer: object,
    ) -> str:
        base = str(scope.get("scheme", "http")).lower()
        if not self._proxy.is_trusted_peer(peer):
            return base
        forwarded = _header_value(headers, b"x-forwarded-proto")
        if not forwarded:
            return base
        candidate = forwarded.split(",")[0].strip().lower()
        if candidate in ("http", "https"):
            return candidate
        return base

    async def _respond_health(self, send: Any, request_id: str) -> None:
        await _send_json(send, 200, {"status": "ok"}, request_id)

    async def _respond_readiness(self, send: Any, request_id: str) -> int:
        try:
            result = self._readiness_probe()
            if not isinstance(result, ReadinessResult):
                result = ReadinessResult(bool(result), {})
        except Exception:  # noqa: BLE001 - readiness must never crash the probe
            result = ReadinessResult(False, {"probe": False})
        status = 200 if result.ready else 503
        body = {
            "status": "ready" if result.ready else "not_ready",
            "checks": {
                str(key): bool(value) for key, value in result.checks.items()
            },
        }
        await _send_json(send, status, body, request_id)
        return status

    async def _respond_metrics(
        self,
        send: Any,
        request_id: str,
        headers: list[tuple[bytes, bytes]],
        *,
        hsts: bool,
    ) -> int:
        policy = self._policy
        security_headers = (
            ((
                b"strict-transport-security",
                f"max-age={policy.hsts_max_age}; includeSubDomains".encode(
                    "latin-1"
                ),
            ),)
            if hsts
            else ()
        )
        if not self._request_host_allowed(headers):
            await _send_json(
                send,
                400,
                {"detail": "invalid host header"},
                request_id,
                extra_headers=security_headers,
            )
            return 400
        if not policy.metrics_enabled:
            await _send_json(
                send,
                404,
                {"detail": "not found"},
                request_id,
                extra_headers=security_headers,
            )
            return 404
        provided = _header_value(headers, b"authorization") or ""
        expected = f"Bearer {policy.metrics_token}"
        if not secrets.compare_digest(provided, expected):
            await _send_json(
                send,
                401,
                {"detail": "metrics authentication required"},
                request_id,
                extra_headers=security_headers,
            )
            return 401
        body = self._metrics.render_prometheus().encode("utf-8")
        await _send_bytes(
            send,
            200,
            body,
            b"text/plain; version=0.0.4; charset=utf-8",
            request_id,
            extra_headers=security_headers,
        )
        return 200

    def _request_host_allowed(
        self,
        headers: list[tuple[bytes, bytes]],
    ) -> bool:
        allowed = self._policy.allowed_hosts
        if not allowed:
            return True
        host = _header_value(headers, b"host")
        if (
            not host
            or "\r" in host
            or "\n" in host
            or len(host) > 261
        ):
            return False
        try:
            parsed = urlsplit(f"//{host}")
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            return False
        if (
            hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        return hostname.casefold() in {item.casefold() for item in allowed}

    async def _reject_insecure(
        self,
        send: Any,
        request_id: str,
        method: str,
        headers: list[tuple[bytes, bytes]],
        path: str,
        scope: dict[str, Any],
    ) -> int:
        if self._policy.https_redirect and method in _SAFE_METHODS:
            location = self._https_redirect_target(headers, path, scope)
            if location is not None:
                await _send_redirect(send, location, request_id)
                return 308
        await _send_json(
            send, 400, {"detail": "https is required"}, request_id
        )
        return 400

    def _https_redirect_target(
        self,
        headers: list[tuple[bytes, bytes]],
        path: str,
        scope: dict[str, Any],
    ) -> str | None:
        host = _header_value(headers, b"host")
        # Redirects are enabled only with an explicit trusted-host allowlist.
        # Parse/rebuild the authority so user-info and delimiter tricks can
        # never turn an allowed prefix into an external redirect target.
        if (
            not self._policy.allowed_hosts
            or not self._request_host_allowed(headers)
            or host is None
        ):
            return None
        try:
            parsed = urlsplit(f"//{host}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if hostname is None:
            return None
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority = f"{authority}:{port}"
        safe_path = quote(path, safe="/%:@!$&'()*+,;=~-._")
        query = scope.get("query_string", b"")
        suffix = ""
        if isinstance(query, (bytes, bytearray)) and query:
            suffix = "?" + query.decode("latin-1")
        return f"https://{authority}{safe_path}{suffix}"

    def _log(
        self,
        request_id: str,
        method: str,
        path: str,
        status: int,
        client_ip: str,
        scheme: str,
        duration_ms: float,
        *,
        reason: str | None = None,
    ) -> None:
        extra = {
            "event": "http_request",
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "client_ip": client_ip,
            "scheme": scheme,
            "duration_ms": round(duration_ms, 3),
        }
        if reason is not None:
            extra["reason"] = reason
        self._logger.info("request", extra=extra)


async def _send_json(
    send: Any,
    status: int,
    body: dict[str, Any],
    request_id: str,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await _send_bytes(
        send,
        status,
        payload,
        b"application/json",
        request_id,
        extra_headers=extra_headers,
    )


async def _send_bytes(
    send: Any,
    status: int,
    body: bytes,
    content_type: bytes,
    request_id: str,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode("latin-1")),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode("latin-1")),
            *extra_headers,
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_redirect(send: Any, location: str, request_id: str) -> None:
    await send({
        "type": "http.response.start",
        "status": 308,
        "headers": [
            (b"location", location.encode("latin-1")),
            (b"content-length", b"0"),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": b""})


__all__ = [
    "OperationalMiddleware",
    "OperationalPolicy",
    "ReadinessProbe",
    "ReadinessResult",
    "always_ready",
]

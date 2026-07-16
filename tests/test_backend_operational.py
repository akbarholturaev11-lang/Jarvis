from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from product_backend.api_auth import BackendConfigurationError, TrustedProxyConfig
from product_backend.api_operational import (
    OperationalMiddleware,
    OperationalPolicy,
    ReadinessResult,
)
from product_backend.observability import InMemoryMetrics


async def _echo_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"inner-ok"})


def _call(
    app: Any,
    method: str,
    path: str,
    *,
    client: tuple[str, int] = ("203.0.113.9", 5555),
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    query: bytes = b"",
) -> tuple[int, dict[bytes, bytes], bytes]:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "scheme": scheme,
        "client": client,
        "query_string": query,
        "headers": headers or [],
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    header_map = {key.lower(): value for key, value in start["headers"]}
    return start["status"], header_map, body


class OperationalPolicyConfigTests(unittest.TestCase):
    def test_metrics_token_must_be_long_enough(self) -> None:
        with self.assertRaises(BackendConfigurationError):
            OperationalPolicy(metrics_token="short")

    def test_from_env_parses_flags(self) -> None:
        policy = OperationalPolicy.from_env(
            {
                "JARVIS_REQUIRE_HTTPS": "true",
                "JARVIS_HTTPS_REDIRECT": "1",
                "JARVIS_METRICS_TOKEN": "0123456789abcdef",
            },
            allowed_hosts=("product.example.com",),
        )
        self.assertTrue(policy.require_https)
        self.assertTrue(policy.https_redirect)
        self.assertTrue(policy.metrics_enabled)

    def test_from_env_rejects_ambiguous_boolean_values(self) -> None:
        with self.assertRaises(BackendConfigurationError):
            OperationalPolicy.from_env({"JARVIS_REQUIRE_HTTPS": "truthy-ish"})
        with self.assertRaises(BackendConfigurationError):
            OperationalPolicy.from_env({"JARVIS_HTTPS_REDIRECT": "sometimes"})


class HealthReadinessTests(unittest.TestCase):
    def _middleware(self, *, ready: bool = True, **policy_kwargs: Any) -> Any:
        return OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(**policy_kwargs),
            readiness_probe=lambda: ReadinessResult(ready, {"database": ready}),
        )

    def test_health_answers_even_with_foreign_host(self) -> None:
        app = self._middleware()
        status, headers, body = _call(
            app, "GET", "/healthz", headers=[(b"host", b"10.0.0.5")]
        )
        self.assertEqual(status, 200)
        self.assertIn(b"ok", body)
        self.assertIn(b"x-request-id", headers)

    def test_readiness_reports_ready_and_not_ready(self) -> None:
        status, _, body = _call(self._middleware(ready=True), "GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertIn(b"ready", body)
        status, _, body = _call(self._middleware(ready=False), "GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertIn(b"not_ready", body)

    def test_readiness_probe_exception_is_not_ready(self) -> None:
        def _boom() -> ReadinessResult:
            raise RuntimeError("db down")

        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(),
            readiness_probe=_boom,
        )
        status, _, _ = _call(app, "GET", "/readyz")
        self.assertEqual(status, 503)


class HttpsEnforcementTests(unittest.TestCase):
    def test_http_rejected_when_https_required(self) -> None:
        app = OperationalMiddleware(
            _echo_app, policy=OperationalPolicy(require_https=True)
        )
        status, _, body = _call(app, "GET", "/api/releases", scheme="http")
        self.assertEqual(status, 400)
        self.assertIn(b"https is required", body)

    def test_http_get_redirected_when_redirect_enabled(self) -> None:
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(
                require_https=True,
                https_redirect=True,
                allowed_hosts=("product.example.com",),
            ),
        )
        status, headers, _ = _call(
            app,
            "GET",
            "/api/releases",
            scheme="http",
            headers=[(b"host", b"product.example.com")],
            query=b"a=1",
        )
        self.assertEqual(status, 308)
        self.assertEqual(
            headers[b"location"], b"https://product.example.com/api/releases?a=1"
        )

    def test_redirect_refused_for_unknown_host_open_redirect(self) -> None:
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(
                require_https=True,
                https_redirect=True,
                allowed_hosts=("product.example.com",),
            ),
        )
        status, _, _ = _call(
            app,
            "GET",
            "/api/releases",
            scheme="http",
            headers=[(b"host", b"evil.example.net")],
        )
        self.assertEqual(status, 400)

    def test_redirect_refuses_userinfo_authority_confusion(self) -> None:
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(
                require_https=True,
                https_redirect=True,
                allowed_hosts=("product.example.com",),
            ),
        )
        status, headers, _ = _call(
            app,
            "GET",
            "/api/releases",
            scheme="http",
            headers=[(b"host", b"product.example.com:443@evil.example")],
        )
        self.assertEqual(status, 400)
        self.assertNotIn(b"location", headers)

    def test_https_request_passes_and_sets_hsts(self) -> None:
        app = OperationalMiddleware(
            _echo_app, policy=OperationalPolicy(require_https=True)
        )
        status, headers, body = _call(
            app, "GET", "/api/releases", scheme="https"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"inner-ok")
        self.assertIn(b"strict-transport-security", headers)


class ForwardedSchemeTrustTests(unittest.TestCase):
    def test_forwarded_proto_spoof_is_ignored_without_trusted_proxy(self) -> None:
        app = OperationalMiddleware(
            _echo_app, policy=OperationalPolicy(require_https=True)
        )
        status, _, _ = _call(
            app,
            "POST",
            "/api/admin/session",
            scheme="http",
            client=("198.51.100.7", 40000),
            headers=[(b"x-forwarded-proto", b"https")],
        )
        # No trusted proxy configured, so the spoofed header is ignored: HTTP.
        self.assertEqual(status, 400)

    def test_forwarded_proto_trusted_from_configured_proxy(self) -> None:
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(require_https=True),
            proxy_config=TrustedProxyConfig.from_spec("10.0.0.0/8"),
        )
        status, headers, body = _call(
            app,
            "GET",
            "/api/releases",
            scheme="http",
            client=("10.1.2.3", 40000),
            headers=[(b"x-forwarded-proto", b"https")],
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"inner-ok")
        self.assertIn(b"strict-transport-security", headers)


class MetricsEndpointTests(unittest.TestCase):
    def test_metrics_disabled_returns_404(self) -> None:
        app = OperationalMiddleware(_echo_app, policy=OperationalPolicy())
        status, _, _ = _call(app, "GET", "/metrics")
        self.assertEqual(status, 404)

    def test_metrics_requires_bearer_token(self) -> None:
        metrics = InMemoryMetrics()
        metrics.increment("jarvis_backend_requests_total", {"method": "GET"})
        policy = OperationalPolicy(metrics_token="0123456789abcdef")
        app = OperationalMiddleware(_echo_app, policy=policy, metrics=metrics)
        status, _, _ = _call(app, "GET", "/metrics")
        self.assertEqual(status, 401)
        status, _, body = _call(
            app,
            "GET",
            "/metrics",
            headers=[(b"authorization", b"Bearer 0123456789abcdef")],
        )
        self.assertEqual(status, 200)
        self.assertIn(b"jarvis_backend_requests_total", body)

    def test_metrics_cannot_bypass_https_and_secure_response_has_hsts(self) -> None:
        token = "0123456789abcdef"
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(require_https=True, metrics_token=token),
            metrics=InMemoryMetrics(),
        )
        auth = [(b"authorization", f"Bearer {token}".encode("ascii"))]
        status, _, body = _call(
            app, "GET", "/metrics", scheme="http", headers=auth
        )
        self.assertEqual(status, 400)
        self.assertIn(b"https is required", body)
        status, secure_headers, _ = _call(
            app, "GET", "/metrics", scheme="https", headers=auth
        )
        self.assertEqual(status, 200)
        self.assertIn(b"strict-transport-security", secure_headers)

    def test_metrics_cannot_bypass_the_trusted_host_boundary(self) -> None:
        token = "0123456789abcdef"
        app = OperationalMiddleware(
            _echo_app,
            policy=OperationalPolicy(
                require_https=True,
                metrics_token=token,
                allowed_hosts=("product.example.com",),
            ),
            metrics=InMemoryMetrics(),
        )
        auth = [(b"authorization", f"Bearer {token}".encode("ascii"))]
        status, _, _ = _call(
            app,
            "GET",
            "/metrics",
            scheme="https",
            headers=[*auth, (b"host", b"evil.example.net")],
        )
        self.assertEqual(status, 400)
        status, _, _ = _call(
            app,
            "GET",
            "/metrics",
            scheme="https",
            headers=[*auth, (b"host", b"product.example.com:443")],
        )
        self.assertEqual(status, 200)


class CorrelationIdTests(unittest.TestCase):
    def test_incoming_safe_request_id_is_preserved(self) -> None:
        app = OperationalMiddleware(_echo_app, policy=OperationalPolicy())
        status, headers, _ = _call(
            app,
            "GET",
            "/api/releases",
            headers=[(b"x-request-id", b"req_incoming1234")],
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"x-request-id"], b"req_incoming1234")

    def test_unsafe_request_id_is_replaced(self) -> None:
        app = OperationalMiddleware(_echo_app, policy=OperationalPolicy())
        _, headers, _ = _call(
            app,
            "GET",
            "/api/releases",
            headers=[(b"x-request-id", b"bad id !!")],
        )
        self.assertTrue(headers[b"x-request-id"].startswith(b"req_"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _runtime_environment(root: Path) -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from product_backend.api_auth import AdminPasswordCredential

    data = root / "data"
    artifacts = root / "artifacts"
    data.mkdir(mode=0o700)
    artifacts.mkdir(mode=0o700)
    entitlement = Ed25519PrivateKey.generate()
    entitlement_file = root / "entitlement.key"
    entitlement_file.write_bytes(
        entitlement.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    entitlement_file.chmod(0o600)
    pepper_file = root / "activation.pepper"
    pepper_file.write_bytes(b"p" * 32)
    pepper_file.chmod(0o600)
    mfa_key_file = root / "admin-mfa.key"
    mfa_key_file.write_bytes(b"m" * 32)
    mfa_key_file.chmod(0o600)
    release = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    credential = AdminPasswordCredential.derive_for_configuration(
        subject="admin:runtime",
        password="strong-runtime-password",
        salt=b"s" * 32,
    )
    return {
        "JARVIS_BACKEND_DATA_DIR": str(data),
        "JARVIS_RELEASE_ARTIFACT_ROOT": str(artifacts),
        "JARVIS_RELEASE_PUBLIC_KEYS_JSON": json.dumps({"release-key-001": _b64(release)}),
        "JARVIS_ENTITLEMENT_KEY_ID": "entitlement-key-001",
        "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE": str(entitlement_file),
        "JARVIS_ACTIVATION_PEPPER_FILE": str(pepper_file),
        "JARVIS_ADMIN_MFA_KEY_FILE": str(mfa_key_file),
        "JARVIS_ADMIN_SUBJECT": credential.subject,
        "JARVIS_ADMIN_PASSWORD_SALT_B64URL": _b64(credential.salt),
        "JARVIS_ADMIN_PASSWORD_HASH_B64URL": _b64(credential.password_digest),
        "JARVIS_ADMIN_PBKDF2_ITERATIONS": str(credential.iterations),
        "JARVIS_ADMIN_SESSION_SECRET_B64URL": _b64(b"z" * 32),
        "JARVIS_API_ALLOWED_HOSTS": "product.example.com",
        "JARVIS_REQUIRE_HTTPS": "true",
    }


@unittest.skipUnless(os.name == "posix", "assembled backend runtime is POSIX-only")
class AssembledAppOperationalTests(unittest.TestCase):
    """Health/host behavior through the fully assembled runtime app."""

    def test_health_bypasses_trusted_host_but_routes_do_not(self) -> None:
        from fastapi.testclient import TestClient

        from product_backend.runtime import create_app_from_environment

        with tempfile.TemporaryDirectory() as temp:
            app = create_app_from_environment(_runtime_environment(Path(temp).resolve()))
            try:
                # Liveness answers regardless of the Host header.
                with TestClient(app, base_url="http://10.9.8.7") as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)
                    self.assertEqual(client.get("/readyz").status_code, 200)
                # A real route is rejected for a foreign host by TrustedHost.
                with TestClient(app, base_url="https://evil.example.net") as client:
                    self.assertEqual(client.get("/api/releases").status_code, 400)
                # The configured host is accepted.
                with TestClient(app, base_url="https://product.example.com") as client:
                    response = client.get("/api/releases")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("x-request-id", response.headers)
            finally:
                app.state.close_backend_resources()


if __name__ == "__main__":
    unittest.main()

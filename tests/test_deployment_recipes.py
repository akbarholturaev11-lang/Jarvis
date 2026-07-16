from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class DeploymentRecipeTests(unittest.TestCase):
    def test_root_docker_context_is_a_deny_by_default_allowlist(self) -> None:
        self.assertFalse((ROOT / "deploy/docker/.dockerignore").exists())
        lines = [
            line.strip()
            for line in _read(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(lines[0], "**")
        for required in (
            "!.dockerignore",
            "!core/",
            "!core/**",
            "!product_backend/",
            "!product_backend/**",
            "!ops/",
            "!ops/**",
            "!deploy/",
            "!deploy/docker/",
            "!deploy/docker/Dockerfile",
            "!deploy/docker/requirements-backend.txt",
            "product_backend/runtime/",
            "**/__pycache__/",
            "**/*.sqlite3",
            "**/*.key",
            "**/*.pem",
        ):
            self.assertIn(required, lines)
        self.assertFalse(
            any(
                line.startswith(("!tests", "!config", "!memory", "!.git"))
                for line in lines
            )
        )

    def test_real_compose_env_file_is_gitignored(self) -> None:
        ignore = _read(".gitignore").splitlines()
        self.assertIn("/deploy/env/backend.env", ignore)
        self.assertIn("!deploy/env/backend.env.example", ignore)

    def test_backend_image_has_runtime_dependencies_and_safe_logging(self) -> None:
        dockerfile = _read("deploy/docker/Dockerfile")
        requirements = _read("deploy/docker/requirements-backend.txt").lower()
        self.assertIn('USER jarvis', dockerfile)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertNotIn("--proxy-headers", dockerfile)
        self.assertNotIn("--forwarded-allow-ips", dockerfile)
        for dependency in (
            "fastapi==0.139.0",
            "uvicorn[standard]==0.51.0",
            "cryptography==49.0.0",
            "python-multipart==0.0.32",
            "qrcode==8.2",
            "pillow==12.3.0",
        ):
            self.assertIn(dependency, requirements)

    def test_compose_pins_proxy_and_mounts_only_online_runtime_secrets(self) -> None:
        compose = _read("deploy/docker/docker-compose.yml")
        backend = compose.split("\n  proxy:\n", 1)[0]

        for variable in (
            "JARVIS_COMPOSE_ENV_FILE",
            "JARVIS_COMPOSE_ENTITLEMENT_KEY_FILE",
            "JARVIS_COMPOSE_ACTIVATION_PEPPER_FILE",
            "JARVIS_COMPOSE_ADMIN_MFA_KEY_FILE",
            "JARVIS_COMPOSE_TLS_CERT_DIR",
        ):
            self.assertIn(f"${{{variable}:?set an absolute host path", compose)

        for target in (
            "/run/jarvis-secrets/entitlement.key",
            "/run/jarvis-secrets/activation.pepper",
            "/run/jarvis-secrets/admin-mfa.key",
        ):
            self.assertEqual(backend.count(f"target: {target}"), 1)
            self.assertGreaterEqual(backend.count(target), 2)

        self.assertEqual(backend.count("read_only: true"), 4)
        self.assertNotIn("release-signing.key", compose)
        self.assertNotIn("JARVIS_RELEASE_PRIVATE", compose)
        self.assertNotIn("--proxy-headers", compose)
        self.assertNotIn("\n    ports:", backend)
        self.assertIn("JARVIS_TRUSTED_PROXIES: 172.30.250.2/32", compose)
        self.assertIn("ipv4_address: 172.30.250.2", compose)
        self.assertIn("ipv4_address: 172.30.250.3", compose)
        self.assertIn("subnet: 172.30.250.0/29", compose)
        self.assertIn("internal: true", compose)

    def test_reverse_proxy_limits_are_truthful_and_upload_fits(self) -> None:
        nginx = _read("deploy/reverse-proxy/nginx.conf")
        caddy = _read("deploy/reverse-proxy/Caddyfile")
        self.assertIn("client_max_body_size 11m;", nginx)
        self.assertIn("limit_req_zone", nginx)
        self.assertIn("location = /metrics { deny all; }", nginx)
        self.assertIn("Stock Caddy has no built-in", caddy)
        self.assertNotIn("rate_limit", caddy)

    def test_systemd_keeps_proxy_resolution_inside_the_application(self) -> None:
        service = _read("deploy/systemd/jarvis-backend.service")
        self.assertEqual(
            service.count(
                "/usr/bin/env JARVIS_TRUSTED_PROXIES=127.0.0.1/32"
            ),
            2,
        )
        self.assertIn("--no-access-log", service)
        self.assertNotIn("--proxy-headers", service)
        self.assertNotIn("--forwarded-allow-ips", service)
        self.assertNotIn("--env-file", service)

    def test_runbook_validates_compose_inside_the_container(self) -> None:
        readme = _read("deploy/README.md")
        runbook = _read("docs/PRODUCTION_DEPLOYMENT.md")
        for document in (readme, runbook):
            self.assertIn(
                "run --rm --no-deps backend \\\n    python -m ops.validate_config",
                document,
            )
            self.assertIn("release-signing private key", document)
            self.assertIn("absolute", document.lower())
            self.assertIn("host path", document.lower())

        self.assertIn("X-Artifact-Grant", runbook)
        self.assertIn("must not\nlog it", runbook)
        self.assertIn("Do not enable uvicorn's\n  proxy-header rewriting", runbook)
        self.assertIn("Stock Caddy has no built-in", runbook)
        self.assertIn('-o "$(id -u)" -g "$(id -g)"', readme)


if __name__ == "__main__":
    unittest.main()

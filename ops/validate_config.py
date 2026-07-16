"""Fail-closed validation of a production backend configuration.

Usage::

    python -m ops.validate_config --env-file /etc/jarvis/backend.env

The check runs structural pre-checks (required variables, wildcard hosts, HTTPS
policy, secret-file permissions) and then, unless ``--no-build`` is given,
assembles the real ASGI app via ``create_app_from_environment`` and closes it.
Assembly is authoritative: it exercises every fail-closed guard in the runtime
(directory/file permissions, key formats, admin credentials, MFA key).  The
command exits non-zero if any error is found so it can gate a deployment.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from product_backend.api_auth import BackendConfigurationError
from product_backend.runtime import (
    _REQUIRED_RUNTIME_ENV,
    create_app_from_environment,
)

from ._common import POSIX, emit, file_is_owner_only

_REQUIRED_AUTH_ENV = (
    "JARVIS_ADMIN_SUBJECT",
    "JARVIS_ADMIN_PASSWORD_SALT_B64URL",
    "JARVIS_ADMIN_PASSWORD_HASH_B64URL",
    "JARVIS_ADMIN_PBKDF2_ITERATIONS",
    "JARVIS_ADMIN_SESSION_SECRET_B64URL",
    "JARVIS_API_ALLOWED_HOSTS",
)
_SECRET_FILE_ENV = (
    "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE",
    "JARVIS_ACTIVATION_PEPPER_FILE",
    "JARVIS_ADMIN_MFA_KEY_FILE",
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` env file (values may be JSON-quoted)."""

    env: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] == '"' and value[-1:] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        env[key] = value
    return env


def _structural_checks(env: Mapping[str, str], report: ValidationReport) -> None:
    for name in (*_REQUIRED_RUNTIME_ENV, *_REQUIRED_AUTH_ENV):
        if not env.get(name):
            report.error(f"missing required variable {name}")

    hosts_raw = env.get("JARVIS_API_ALLOWED_HOSTS", "")
    hosts = [item.strip() for item in hosts_raw.split(",") if item.strip()]
    if not hosts:
        report.error("JARVIS_API_ALLOWED_HOSTS must list at least one host")
    if any(host == "*" for host in hosts):
        report.error("JARVIS_API_ALLOWED_HOSTS must not contain a wildcard")

    if env.get("JARVIS_REQUIRE_HTTPS", "").strip().lower() not in _TRUTHY:
        report.warn(
            "JARVIS_REQUIRE_HTTPS is not enabled; the app will accept plain "
            "HTTP unless the edge terminates and enforces TLS"
        )
    if env.get("JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY", "").strip().lower() in _TRUTHY:
        report.error(
            "JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY must never be set in production"
        )
    if not env.get("JARVIS_ADMIN_ALLOWED_NETWORKS") and not env.get(
        "JARVIS_TRUSTED_PROXIES"
    ):
        report.warn(
            "neither JARVIS_ADMIN_ALLOWED_NETWORKS nor JARVIS_TRUSTED_PROXIES is "
            "set; ensure the network edge restricts admin access"
        )

    for name in _SECRET_FILE_ENV:
        value = env.get(name)
        if not value:
            continue
        path = Path(value)
        if not path.is_file():
            report.error(f"{name} does not point to a regular file: {value}")
            continue
        if POSIX and not file_is_owner_only(path):
            report.error(f"{name} is not owner-only (need 0600): {value}")


def _build_check(env: Mapping[str, str], report: ValidationReport) -> None:
    try:
        app = create_app_from_environment(env)
    except BackendConfigurationError as exc:
        report.error(f"app assembly failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - surface any startup failure
        report.error(f"app assembly raised {type(exc).__name__}: {exc}")
        return
    try:
        data_dir = Path(env["JARVIS_BACKEND_DATA_DIR"])
        evidence_dir = data_dir / "payment-evidence"
        if POSIX and evidence_dir.exists():
            info = evidence_dir.stat()
            if info.st_mode & 0o077:
                report.error("payment-evidence directory is not owner-only (0700)")
    finally:
        closer = getattr(app.state, "close_backend_resources", None)
        if callable(closer):
            closer()


def validate(env: Mapping[str, str], *, build: bool = True) -> ValidationReport:
    report = ValidationReport()
    _structural_checks(env, report)
    if build and report.ok:
        _build_check(env, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a JARVIS backend configuration fail-closed."
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args(argv)

    env: dict[str, str] = dict(os.environ)
    if args.env_file is not None:
        env.update(load_env_file(args.env_file))

    report = validate(env, build=not args.no_build)
    for warning in report.warnings:
        emit(f"[warn] {warning}")
    for error in report.errors:
        emit(f"[error] {error}")
    if report.ok:
        emit("[ok] configuration is valid")
        return 0
    emit(f"[fail] {len(report.errors)} configuration error(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate create-only, owner-only deployment secret material.

No private value is printed.  The generated environment is always written to an
owner-only file, and a generated bootstrap password is written once to a separate
owner-only file.  Operators who supply a password must use an owner-only input
file; command-line password values are rejected before ``argparse`` can echo them.
HTTPS is mandatory and has no production-disable switch.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from product_backend.api_auth import (
    MIN_PBKDF2_ITERATIONS,
    AdminPasswordCredential,
)

from ._common import (
    OpsNotAvailableError,
    POSIX,
    PermissionResult,
    emit,
    ensure_private_directory,
    eprint,
    file_is_owner_only,
    read_stable_bytes,
    reject_repository_output_path,
    require_permission_applied,
    require_secure_ops_platform,
    validate_write_target,
    write_secret_bytes,
    write_secret_text,
)

DEFAULT_ENTITLEMENT_KEY_ID = "entitlement-key-001"
DEFAULT_RELEASE_KEY_ID = "release-key-001"
DEFAULT_ENV_FILENAME = "backend.env"
INITIAL_PASSWORD_FILENAME = "initial-admin-password.txt"
# Non-secret public trust material for building the client ``product.json``. The
# entitlement public key is derived here because generation is the only moment it
# is known alongside the release public key; the client can never see the private
# key. See ``ops.build_client_config``.
CLIENT_TRUST_FILENAME = "client-trust.json"
CLIENT_TRUST_SCHEMA = "jarvis.product-client-trust.v1"
_MAX_PASSWORD_FILE_BYTES = 4096
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _raw_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _generate_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(24))


@dataclass(frozen=True, slots=True)
class SecretBundle:
    out_dir: Path
    env: dict[str, str] = field(repr=False)
    files: dict[str, Path]
    initial_admin_password_file: Path | None
    permission_notes: list[str] = field(default_factory=list)
    client_trust: dict[str, object] = field(default_factory=dict)

    def render_client_trust(self) -> str:
        return json.dumps(self.client_trust, indent=2, sort_keys=True) + "\n"

    def render_env(self) -> str:
        lines = [
            "# JARVIS product backend environment (generated).",
            "# Keep this file owner-only and outside the repository.",
            "# Set the two path placeholders below to real owner-only directories.",
            'JARVIS_BACKEND_DATA_DIR="/var/lib/jarvis/data"',
            'JARVIS_RELEASE_ARTIFACT_ROOT="/var/lib/jarvis/artifacts"',
        ]
        for key in sorted(self.env):
            lines.append(f"{key}={json.dumps(self.env[key])}")
        return "\n".join(lines) + "\n"


def generate_secret_bundle(
    out_dir: Path,
    *,
    admin_subject: str,
    allowed_hosts: str,
    admin_password: str | None = None,
    entitlement_key_id: str = DEFAULT_ENTITLEMENT_KEY_ID,
    release_key_id: str = DEFAULT_RELEASE_KEY_ID,
    pbkdf2_iterations: int = MIN_PBKDF2_ITERATIONS,
    require_https: bool = True,
    write_files: bool = True,
) -> SecretBundle:
    """Create secret material without ever returning a generated password."""

    if require_https is not True:
        raise ValueError("HTTPS cannot be disabled for generated deployment config")
    if not write_files and admin_password is None:
        raise ValueError("write_files=False requires an explicitly supplied password")

    require_secure_ops_platform()
    out_dir = reject_repository_output_path(Path(out_dir))
    notes: list[str] = []
    files: dict[str, Path] = {}

    def _record(result: PermissionResult, label: str) -> None:
        require_permission_applied(result, label=label)

    generated = admin_password is None
    password = admin_password if admin_password is not None else _generate_password()
    credential = AdminPasswordCredential.derive_for_configuration(
        subject=admin_subject,
        password=password,
        iterations=pbkdf2_iterations,
    )

    entitlement_key = Ed25519PrivateKey.generate()
    entitlement_path = out_dir / "entitlement.key"
    release_key = Ed25519PrivateKey.generate()
    release_private_path = out_dir / "release-signing.key"
    pepper_path = out_dir / "activation.pepper"
    mfa_path = out_dir / "admin-mfa.key"
    password_path = out_dir / INITIAL_PASSWORD_FILENAME if generated else None
    client_trust_path = out_dir / CLIENT_TRUST_FILENAME

    # Public (non-secret) trust material the client pins in ``product.json``.
    entitlement_public_keys = {entitlement_key_id: _b64url(_raw_public(entitlement_key))}
    release_public_keys = {release_key_id: _b64url(_raw_public(release_key))}
    client_trust: dict[str, object] = {
        "schema": CLIENT_TRUST_SCHEMA,
        "entitlement_public_keys": entitlement_public_keys,
        "release_public_keys": release_public_keys,
    }

    output_paths = [entitlement_path, release_private_path, pepper_path, mfa_path]
    if password_path is not None:
        output_paths.append(password_path)
    # The trust file is public, but keep it owner-only next to the secrets so a
    # single deployment step produces both the backend env and the client trust.
    output_paths.append(client_trust_path)
    if write_files:
        require_permission_applied(
            ensure_private_directory(out_dir),
            label="secret output directory",
        )
        # Fail before publishing the first secret when an output already exists.
        for path in output_paths:
            validate_write_target(path)
        _record(
            write_secret_bytes(entitlement_path, _raw_private(entitlement_key)),
            "entitlement.key",
        )
        _record(
            write_secret_bytes(release_private_path, _raw_private(release_key)),
            "release-signing.key",
        )
        _record(
            write_secret_bytes(pepper_path, secrets.token_bytes(32)),
            "activation.pepper",
        )
        _record(
            write_secret_bytes(mfa_path, secrets.token_bytes(32)),
            "admin-mfa.key",
        )
        if password_path is not None:
            _record(
                write_secret_text(password_path, password + "\n"),
                INITIAL_PASSWORD_FILENAME,
            )
        _record(
            write_secret_text(
                client_trust_path,
                json.dumps(client_trust, indent=2, sort_keys=True) + "\n",
            ),
            CLIENT_TRUST_FILENAME,
        )

    files.update(
        entitlement_key=entitlement_path,
        release_signing_key=release_private_path,
        activation_pepper=pepper_path,
        admin_mfa_key=mfa_path,
        client_trust=client_trust_path,
    )
    if password_path is not None:
        files["initial_admin_password"] = password_path

    hosts = ",".join(item.strip() for item in allowed_hosts.split(",") if item.strip())
    env: dict[str, str] = {
        "JARVIS_RELEASE_PUBLIC_KEYS_JSON": json.dumps(release_public_keys),
        "JARVIS_ENTITLEMENT_KEY_ID": entitlement_key_id,
        "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE": str(entitlement_path),
        "JARVIS_ACTIVATION_PEPPER_FILE": str(pepper_path),
        "JARVIS_ADMIN_MFA_KEY_FILE": str(mfa_path),
        "JARVIS_ADMIN_SUBJECT": admin_subject,
        "JARVIS_ADMIN_PASSWORD_SALT_B64URL": _b64url(credential.salt),
        "JARVIS_ADMIN_PASSWORD_HASH_B64URL": _b64url(credential.password_digest),
        "JARVIS_ADMIN_PBKDF2_ITERATIONS": str(credential.iterations),
        "JARVIS_ADMIN_SESSION_SECRET_B64URL": _b64url(secrets.token_bytes(32)),
        "JARVIS_API_ALLOWED_HOSTS": hosts,
        "JARVIS_REQUIRE_HTTPS": "true",
    }

    # Drop the only plaintext reference before constructing/returning an object
    # whose repr is safe for diagnostics.
    password = ""
    return SecretBundle(
        out_dir=out_dir,
        env=env,
        files=files,
        initial_admin_password_file=password_path,
        permission_notes=notes,
        client_trust=client_trust,
    )


def _read_admin_password(path: Path) -> str:
    if POSIX and not file_is_owner_only(path):
        raise PermissionError("admin password file must be owner-only")
    raw = read_stable_bytes(path, max_bytes=_MAX_PASSWORD_FILE_BYTES)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("admin password file must be UTF-8") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise ValueError("admin password file must contain exactly one value")
    return value


def _contains_forbidden_cli_secret(argv: list[str]) -> bool:
    return any(
        argument == "--admin-password" or argument.startswith("--admin-password=")
        for argument in argv
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if _contains_forbidden_cli_secret(raw_argv):
        eprint(
            "[fail] command-line passwords are disabled; use an owner-only "
            "--admin-password-file"
        )
        return 2

    parser = argparse.ArgumentParser(description="Generate JARVIS backend secrets.")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--admin-subject", required=True)
    parser.add_argument("--allowed-hosts", required=True)
    parser.add_argument("--admin-password-file", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args(raw_argv)

    env_file = args.env_file or (args.out_dir / DEFAULT_ENV_FILENAME)
    try:
        require_secure_ops_platform()
        reject_repository_output_path(args.out_dir)
        env_file = reject_repository_output_path(env_file)
        validate_write_target(env_file)
        admin_password = (
            _read_admin_password(args.admin_password_file)
            if args.admin_password_file is not None
            else None
        )
        bundle = generate_secret_bundle(
            args.out_dir,
            admin_subject=args.admin_subject,
            allowed_hosts=args.allowed_hosts,
            admin_password=admin_password,
        )
        result = require_permission_applied(
            write_secret_text(env_file, bundle.render_env()),
            label="environment file",
        )
    except OpsNotAvailableError:
        eprint(
            "[not_available] native owner-only secret output is not implemented "
            "on this platform"
        )
        return 1
    except Exception:  # noqa: BLE001 - never echo exception data containing a secret
        eprint(
            "[fail] secret generation failed; no secret value was printed. "
            "Private partial output may remain; do not reuse it."
        )
        return 1

    emit(f"[ok] wrote owner-only environment file: {env_file} ({result.status})")
    client_trust_file = bundle.files.get("client_trust")
    if client_trust_file is not None:
        emit(
            "[ok] wrote non-secret client trust material: "
            f"{client_trust_file} (build config/product.json with "
            "python -m ops.build_client_config)"
        )
    if bundle.initial_admin_password_file is not None:
        emit(
            "[ok] wrote the one-time bootstrap password to owner-only file: "
            f"{bundle.initial_admin_password_file}"
        )
    for note in bundle.permission_notes:
        eprint(f"[permission] {note}")
    eprint(
        "[reminder] Move release-signing.key to an offline signer; never commit "
        "any generated file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

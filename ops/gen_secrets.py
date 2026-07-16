"""Generate the secret material and environment template for a deployment.

Usage::

    python -m ops.gen_secrets --out-dir /etc/jarvis/secrets \
        --admin-subject admin:ops --allowed-hosts product.example.com

Every private value is written to an owner-only file (POSIX ``0600``) or, on a
platform without POSIX mode bits, written with an explicit ``manual`` hardening
note.  The command prints an environment template that references the generated
files and inline values; the release signing private key is written for the
offline pipeline and must be moved to an offline signer.  No secret is committed
to the repository or printed except a freshly generated admin password, which is
shown exactly once so the operator can store it.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from product_backend.api_auth import (
    MIN_PBKDF2_ITERATIONS,
    AdminPasswordCredential,
)

from ._common import PermissionResult, emit, eprint, write_secret_bytes

DEFAULT_ENTITLEMENT_KEY_ID = "entitlement-key-001"
DEFAULT_RELEASE_KEY_ID = "release-key-001"
_PASSWORD_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
)


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
    env: dict[str, str]
    files: dict[str, Path]
    generated_password: str | None
    permission_notes: list[str] = field(default_factory=list)

    def render_env(self) -> str:
        lines = [
            "# JARVIS product backend environment (generated).",
            "# Keep this file owner-only and outside the repository.",
            "# Set the two path placeholders below to real owner-only directories.",
            'JARVIS_BACKEND_DATA_DIR="/var/lib/jarvis/data"',
            'JARVIS_RELEASE_ARTIFACT_ROOT="/var/lib/jarvis/artifacts"',
        ]
        for key in sorted(self.env):
            lines.append(f'{key}={json.dumps(self.env[key])}')
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
    """Create all secret material for one backend deployment."""

    out_dir = Path(out_dir)
    notes: list[str] = []
    files: dict[str, Path] = {}

    def _record(result: PermissionResult, label: str) -> None:
        if result.status != "applied":
            notes.append(f"{label}: {result.status} - {result.note}")

    generated_password = admin_password or _generate_password()
    credential = AdminPasswordCredential.derive_for_configuration(
        subject=admin_subject,
        password=generated_password,
        iterations=pbkdf2_iterations,
    )

    entitlement_key = Ed25519PrivateKey.generate()
    entitlement_path = out_dir / "entitlement.key"
    release_key = Ed25519PrivateKey.generate()
    release_private_path = out_dir / "release-signing.key"
    pepper_path = out_dir / "activation.pepper"
    mfa_path = out_dir / "admin-mfa.key"

    if write_files:
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
    files.update(
        entitlement_key=entitlement_path,
        release_signing_key=release_private_path,
        activation_pepper=pepper_path,
        admin_mfa_key=mfa_path,
    )

    release_public_keys = {release_key_id: _b64url(_raw_public(release_key))}
    hosts = ",".join(
        item.strip() for item in allowed_hosts.split(",") if item.strip()
    )
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
    }
    if require_https:
        env["JARVIS_REQUIRE_HTTPS"] = "true"

    return SecretBundle(
        out_dir=out_dir,
        env=env,
        files=files,
        generated_password=None if admin_password else generated_password,
        permission_notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate JARVIS backend secrets.")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--admin-subject", required=True)
    parser.add_argument("--allowed-hosts", required=True)
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--no-require-https", action="store_true")
    args = parser.parse_args(argv)

    bundle = generate_secret_bundle(
        args.out_dir,
        admin_subject=args.admin_subject,
        allowed_hosts=args.allowed_hosts,
        admin_password=args.admin_password,
        require_https=not args.no_require_https,
    )
    env_text = bundle.render_env()
    if args.env_file is not None:
        result = write_secret_bytes(args.env_file, env_text.encode("utf-8"))
        emit(f"Wrote environment template to {args.env_file} ({result.status}).")
    else:
        emit(env_text)
    for note in bundle.permission_notes:
        eprint(f"[permission] {note}")
    if bundle.generated_password is not None:
        eprint(
            "[admin-password] Generated admin password (store securely, shown "
            f"once): {bundle.generated_password}"
        )
    eprint(
        "[reminder] Move release-signing.key to an offline signer; never commit "
        "any generated file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

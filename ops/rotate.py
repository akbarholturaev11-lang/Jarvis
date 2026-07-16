"""Generate create-only rotation material and print only non-secret guidance.

Private values are written to owner-only files.  In particular, session-secret
rotation emits a private dotenv fragment instead of returning or printing the
new HMAC secret.  Operators perform the documented managed-secret-store cutover;
the tool never silently mutates live deployment state.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._common import (
    OpsNotAvailableError,
    emit,
    ensure_private_directory,
    eprint,
    reject_repository_output_path,
    require_permission_applied,
    require_secure_ops_platform,
    validate_write_target,
    write_secret_bytes,
    write_secret_text,
)

KEY_TYPES = (
    "session-secret",
    "mfa-key",
    "activation-pepper",
    "entitlement-key",
    "release-key",
)
SESSION_SECRET_FILENAME = "session-secret.env"
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


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


@dataclass(frozen=True, slots=True)
class RotationResult:
    key_type: str
    env_updates: dict[str, str] = field(default_factory=dict, repr=False)
    files: dict[str, Path] = field(default_factory=dict)
    public_values: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _prepare_output(out_dir: Path, path: Path, *, write_files: bool) -> None:
    if not write_files:
        return
    require_permission_applied(
        ensure_private_directory(out_dir),
        label="rotation output directory",
    )
    validate_write_target(path)


def _validated_key_id(value: str) -> str:
    if not _KEY_ID_PATTERN.fullmatch(value):
        raise ValueError("key id must be 1-64 portable filename characters")
    return value


def rotate(
    key_type: str,
    out_dir: Path,
    *,
    key_id: str | None = None,
    write_files: bool = True,
) -> RotationResult:
    require_secure_ops_platform()
    if key_type == "mfa-key":
        raise OpsNotAvailableError(
            "admin MFA master-key rotation is not_available until stored TOTP "
            "secrets and recovery-code authenticators can be transactionally "
            "re-encrypted without locking out every administrator"
        )
    out_dir = reject_repository_output_path(Path(out_dir))
    if key_type == "session-secret":
        if not write_files:
            raise ValueError("session secret rotation requires an owner-only output file")
        path = out_dir / SESSION_SECRET_FILENAME
        _prepare_output(out_dir, path, write_files=write_files)
        secret = _b64url(secrets.token_bytes(32))
        require_permission_applied(
            write_secret_text(
                path,
                "JARVIS_ADMIN_SESSION_SECRET_B64URL=" + json.dumps(secret) + "\n",
            ),
            label="session secret file",
        )
        secret = ""
        return RotationResult(
            key_type,
            files={"session_secret_env": path},
            steps=[
                "Import the private session-secret.env value into the managed "
                "secret store, remove the fragment, and restart the service.",
                "All active admin sessions and device-action grants are "
                "invalidated on restart; admins simply log in again.",
            ],
        )

    if key_type == "activation-pepper":
        path = out_dir / "activation.pepper"
        _prepare_output(out_dir, path, write_files=write_files)
        if write_files:
            require_permission_applied(
                write_secret_bytes(path, secrets.token_bytes(32)),
                label="activation pepper file",
            )
        return RotationResult(
            key_type,
            files={"activation_pepper": path},
            steps=[
                "Point JARVIS_ACTIVATION_PEPPER_FILE at the new activation.pepper "
                "during a maintenance window and restart the service.",
            ],
            notes=[
                "The pepper hashes one-time activation credentials. Already "
                "activated devices keep working because their cached signed "
                "certificate is entitlement-key signed, not pepper dependent. "
                "Any unused/pending activation credential must be re-issued.",
            ],
        )

    if key_type == "entitlement-key":
        new_id = _validated_key_id(key_id or "entitlement-key-002")
        key = Ed25519PrivateKey.generate()
        path = out_dir / f"{new_id}.key"
        _prepare_output(out_dir, path, write_files=write_files)
        if write_files:
            require_permission_applied(
                write_secret_bytes(path, _raw_private(key)),
                label="entitlement private-key file",
            )
        return RotationResult(
            key_type,
            env_updates={
                "JARVIS_ENTITLEMENT_KEY_ID": new_id,
                "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE": str(path),
            },
            files={"entitlement_key": path},
            public_values={new_id: _b64url(_raw_public(key))},
            steps=[
                "Add the new entitlement PUBLIC key + id to the client "
                "product.json ALONGSIDE the current one (overlap window).",
                "Ship that client update so installed apps trust both key ids.",
                "Only after clients trust both, set JARVIS_ENTITLEMENT_KEY_ID and "
                "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE to the new key and restart.",
                "Retire the old public key from the client only after no valid "
                "cached certificate depends on it.",
            ],
            notes=[
                "Certificates already cached on devices were signed by the old "
                "key and keep verifying while the client still pins it. Never "
                "reuse an entitlement key id for different key material.",
            ],
        )

    if key_type == "release-key":
        new_id = _validated_key_id(key_id or "release-key-002")
        key = Ed25519PrivateKey.generate()
        path = out_dir / f"{new_id}.key"
        _prepare_output(out_dir, path, write_files=write_files)
        if write_files:
            require_permission_applied(
                write_secret_bytes(path, _raw_private(key)),
                label="release private-key file",
            )
        return RotationResult(
            key_type,
            files={"release_signing_key": path},
            public_values={new_id: _b64url(_raw_public(key))},
            steps=[
                "Add the new release PUBLIC key + id to "
                "JARVIS_RELEASE_PUBLIC_KEYS_JSON alongside the existing ids "
                "(the runtime trusts up to 16).",
                "Sign new artifacts with the new key; keep old-signed artifacts "
                "verifiable by leaving the old public key in place.",
                "Move the new private key to the offline signer; never keep it "
                "on the API host. Retire the old id only when no served artifact "
                "is signed with it.",
            ],
            notes=[
                "Never replace an existing key id with different material; add a "
                "new id and overlap.",
            ],
        )

    raise ValueError(f"unknown key type: {key_type}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate a JARVIS backend key.")
    parser.add_argument("key_type", choices=KEY_TYPES)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--key-id", default=None)
    args = parser.parse_args(argv)

    try:
        result = rotate(args.key_type, args.out_dir, key_id=args.key_id)
    except OpsNotAvailableError:
        if args.key_type == "mfa-key":
            eprint(
                "[not_available] admin MFA master-key rotation requires an "
                "authenticated transactional re-encryption workflow"
            )
        else:
            eprint(
                "[not_available] native owner-only rotation output is not "
                "implemented on this platform"
            )
        return 1
    except Exception:  # noqa: BLE001 - private exception values must not reach logs
        eprint("[fail] rotation material was not created; no secret value was printed")
        return 1
    emit(f"[rotate] {result.key_type}")
    for name in result.env_updates:
        emit(f"  env-key-to-update: {name} (value intentionally not printed)")
    for label, path in result.files.items():
        emit(f"  private-file: {label} -> {path}")
    for public_key_id, public in result.public_values.items():
        emit(f"  public-key: {public_key_id} = {public}")
    emit("  steps:")
    for index, step in enumerate(result.steps, start=1):
        emit(f"    {index}. {step}")
    for note in result.notes:
        eprint(f"[note] {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

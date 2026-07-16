"""Generate new key material and print the safe rotation procedure.

This tool does the mechanical, error-prone part of a rotation (generating fresh
material) and prints the exact cutover steps and honest side effects.  It never
silently replaces live material: the operator applies the new values through the
managed secret store and restarts the service.

Supported key types::

    session-secret      rotate the admin session HMAC secret
    mfa-key             rotate the admin MFA master key (forces MFA re-enrolment)
    activation-pepper   rotate the activation pepper (invalidates pending codes)
    entitlement-key     rotate the entitlement signing key (overlap required)
    release-key         rotate the release signing key (overlap required)

Usage::

    python -m ops.rotate entitlement-key --out-dir /etc/jarvis/rotate --key-id v2
"""

from __future__ import annotations

import argparse
import base64
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._common import emit, eprint, write_secret_bytes

KEY_TYPES = (
    "session-secret",
    "mfa-key",
    "activation-pepper",
    "entitlement-key",
    "release-key",
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


@dataclass(frozen=True, slots=True)
class RotationResult:
    key_type: str
    env_updates: dict[str, str] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)
    public_values: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def rotate(
    key_type: str,
    out_dir: Path,
    *,
    key_id: str | None = None,
    write_files: bool = True,
) -> RotationResult:
    out_dir = Path(out_dir)
    if key_type == "session-secret":
        secret = _b64url(secrets.token_bytes(32))
        return RotationResult(
            key_type,
            env_updates={"JARVIS_ADMIN_SESSION_SECRET_B64URL": secret},
            steps=[
                "Store the new JARVIS_ADMIN_SESSION_SECRET_B64URL in the secret "
                "store and restart the service.",
                "All active admin sessions and device-action grants are "
                "invalidated on restart; admins simply log in again.",
            ],
        )

    if key_type == "mfa-key":
        path = out_dir / "admin-mfa.key"
        if write_files:
            write_secret_bytes(path, secrets.token_bytes(32))
        return RotationResult(
            key_type,
            files={"admin_mfa_key": path},
            steps=[
                "Point JARVIS_ADMIN_MFA_KEY_FILE at the new admin-mfa.key and "
                "restart the service.",
            ],
            notes=[
                "The MFA master key encrypts stored TOTP secrets and recovery "
                "codes. After rotation every admin MUST re-enrol their second "
                "factor; existing authenticator entries stop verifying.",
            ],
        )

    if key_type == "activation-pepper":
        path = out_dir / "activation.pepper"
        if write_files:
            write_secret_bytes(path, secrets.token_bytes(32))
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
        new_id = key_id or "entitlement-key-002"
        key = Ed25519PrivateKey.generate()
        path = out_dir / f"{new_id}.key"
        if write_files:
            write_secret_bytes(path, _raw_private(key))
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
        new_id = key_id or "release-key-002"
        key = Ed25519PrivateKey.generate()
        path = out_dir / f"{new_id}.key"
        if write_files:
            write_secret_bytes(path, _raw_private(key))
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
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--key-id", default=None)
    args = parser.parse_args(argv)

    result = rotate(args.key_type, args.out_dir, key_id=args.key_id)
    emit(f"[rotate] {result.key_type}")
    for name, value in result.env_updates.items():
        emit(f"  env: {name}={value}")
    for label, path in result.files.items():
        emit(f"  file: {label} -> {path}")
    for key_id, public in result.public_values.items():
        emit(f"  public-key: {key_id} = {public}")
    emit("  steps:")
    for index, step in enumerate(result.steps, start=1):
        emit(f"    {index}. {step}")
    for note in result.notes:
        eprint(f"[note] {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

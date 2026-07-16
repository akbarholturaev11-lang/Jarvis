"""Fail-closed environment assembly for a deployable backend ASGI factory.

Private signing material is read only from explicitly configured, owner-only
files.  The module has no import-time app or filesystem side effects; ASGI
servers should use ``--factory product_backend.runtime:create_app_from_environment``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .admin_mfa import (
    AdminMfaSettings,
    MfaSecretCipher,
    SQLiteAdminMfaManager,
)
from .admin_credentials import SQLiteAdminCredentialStore
from .api_activation import SQLiteClientActivationService
from .api_app import create_product_backend_app
from .api_artifact_storage import LocalReadOnlyReleaseArtifactStore
from .api_auth import (
    AdminAuthSettings,
    AdminIpAllowlist,
    BackendConfigurationError,
    TrustedProxyConfig,
)
from .api_operational import OperationalPolicy
from .api_queries import SQLiteProductReadStore
from .observability import (
    InMemoryMetrics,
    MetricsRegistry,
    NullMetrics,
    configure_json_logging,
)
from .api_signing import InjectedEd25519EntitlementSigner
from .device_challenges import SQLiteDeviceChallengeService
from .private_storage import LocalPrivateObjectStore
from .payment_instructions import load_payment_instructions
from .release_verifier import PinnedReleaseArtifactVerifier
from .sqlite_repository import SQLiteCommerceRepository


_MAX_PUBLIC_KEYS_JSON_BYTES: Final = 64 * 1024
_REQUIRED_RUNTIME_ENV: Final = (
    "JARVIS_BACKEND_DATA_DIR",
    "JARVIS_RELEASE_ARTIFACT_ROOT",
    "JARVIS_RELEASE_PUBLIC_KEYS_JSON",
    "JARVIS_ENTITLEMENT_KEY_ID",
    "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE",
    "JARVIS_ACTIVATION_PEPPER_FILE",
    "JARVIS_ADMIN_MFA_KEY_FILE",
    "JARVIS_REQUIRE_HTTPS",
)
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def _admin_mfa_settings(source: Mapping[str, str]) -> AdminMfaSettings:
    """Mandatory MFA by default; a password-only bypass is explicit opt-in."""

    allow_password_only = (
        source.get("JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY", "").strip().lower()
        in _TRUTHY
    )
    issuer = source.get("JARVIS_ADMIN_MFA_ISSUER", "").strip() or "JARVIS Admin"
    return AdminMfaSettings(
        issuer=issuer,
        mandatory=not allow_password_only,
        allow_password_only=allow_password_only,
    )


def create_app_from_environment(
    environ: Mapping[str, str] | None = None,
):
    """Assemble the FastAPI app from explicit paths and security material."""

    source = os.environ if environ is None else environ
    if any(not source.get(name) for name in _REQUIRED_RUNTIME_ENV):
        raise BackendConfigurationError(
            "required backend runtime configuration is missing"
        )
    admin_settings = AdminAuthSettings.from_env(source)
    operational_policy = OperationalPolicy.from_env(
        source,
        allowed_hosts=admin_settings.allowed_hosts,
    )
    if not operational_policy.require_https:
        raise BackendConfigurationError(
            "JARVIS_REQUIRE_HTTPS must be enabled for the deployable runtime"
        )
    data_dir = _ensure_private_directory(source["JARVIS_BACKEND_DATA_DIR"])
    artifact_root = _existing_private_directory(
        source["JARVIS_RELEASE_ARTIFACT_ROOT"]
    )
    release_keys = _public_key_map(source["JARVIS_RELEASE_PUBLIC_KEYS_JSON"])
    entitlement_private_key = _read_private_file(
        source["JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE"],
        minimum_bytes=32,
        maximum_bytes=32,
    )
    activation_pepper = _read_private_file(
        source["JARVIS_ACTIVATION_PEPPER_FILE"],
        minimum_bytes=32,
        maximum_bytes=128,
    )
    mfa_master_key = _read_private_file(
        source["JARVIS_ADMIN_MFA_KEY_FILE"],
        minimum_bytes=32,
        maximum_bytes=128,
    )
    mfa_settings = _admin_mfa_settings(source)
    trusted_proxy = TrustedProxyConfig.from_spec(
        source.get("JARVIS_TRUSTED_PROXIES")
    )
    admin_ip_allowlist = AdminIpAllowlist.from_spec(
        source.get("JARVIS_ADMIN_ALLOWED_NETWORKS")
    )
    request_logger = configure_json_logging(name="jarvis.backend.access")
    metrics: MetricsRegistry = (
        InMemoryMetrics() if operational_policy.metrics_enabled else NullMetrics()
    )
    mfa_cipher = MfaSecretCipher(mfa_master_key)
    clock = lambda: datetime.now(timezone.utc)
    commerce_path = data_dir / "commerce.sqlite3"
    commerce = SQLiteCommerceRepository(
        commerce_path,
        artifact_verifier=PinnedReleaseArtifactVerifier(
            release_keys,
            clock=clock,
        ),
        clock=clock,
    )
    challenges = None
    activation = None
    mfa = None
    credential_store = None
    try:
        credential_store = SQLiteAdminCredentialStore(
            data_dir / "admin-credentials.sqlite3",
            admin_settings.credentials,
            clock=clock,
        )
        challenges = SQLiteDeviceChallengeService(
            commerce,
            data_dir / "device-challenges.sqlite3",
            clock=clock,
        )
        signer = InjectedEd25519EntitlementSigner(
            entitlement_private_key,
            key_id=source["JARVIS_ENTITLEMENT_KEY_ID"],
        )
        activation = SQLiteClientActivationService(
            commerce,
            signer,
            activation_pepper,
            data_dir / "activation.sqlite3",
            clock=clock,
        )
        mfa = SQLiteAdminMfaManager(
            mfa_cipher,
            data_dir / "admin-mfa.sqlite3",
            settings=mfa_settings,
            clock=clock,
        )
        evidence = LocalPrivateObjectStore(data_dir / "payment-evidence").ensure()
        reads = SQLiteProductReadStore(commerce_path)
        artifact_store = LocalReadOnlyReleaseArtifactStore(artifact_root)
        payment_instructions = load_payment_instructions(
            source.get("JARVIS_PAYMENT_INSTRUCTIONS_FILE")
        )
        app = create_product_backend_app(
            commerce=commerce,
            reads=reads,
            evidence_store=evidence,
            challenges=challenges,
            activation=activation,
            release_artifact_store=artifact_store,
            auth_settings=admin_settings,
            payment_instructions=payment_instructions,
            mfa=mfa,
            trusted_proxy=trusted_proxy,
            admin_ip_allowlist=admin_ip_allowlist,
            admin_credential_store=credential_store,
            operational_policy=operational_policy,
            metrics=metrics,
            request_logger=request_logger,
            clock=clock,
        )
    except BaseException:
        if mfa is not None:
            mfa.close()
        if credential_store is not None:
            credential_store.close()
        if activation is not None:
            activation.close()
        if challenges is not None:
            challenges.close()
        commerce.close()
        raise

    closed = False

    def close_backend_resources() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        mfa.close()
        credential_store.close()
        activation.close()
        challenges.close()
        commerce.close()

    app.state.close_backend_resources = close_backend_resources
    app.router.add_event_handler("shutdown", close_backend_resources)
    return app


def _ensure_private_directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path(os.sep) or path.is_symlink():
        raise BackendConfigurationError("backend data directory is invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise BackendConfigurationError(
            "backend data directory is unavailable"
        ) from exc
    return _validate_private_directory(path)


def _existing_private_directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path(os.sep) or path.is_symlink():
        raise BackendConfigurationError("artifact directory is invalid")
    return _validate_private_directory(path)


def _validate_private_directory(path: Path) -> Path:
    try:
        opened = path.stat()
    except OSError as exc:
        raise BackendConfigurationError("private directory is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_mode & 0o077
        or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
    ):
        raise BackendConfigurationError("private directory permissions are invalid")
    return path.resolve(strict=True)


def _read_private_file(
    value: str,
    *,
    minimum_bytes: int,
    maximum_bytes: int,
) -> bytes:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise BackendConfigurationError("private key material path is invalid")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_mode & 0o077
            or not minimum_bytes <= opened.st_size <= maximum_bytes
            or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
        ):
            raise BackendConfigurationError(
                "private key material permissions are invalid"
            )
        raw = os.read(descriptor, maximum_bytes + 1)
        if len(raw) != opened.st_size or os.read(descriptor, 1):
            raise BackendConfigurationError("private key material is invalid")
        return raw
    except BackendConfigurationError:
        raise
    except OSError as exc:
        raise BackendConfigurationError("private key material is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _public_key_map(value: str) -> dict[str, bytes]:
    if not 1 <= len(value.encode("utf-8")) <= _MAX_PUBLIC_KEYS_JSON_BYTES:
        raise BackendConfigurationError("release public keys are invalid")
    try:
        document = json.loads(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackendConfigurationError("release public keys are invalid") from exc
    if type(document) is not dict or not 1 <= len(document) <= 16:
        raise BackendConfigurationError("release public keys are invalid")
    result: dict[str, bytes] = {}
    for key_id, encoded in document.items():
        if type(key_id) is not str or type(encoded) is not str or len(encoded) != 43:
            raise BackendConfigurationError("release public key is invalid")
        try:
            raw = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BackendConfigurationError("release public key is invalid") from exc
        if (
            len(raw) != 32
            or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded
        ):
            raise BackendConfigurationError("release public key is invalid")
        result[key_id] = raw
    return result


__all__ = ["create_app_from_environment"]

"""Injected Ed25519 entitlement certificate signer.

This module accepts already-loaded private material.  It deliberately performs
no filesystem, environment, keychain, or network secret loading.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from core.entitlement_certificate import (
    CERTIFICATE_SCHEMA,
    ENVELOPE_SCHEMA,
    SCHEMA_VERSION,
)
from core.product_version import BUNDLE_ID, PRODUCT_ID

from .api_auth import BackendConfigurationError
from .models import (
    normalize_semver,
    normalize_utc_timestamp,
    validate_device_key_fingerprint,
    validate_opaque_identifier,
)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover - availability is environment-specific
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    _ED25519_AVAILABLE = False


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class InjectedEd25519EntitlementSigner:
    """Canonical entitlement signer with redacted private material."""

    __slots__ = ("__private_key", "_key_id")

    def __init__(self, private_key: object, *, key_id: str) -> None:
        if not _ED25519_AVAILABLE or Ed25519PrivateKey is None:
            raise BackendConfigurationError("Ed25519 signing is unavailable")
        self._key_id = validate_opaque_identifier(key_id, field="key_id")
        try:
            if type(private_key) is bytes:
                if len(private_key) != 32:
                    raise ValueError("private key length")
                loaded: Any = Ed25519PrivateKey.from_private_bytes(private_key)
            elif isinstance(private_key, Ed25519PrivateKey):
                loaded = private_key
            else:
                raise TypeError("private key type")
        except (TypeError, ValueError) as exc:
            raise BackendConfigurationError(
                "entitlement signing key is invalid"
            ) from exc
        self.__private_key = loaded

    @property
    def key_id(self) -> str:
        return self._key_id

    def __repr__(self) -> str:
        return (
            "InjectedEd25519EntitlementSigner("
            f"key_id={self._key_id!r}, private_key=<redacted>)"
        )

    def sign_entitlement_certificate(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        version: str,
        issued_at: str,
    ) -> str:
        license_id = validate_opaque_identifier(license_id, field="license_id")
        fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
        version = normalize_semver(version)
        issued_at = normalize_utc_timestamp(issued_at, field="issued_at")
        payload = _canonical_json(
            {
                "schema": CERTIFICATE_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "product_id": PRODUCT_ID,
                "bundle_id": BUNDLE_ID,
                "license_id": license_id,
                "device_key_fingerprint": fingerprint,
                "version": version,
                "issued_at": issued_at,
                "key_id": self._key_id,
            }
        )
        signature = self.__private_key.sign(payload)
        if type(signature) is not bytes or len(signature) != 64:
            raise BackendConfigurationError(
                "entitlement signer returned an invalid signature"
            )
        return _canonical_json(
            {
                "schema": ENVELOPE_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "payload": _base64url(payload),
                "signature": _base64url(signature),
            }
        ).decode("utf-8")


__all__ = ["InjectedEd25519EntitlementSigner"]

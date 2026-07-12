"""Offline verification for exact-version JARVIS entitlement certificates.

Certificates use a canonical JSON payload and a detached Ed25519 signature.  The
payload and signature are carried as unpadded base64url strings in a small JSON
envelope.  This module verifies certificates only; private-key loading and
signing deliberately do not exist here.

Messages returned by this module are sanitized internal diagnostics.  A future
UI must map statuses through the shared bilingual localization layer.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from core.product_version import BUNDLE_ID, PRODUCT_ID, SemanticVersion

try:
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as _Ed25519PublicKey,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through the availability flag
    _InvalidSignature = ValueError  # type: ignore[assignment,misc]
    _Ed25519PublicKey = None  # type: ignore[assignment,misc]
    _ED25519_AVAILABLE = False


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID: Final = "invalid"
STATUS_NOT_AVAILABLE: Final = "not_available"

ENVELOPE_SCHEMA: Final = "jarvis.entitlement.envelope"
CERTIFICATE_SCHEMA: Final = "jarvis.entitlement.certificate"
SCHEMA_VERSION: Final = 1

_VALID_STATUSES: Final = frozenset(
    {STATUS_SUCCESS, STATUS_NOT_FOUND, STATUS_INVALID, STATUS_NOT_AVAILABLE}
)
_ENVELOPE_FIELDS: Final = frozenset(
    {"schema", "schema_version", "payload", "signature"}
)
_CERTIFICATE_FIELDS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "product_id",
        "bundle_id",
        "license_id",
        "device_key_fingerprint",
        "version",
        "issued_at",
        "key_id",
    }
)

_MAX_ENVELOPE_BYTES: Final = 32 * 1024
_MAX_PAYLOAD_BYTES: Final = 8 * 1024
_ED25519_PUBLIC_KEY_BYTES: Final = 32
_ED25519_SIGNATURE_BYTES: Final = 64

_NORMALIZED_IDENTIFIER_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])"
)
_DEVICE_FINGERPRINT_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_BASE64URL_RE: Final = re.compile(r"[A-Za-z0-9_-]+")
_UTC_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)

_MESSAGE_SUCCESS: Final = "Entitlement certificate verified."
_MESSAGE_NOT_FOUND: Final = "Entitlement certificate was not provided."
_MESSAGE_INVALID: Final = "Entitlement certificate is invalid."
_MESSAGE_NOT_AVAILABLE: Final = "Entitlement verification is not available."


class _CertificateInvalid(ValueError):
    """Internal marker for all untrusted-certificate validation failures."""


@dataclass(frozen=True, slots=True)
class EntitlementCertificate:
    """Verified, non-secret claims from a signed entitlement certificate."""

    schema: str
    schema_version: int
    product_id: str
    bundle_id: str
    license_id: str = field(repr=False)
    device_key_fingerprint: str = field(repr=False)
    version: SemanticVersion
    issued_at: str
    key_id: str


@dataclass(frozen=True, slots=True)
class EntitlementVerificationResult:
    """Sanitized result that never retains raw certificate or signature data."""

    status: str
    message: str = field(repr=False)
    certificate: EntitlementCertificate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported verification status")
        if (self.status == STATUS_SUCCESS) != (self.certificate is not None):
            raise ValueError("only successful verification may carry claims")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.certificate is not None

    def __repr__(self) -> str:
        claims = "verified" if self.certificate is not None else "none"
        return (
            "EntitlementVerificationResult("
            f"status={self.status!r}, claims={claims!r})"
        )

    __str__ = __repr__


def _result(
    status: str,
    message: str,
    certificate: EntitlementCertificate | None = None,
) -> EntitlementVerificationResult:
    return EntitlementVerificationResult(status, message, certificate)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _CertificateInvalid("duplicate JSON key")
        document[key] = value
    return document


def _reject_non_finite(_value: str) -> None:
    raise _CertificateInvalid("non-finite JSON number")


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        _CertificateInvalid,
    ) as exc:
        raise _CertificateInvalid("invalid JSON") from exc
    if type(document) is not dict:
        raise _CertificateInvalid("JSON root must be an object")
    return document


def _require_exact_fields(document: Mapping[str, Any], expected: frozenset[str]) -> None:
    if frozenset(document) != expected:
        raise _CertificateInvalid("unexpected JSON fields")


def _require_schema(
    document: Mapping[str, Any],
    *,
    expected_schema: str,
) -> None:
    if type(document.get("schema")) is not str:
        raise _CertificateInvalid("schema must be a string")
    if document["schema"] != expected_schema:
        raise _CertificateInvalid("unknown schema")
    if type(document.get("schema_version")) is not int:
        raise _CertificateInvalid("schema_version must be an integer")
    if document["schema_version"] != SCHEMA_VERSION:
        raise _CertificateInvalid("unknown schema version")


def _canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _CertificateInvalid("payload is not canonicalizable") from exc


def _decode_base64url(value: object, *, maximum_bytes: int) -> bytes:
    if type(value) is not str or _BASE64URL_RE.fullmatch(value) is None:
        raise _CertificateInvalid("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise _CertificateInvalid("invalid base64url value") from exc
    encoded = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if encoded != value or len(decoded) > maximum_bytes:
        raise _CertificateInvalid("non-canonical base64url value")
    return decoded


def _validate_normalized_identifier(value: object) -> str:
    if type(value) is not str or _NORMALIZED_IDENTIFIER_RE.fullmatch(value) is None:
        raise _CertificateInvalid("invalid normalized identifier")
    return value


def _validate_device_fingerprint(value: object) -> str:
    if type(value) is not str or _DEVICE_FINGERPRINT_RE.fullmatch(value) is None:
        raise _CertificateInvalid("invalid device fingerprint")
    return value


def _validate_issued_at(value: object) -> str:
    if type(value) is not str or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise _CertificateInvalid("issued_at must be normalized UTC ISO-8601")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _CertificateInvalid("issued_at is not a real timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise _CertificateInvalid("issued_at must be UTC")
    return value


def _parse_certificate_payload(payload_bytes: bytes) -> EntitlementCertificate:
    payload = _parse_json_object(payload_bytes)
    _require_exact_fields(payload, _CERTIFICATE_FIELDS)
    _require_schema(payload, expected_schema=CERTIFICATE_SCHEMA)
    if _canonical_json_bytes(payload) != payload_bytes:
        raise _CertificateInvalid("payload JSON is not canonical")

    if payload.get("product_id") != PRODUCT_ID:
        raise _CertificateInvalid("wrong product")
    if payload.get("bundle_id") != BUNDLE_ID:
        raise _CertificateInvalid("wrong bundle")

    license_id = _validate_normalized_identifier(payload.get("license_id"))
    fingerprint = _validate_device_fingerprint(payload.get("device_key_fingerprint"))
    key_id = _validate_normalized_identifier(payload.get("key_id"))
    issued_at = _validate_issued_at(payload.get("issued_at"))
    if type(payload.get("version")) is not str:
        raise _CertificateInvalid("version must be a string")
    try:
        version = SemanticVersion.parse(payload["version"])
    except (TypeError, ValueError) as exc:
        raise _CertificateInvalid("version must be strict semantic version") from exc

    return EntitlementCertificate(
        schema=CERTIFICATE_SCHEMA,
        schema_version=SCHEMA_VERSION,
        product_id=PRODUCT_ID,
        bundle_id=BUNDLE_ID,
        license_id=license_id,
        device_key_fingerprint=fingerprint,
        version=version,
        issued_at=issued_at,
        key_id=key_id,
    )


def _expected_version(value: object) -> SemanticVersion:
    if isinstance(value, SemanticVersion):
        return value
    if type(value) is str:
        try:
            return SemanticVersion.parse(value)
        except (TypeError, ValueError) as exc:
            raise _CertificateInvalid("invalid expected version") from exc
    raise _CertificateInvalid("invalid expected version")


def _load_trusted_public_key(value: object) -> Any:
    if not _ED25519_AVAILABLE or _Ed25519PublicKey is None:
        raise RuntimeError("Ed25519 verification unavailable")
    if isinstance(value, _Ed25519PublicKey):
        return value
    if type(value) is bytes and len(value) == _ED25519_PUBLIC_KEY_BYTES:
        try:
            return _Ed25519PublicKey.from_public_bytes(value)
        except ValueError as exc:
            raise RuntimeError("invalid trusted public key") from exc
    raise RuntimeError("invalid trusted public key")


def verify_entitlement_certificate(
    certificate_input: str | bytes | None,
    *,
    trusted_public_keys: Mapping[str, object],
    expected_license_id: str,
    expected_device_fingerprint: str,
    expected_version: str | SemanticVersion,
) -> EntitlementVerificationResult:
    """Verify an offline certificate for one device and exact semantic version.

    ``trusted_public_keys`` maps normalized key IDs to either Ed25519 public-key
    objects or their raw 32-byte public representation.  No network, clock-based
    expiry, or revocation check is performed.
    """

    if certificate_input is None:
        return _result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
    if not _ED25519_AVAILABLE or _Ed25519PublicKey is None:
        return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
    if not isinstance(trusted_public_keys, Mapping) or not trusted_public_keys:
        return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)

    try:
        fingerprint = _validate_device_fingerprint(expected_device_fingerprint)
        license_id = _validate_normalized_identifier(expected_license_id)
        version = _expected_version(expected_version)
        if type(certificate_input) is str:
            # Bound characters before allocating a potentially much larger
            # UTF-8 byte string; the exact byte limit is checked immediately
            # after encoding as well.
            if len(certificate_input) > _MAX_ENVELOPE_BYTES:
                raise _CertificateInvalid("certificate envelope size is invalid")
            envelope_bytes = certificate_input.encode("utf-8", errors="strict")
        elif type(certificate_input) is bytes:
            envelope_bytes = certificate_input
        else:
            raise _CertificateInvalid("certificate input must be text or bytes")
        if not envelope_bytes or len(envelope_bytes) > _MAX_ENVELOPE_BYTES:
            raise _CertificateInvalid("certificate envelope size is invalid")

        envelope = _parse_json_object(envelope_bytes)
        _require_exact_fields(envelope, _ENVELOPE_FIELDS)
        _require_schema(envelope, expected_schema=ENVELOPE_SCHEMA)
        payload_bytes = _decode_base64url(
            envelope.get("payload"),
            maximum_bytes=_MAX_PAYLOAD_BYTES,
        )
        signature = _decode_base64url(
            envelope.get("signature"),
            maximum_bytes=_ED25519_SIGNATURE_BYTES,
        )
        if len(signature) != _ED25519_SIGNATURE_BYTES:
            raise _CertificateInvalid("wrong Ed25519 signature length")

        claims = _parse_certificate_payload(payload_bytes)
        if claims.key_id not in trusted_public_keys:
            raise _CertificateInvalid("certificate key is not trusted")
        try:
            public_key = _load_trusted_public_key(trusted_public_keys[claims.key_id])
        except RuntimeError:
            return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        try:
            public_key.verify(signature, payload_bytes)
        except (_InvalidSignature, ValueError, TypeError) as exc:
            raise _CertificateInvalid("signature verification failed") from exc

        if claims.device_key_fingerprint != fingerprint:
            raise _CertificateInvalid("certificate belongs to another device")
        if claims.license_id != license_id:
            raise _CertificateInvalid("certificate belongs to another license")
        if claims.version != version:
            raise _CertificateInvalid("certificate is for another version")
    except (
        _CertificateInvalid,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
    ):
        return _result(STATUS_INVALID, _MESSAGE_INVALID)

    return _result(STATUS_SUCCESS, _MESSAGE_SUCCESS, claims)

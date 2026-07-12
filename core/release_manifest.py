"""Ed25519 verification for canonical JARVIS release manifests.

The verifier supports initial installers and update packages.  It validates a
canonical JSON payload carried in a detached-signature JSON envelope.  This
module contains no private-key loading or signing implementation.

Verified metadata does not by itself prove downloaded artifact bytes.  A caller
must still compare the real download length and SHA-256 digest with the verified
claims before installation.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    UNKNOWN_TARGET,
    ProductVersion,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)

try:
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as _Ed25519PublicKey,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover - availability is tested via the flag
    _InvalidSignature = ValueError  # type: ignore[assignment,misc]
    _Ed25519PublicKey = None  # type: ignore[assignment,misc]
    _ED25519_AVAILABLE = False


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID: Final = "invalid"
STATUS_NOT_AVAILABLE: Final = "not_available"

ENVELOPE_SCHEMA: Final = "jarvis.release-manifest.envelope"
MANIFEST_SCHEMA: Final = "jarvis.release-manifest"
SCHEMA_VERSION: Final = 1

MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024 * 1024
MAX_COMPATIBLE_SOURCE_VERSIONS: Final = 128

_MAX_BUILD_NUMBER: Final = (2**63) - 1
_MAX_ENVELOPE_BYTES: Final = 64 * 1024
_MAX_PAYLOAD_BYTES: Final = 32 * 1024
_MAX_STORAGE_KEY_LENGTH: Final = 512
_ED25519_PUBLIC_KEY_BYTES: Final = 32
_ED25519_SIGNATURE_BYTES: Final = 64

_VALID_STATUSES: Final = frozenset(
    {STATUS_SUCCESS, STATUS_NOT_FOUND, STATUS_INVALID, STATUS_NOT_AVAILABLE}
)
_ENVELOPE_FIELDS: Final = frozenset(
    {"schema", "schema_version", "payload", "signature"}
)
_MANIFEST_FIELDS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "product_id",
        "bundle_id",
        "version",
        "build",
        "platform",
        "architecture",
        "artifact_kind",
        "sha256",
        "byte_size",
        "storage_key",
        "signing_key_id",
        "compatible_source_versions",
    }
)

_BASE64URL_RE: Final = re.compile(r"[A-Za-z0-9_-]+")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_NORMALIZED_IDENTIFIER_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])"
)
_STORAGE_KEY_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")

_MESSAGE_SUCCESS: Final = "Release manifest verified."
_MESSAGE_NOT_FOUND: Final = "Release manifest was not provided."
_MESSAGE_INVALID: Final = "Release manifest is invalid."
_MESSAGE_NOT_AVAILABLE: Final = "Release verification is not available."


class ArtifactKind(StrEnum):
    INITIAL_INSTALLER = "initial_installer"
    UPDATE_PACKAGE = "update_package"


class _ManifestInvalid(ValueError):
    """Internal marker for all untrusted-manifest validation failures."""


@dataclass(frozen=True, slots=True)
class VerifiedReleaseManifest:
    """Canonical verified claims for backend and updater integration."""

    schema: str
    schema_version: int
    product_id: str
    bundle_id: str
    version: SemanticVersion
    build: int
    platform: str
    architecture: str
    artifact_kind: ArtifactKind
    sha256: str
    byte_size: int
    storage_key: str = field(repr=False)
    signing_key_id: str
    compatible_source_versions: tuple[SemanticVersion, ...]

    @property
    def product_version(self) -> ProductVersion:
        return ProductVersion(self.version, self.build)

    def canonical_payload_bytes(self) -> bytes:
        """Reconstruct the exact canonical payload verified by this object."""

        return _canonical_json_bytes(
            {
                "schema": self.schema,
                "schema_version": self.schema_version,
                "product_id": self.product_id,
                "bundle_id": self.bundle_id,
                "version": str(self.version),
                "build": self.build,
                "platform": self.platform,
                "architecture": self.architecture,
                "artifact_kind": self.artifact_kind.value,
                "sha256": self.sha256,
                "byte_size": self.byte_size,
                "storage_key": self.storage_key,
                "signing_key_id": self.signing_key_id,
                "compatible_source_versions": [
                    str(version) for version in self.compatible_source_versions
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class ReleaseManifestVerificationResult:
    """Sanitized result that retains no raw manifest or signature."""

    status: str
    message: str = field(repr=False)
    claims: VerifiedReleaseManifest | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported release verification status")
        if (self.status == STATUS_SUCCESS) != (self.claims is not None):
            raise ValueError("only success may carry verified release claims")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.claims is not None

    def __repr__(self) -> str:
        claims = "verified" if self.claims is not None else "none"
        return (
            "ReleaseManifestVerificationResult("
            f"status={self.status!r}, claims={claims!r})"
        )

    __str__ = __repr__


def _result(
    status: str,
    message: str,
    claims: VerifiedReleaseManifest | None = None,
) -> ReleaseManifestVerificationResult:
    return ReleaseManifestVerificationResult(status, message, claims)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _ManifestInvalid("duplicate JSON key")
        document[key] = value
    return document


def _reject_non_finite(_value: str) -> None:
    raise _ManifestInvalid("non-finite JSON number")


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        _ManifestInvalid,
    ) as exc:
        raise _ManifestInvalid("invalid JSON") from exc
    if type(document) is not dict:
        raise _ManifestInvalid("JSON root must be an object")
    return document


def _require_exact_fields(document: Mapping[str, Any], expected: frozenset[str]) -> None:
    if frozenset(document) != expected:
        raise _ManifestInvalid("unexpected JSON fields")


def _require_schema(
    document: Mapping[str, Any],
    *,
    expected_schema: str,
) -> None:
    if type(document.get("schema")) is not str:
        raise _ManifestInvalid("schema must be a string")
    if document["schema"] != expected_schema:
        raise _ManifestInvalid("unknown schema")
    if type(document.get("schema_version")) is not int:
        raise _ManifestInvalid("schema_version must be an integer")
    if document["schema_version"] != SCHEMA_VERSION:
        raise _ManifestInvalid("unknown schema version")


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
        raise _ManifestInvalid("manifest is not canonicalizable") from exc


def _decode_base64url(value: object, *, maximum_bytes: int) -> bytes:
    if type(value) is not str or _BASE64URL_RE.fullmatch(value) is None:
        raise _ManifestInvalid("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise _ManifestInvalid("invalid base64url value") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value or len(decoded) > maximum_bytes:
        raise _ManifestInvalid("non-canonical base64url value")
    return decoded


def _strict_semver(value: object) -> SemanticVersion:
    if type(value) is not str:
        raise _ManifestInvalid("version must be a string")
    try:
        return SemanticVersion.parse(value)
    except (TypeError, ValueError) as exc:
        raise _ManifestInvalid("version must be strict semantic version") from exc


def _positive_build(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_BUILD_NUMBER:
        raise _ManifestInvalid("build must be a positive bounded integer")
    return value


def _artifact_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ARTIFACT_BYTES:
        raise _ManifestInvalid("byte_size must be a positive bounded integer")
    return value


def _normalized_platform(value: object) -> str:
    if type(value) is not str:
        raise _ManifestInvalid("platform must be a string")
    normalized = normalize_platform(value)
    if normalized == UNKNOWN_TARGET or value != normalized:
        raise _ManifestInvalid("platform must be canonical")
    return normalized


def _normalized_architecture(value: object) -> str:
    if type(value) is not str:
        raise _ManifestInvalid("architecture must be a string")
    normalized = normalize_architecture(value)
    if normalized == UNKNOWN_TARGET or value != normalized:
        raise _ManifestInvalid("architecture must be canonical")
    return normalized


def _artifact_kind(value: object) -> ArtifactKind:
    if isinstance(value, ArtifactKind):
        return value
    if type(value) is not str:
        raise _ManifestInvalid("artifact_kind must be a string")
    try:
        return ArtifactKind(value)
    except ValueError as exc:
        raise _ManifestInvalid("unknown artifact_kind") from exc


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _ManifestInvalid("sha256 must be lowercase hexadecimal")
    return value


def _storage_key(value: object) -> str:
    if (
        type(value) is not str
        or not 3 <= len(value) <= _MAX_STORAGE_KEY_LENGTH
        or _STORAGE_KEY_RE.fullmatch(value) is None
        or value.startswith("/")
        or value.endswith("/")
        or "://" in value
    ):
        raise _ManifestInvalid("storage_key must be an opaque private key")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _ManifestInvalid("storage_key must not traverse namespaces")
    return value


def _key_id(value: object) -> str:
    if type(value) is not str or _NORMALIZED_IDENTIFIER_RE.fullmatch(value) is None:
        raise _ManifestInvalid("signing_key_id is not normalized")
    return value


def _compatible_source_versions(
    value: object,
    *,
    target: SemanticVersion,
    kind: ArtifactKind,
) -> tuple[SemanticVersion, ...]:
    if type(value) is not list or len(value) > MAX_COMPATIBLE_SOURCE_VERSIONS:
        raise _ManifestInvalid("compatible_source_versions must be a bounded list")
    versions = tuple(_strict_semver(item) for item in value)
    if len(set(versions)) != len(versions):
        raise _ManifestInvalid("compatible source versions must be unique")
    if versions != tuple(sorted(versions)):
        raise _ManifestInvalid("compatible source versions must be sorted")
    if kind is ArtifactKind.INITIAL_INSTALLER:
        if versions:
            raise _ManifestInvalid("initial installer cannot declare source versions")
    elif not versions or any(version >= target for version in versions):
        raise _ManifestInvalid("update source versions must be older than target")
    return versions


def _parse_manifest_payload(payload_bytes: bytes) -> VerifiedReleaseManifest:
    payload = _parse_json_object(payload_bytes)
    _require_exact_fields(payload, _MANIFEST_FIELDS)
    _require_schema(payload, expected_schema=MANIFEST_SCHEMA)
    if _canonical_json_bytes(payload) != payload_bytes:
        raise _ManifestInvalid("manifest payload is not canonical")
    if payload.get("product_id") != PRODUCT_ID:
        raise _ManifestInvalid("wrong product")
    if payload.get("bundle_id") != BUNDLE_ID:
        raise _ManifestInvalid("wrong bundle")

    version = _strict_semver(payload.get("version"))
    build = _positive_build(payload.get("build"))
    platform = _normalized_platform(payload.get("platform"))
    architecture = _normalized_architecture(payload.get("architecture"))
    kind = _artifact_kind(payload.get("artifact_kind"))
    sha256 = _sha256(payload.get("sha256"))
    byte_size = _artifact_size(payload.get("byte_size"))
    storage_key = _storage_key(payload.get("storage_key"))
    signing_key_id = _key_id(payload.get("signing_key_id"))
    sources = _compatible_source_versions(
        payload.get("compatible_source_versions"),
        target=version,
        kind=kind,
    )
    return VerifiedReleaseManifest(
        schema=MANIFEST_SCHEMA,
        schema_version=SCHEMA_VERSION,
        product_id=PRODUCT_ID,
        bundle_id=BUNDLE_ID,
        version=version,
        build=build,
        platform=platform,
        architecture=architecture,
        artifact_kind=kind,
        sha256=sha256,
        byte_size=byte_size,
        storage_key=storage_key,
        signing_key_id=signing_key_id,
        compatible_source_versions=sources,
    )


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


def _matches_expectations(
    claims: VerifiedReleaseManifest,
    *,
    expected_platform: str | None,
    expected_architecture: str | None,
    expected_version: str | SemanticVersion | None,
    expected_build: int | None,
    expected_artifact_kind: str | ArtifactKind | None,
    expected_sha256: str | None,
    expected_byte_size: int | None,
    expected_storage_key: str | None,
) -> bool:
    if expected_platform is not None and claims.platform != _normalized_platform(
        expected_platform
    ):
        return False
    if expected_architecture is not None and claims.architecture != _normalized_architecture(
        expected_architecture
    ):
        return False
    if expected_version is not None:
        version = (
            expected_version
            if isinstance(expected_version, SemanticVersion)
            else _strict_semver(expected_version)
        )
        if claims.version != version:
            return False
    if expected_build is not None and claims.build != _positive_build(expected_build):
        return False
    if expected_artifact_kind is not None and claims.artifact_kind is not _artifact_kind(
        expected_artifact_kind
    ):
        return False
    if expected_sha256 is not None and claims.sha256 != _sha256(expected_sha256):
        return False
    if expected_byte_size is not None and claims.byte_size != _artifact_size(
        expected_byte_size
    ):
        return False
    if expected_storage_key is not None and claims.storage_key != _storage_key(
        expected_storage_key
    ):
        return False
    return True


def verify_release_manifest(
    manifest_input: str | bytes | None,
    *,
    trusted_public_keys: Mapping[str, object],
    expected_platform: str | None = None,
    expected_architecture: str | None = None,
    expected_version: str | SemanticVersion | None = None,
    expected_build: int | None = None,
    expected_artifact_kind: str | ArtifactKind | None = None,
    expected_sha256: str | None = None,
    expected_byte_size: int | None = None,
    expected_storage_key: str | None = None,
) -> ReleaseManifestVerificationResult:
    """Verify a signed release manifest and every supplied expectation."""

    if manifest_input is None:
        return _result(STATUS_NOT_FOUND, _MESSAGE_NOT_FOUND)
    if not _ED25519_AVAILABLE or _Ed25519PublicKey is None:
        return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
    if not isinstance(trusted_public_keys, Mapping) or not trusted_public_keys:
        return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
    try:
        if type(manifest_input) is str:
            if len(manifest_input) > _MAX_ENVELOPE_BYTES:
                raise _ManifestInvalid("release envelope is too large")
            envelope_bytes = manifest_input.encode("utf-8", errors="strict")
        elif type(manifest_input) is bytes:
            envelope_bytes = manifest_input
        else:
            raise _ManifestInvalid("release manifest must be text or bytes")
        if not envelope_bytes or len(envelope_bytes) > _MAX_ENVELOPE_BYTES:
            raise _ManifestInvalid("release envelope size is invalid")

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
            raise _ManifestInvalid("wrong Ed25519 signature length")

        claims = _parse_manifest_payload(payload_bytes)
        if claims.signing_key_id not in trusted_public_keys:
            raise _ManifestInvalid("release signing key is not trusted")
        try:
            public_key = _load_trusted_public_key(
                trusted_public_keys[claims.signing_key_id]
            )
        except RuntimeError:
            return _result(STATUS_NOT_AVAILABLE, _MESSAGE_NOT_AVAILABLE)
        try:
            public_key.verify(signature, payload_bytes)
        except (_InvalidSignature, TypeError, ValueError) as exc:
            raise _ManifestInvalid("release signature verification failed") from exc

        if not _matches_expectations(
            claims,
            expected_platform=expected_platform,
            expected_architecture=expected_architecture,
            expected_version=expected_version,
            expected_build=expected_build,
            expected_artifact_kind=expected_artifact_kind,
            expected_sha256=expected_sha256,
            expected_byte_size=expected_byte_size,
            expected_storage_key=expected_storage_key,
        ):
            raise _ManifestInvalid("release does not match expected artifact")
    except (
        _ManifestInvalid,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
    ):
        return _result(STATUS_INVALID, _MESSAGE_INVALID)
    return _result(STATUS_SUCCESS, _MESSAGE_SUCCESS, claims)


__all__ = [
    "ENVELOPE_SCHEMA",
    "MANIFEST_SCHEMA",
    "MAX_ARTIFACT_BYTES",
    "MAX_COMPATIBLE_SOURCE_VERSIONS",
    "SCHEMA_VERSION",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "ArtifactKind",
    "ReleaseManifestVerificationResult",
    "VerifiedReleaseManifest",
    "verify_release_manifest",
]

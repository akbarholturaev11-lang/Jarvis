"""Validated, platform-neutral commerce domain types for JARVIS.

This module deliberately contains no HTTP, UI, payment-provider, or object-
storage implementation.  Screenshot and release package bodies stay outside the
database; the domain stores only opaque private-storage keys and verification
metadata.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from core.product_state import PaymentState
from core.release_manifest import ArtifactKind
from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    UNKNOWN_TARGET,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)


SINGLE_PAID_PLAN_CODE: Final = "jarvis_single_paid"
MAX_PAYMENT_SCREENSHOT_BYTES: Final = 10 * 1024 * 1024
PAYMENT_SCREENSHOT_MIME_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)


class CommerceError(RuntimeError):
    """Base class for expected commerce-domain failures."""


class ValidationError(CommerceError, ValueError):
    """Input failed a domain validation rule."""


class NotFoundError(CommerceError):
    """A requested domain record does not exist."""


class ConflictError(CommerceError):
    """A uniqueness or explicit-replacement invariant was violated."""


class InvalidTransitionError(CommerceError):
    """A state transition was not explicitly allowed."""


class PersistenceInvariantError(CommerceError):
    """Stored data violated an invariant that repository code guarantees."""


class ArtifactVerificationError(CommerceError):
    """Artifact authenticity could not be verified; persistence is forbidden."""


class ReleaseState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class InstallMode(StrEnum):
    FRESH_INSTALL = "fresh_install"
    UPDATE = "update"


class AdminDecisionKind(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class InstallDecisionReason(StrEnum):
    AUTHORIZED = "authorized"
    LICENSE_NOT_FOUND = "license_not_found"
    DEVICE_NOT_BOUND = "device_not_bound"
    DEVICE_PROOF_REQUIRED = "device_proof_required"
    DEVICE_NOT_ACTIVE = "device_not_active"
    DEVICE_TARGET_MISMATCH = "device_target_mismatch"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_NOT_VERIFIED = "artifact_not_verified"
    RELEASE_NOT_PUBLISHED = "release_not_published"
    ENTITLEMENT_REQUIRED = "entitlement_required"
    INCOMPATIBLE_SOURCE_VERSION = "incompatible_source_version"
    SOURCE_BUILD_NOT_OLDER = "source_build_not_older"
    SOURCE_VERSION_NOT_OLDER = "source_version_not_older"
    ARTIFACT_KIND_MISMATCH = "artifact_kind_mismatch"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    external_subject: str
    created_at: str


@dataclass(frozen=True, slots=True)
class License:
    id: str
    account_id: str
    plan_code: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    id: str
    license_id: str
    device_key_fingerprint: str
    platform: str
    architecture: str
    device_label: str | None
    activated_at: str
    deactivated_at: str | None
    replaced_by_binding_id: str | None
    replacement_reason: str | None

    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


@dataclass(frozen=True, slots=True)
class Release:
    id: str
    version: str
    state: ReleaseState
    price_minor: int
    currency: str
    created_at: str
    published_at: str | None
    features_en: str = ""
    features_ru: str = ""
    fixes_en: str = ""
    fixes_ru: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    version: str
    platform: str
    architecture: str
    build: int
    artifact_kind: ArtifactKind


@dataclass(frozen=True, slots=True)
class VerifiedDevicePrincipal:
    """Device authorization context produced after API challenge verification.

    The API/authentication layer proves possession of the per-install private key
    and creates this context.  The repository performs authorization *after* that
    proof; it is not a network authenticator and never receives the private key.
    """

    device_key_fingerprint: str
    platform: str
    architecture: str
    proof_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "device_key_fingerprint",
            validate_device_key_fingerprint(self.device_key_fingerprint),
        )
        object.__setattr__(
            self, "platform", normalize_target_platform(self.platform)
        )
        object.__setattr__(
            self,
            "architecture",
            normalize_target_architecture(self.architecture),
        )
        if type(self.proof_verified) is not bool:
            raise ValidationError("proof_verified must be a boolean")


@dataclass(frozen=True, slots=True)
class ArtifactVerificationCandidate:
    """Complete immutable artifact metadata presented to a trusted verifier."""

    product_id: str
    bundle_id: str
    release_version: str
    platform: str
    architecture: str
    artifact_kind: ArtifactKind
    build: int
    sha256: str
    byte_size: int
    storage_key: str
    signature: str
    signing_key_id: str
    compatible_source_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactVerificationReceipt:
    verified_at: str
    verification_key_id: str


@runtime_checkable
class ArtifactVerifier(Protocol):
    def verify(
        self, candidate: ArtifactVerificationCandidate
    ) -> ArtifactVerificationReceipt: ...


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    id: str
    release_id: str
    identity: ArtifactIdentity
    sha256: str
    byte_size: int
    storage_key: str
    signature: str
    signing_key_id: str
    signature_verified_at: str
    verification_key_id: str
    compatible_source_versions: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class PaymentSubmission:
    id: str
    license_id: str
    release_id: str
    amount_minor: int
    currency: str
    screenshot_storage_key: str
    screenshot_sha256: str
    screenshot_byte_size: int
    screenshot_mime_type: str
    paid_at: str
    submitted_at: str
    state: PaymentState
    review_started_at: str | None
    review_started_by: str | None
    decided_at: str | None
    decided_by: str | None
    rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class Entitlement:
    id: str
    license_id: str
    release_id: str
    version: str
    granted_by_payment_id: str
    granted_at: str


@dataclass(frozen=True, slots=True)
class AdminDecisionAudit:
    id: str
    payment_id: str
    actor_admin_subject: str
    decision: AdminDecisionKind
    reason: str | None
    occurred_at: str


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    payment: PaymentSubmission
    entitlement: Entitlement
    audit: AdminDecisionAudit
    idempotent: bool


@dataclass(frozen=True, slots=True)
class InstallAuthorization:
    allowed: bool
    reason: InstallDecisionReason
    artifact_identity: ArtifactIdentity | None = None


_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DEVICE_KEY_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_SIGNATURE_RE = re.compile(r"[A-Za-z0-9_-]{86}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[ _-]?key|token|password|secret|authorization)"
    r"\s*[:=]\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")


def validate_opaque_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValidationError(f"{field} must be an opaque identifier")
    return value


def validate_device_key_fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or _DEVICE_KEY_FINGERPRINT_RE.fullmatch(value) is None
    ):
        raise ValidationError(
            "device_key_fingerprint must be sha256: plus 64 lowercase hex digits"
        )
    return value


def normalize_semver(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("version must be a string")
    try:
        return str(SemanticVersion.parse(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("version must use strict MAJOR.MINOR.PATCH") from exc


def normalize_target_platform(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("platform must be explicit")
    normalized = normalize_platform(value)
    if normalized == UNKNOWN_TARGET:
        raise ValidationError("platform must be macos, windows, or linux")
    return normalized


def normalize_target_architecture(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("architecture must be explicit")
    normalized = normalize_architecture(value)
    if normalized == UNKNOWN_TARGET:
        raise ValidationError("architecture is not supported")
    return normalized


def validate_build(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError("build must be a positive integer")
    return value


def validate_minor_amount(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError("price_minor must be a positive integer")
    return value


def validate_currency(value: object) -> str:
    if not isinstance(value, str) or _CURRENCY_RE.fullmatch(value) is None:
        raise ValidationError("currency must be a three-letter uppercase ISO code")
    return value


def validate_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValidationError("sha256 must be a lowercase hexadecimal string")
    return value


def validate_byte_size(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError("byte_size must be a positive integer")
    return value


def validate_payment_screenshot_size(value: object) -> int:
    value = validate_byte_size(value)
    if value > MAX_PAYMENT_SCREENSHOT_BYTES:
        raise ValidationError("payment screenshot exceeds the 10 MiB limit")
    return value


def validate_payment_screenshot_mime_type(value: object) -> str:
    if not isinstance(value, str) or value not in PAYMENT_SCREENSHOT_MIME_TYPES:
        raise ValidationError("payment screenshot MIME type is not allowed")
    return value


def validate_storage_key(value: object, *, field: str) -> str:
    """Validate an opaque private object-store key, never a public URL."""

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a private storage key")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 512:
        raise ValidationError(f"{field} has an invalid length")
    if "://" in normalized or normalized.startswith(("/", "\\")):
        raise ValidationError(f"{field} must not be a URL or absolute path")
    if _CONTROL_RE.search(normalized):
        raise ValidationError(f"{field} contains control characters")
    if any(part == ".." for part in normalized.replace("\\", "/").split("/")):
        raise ValidationError(f"{field} must not traverse storage namespaces")
    return normalized


def validate_signature(value: object) -> str:
    if type(value) is not str or _SIGNATURE_RE.fullmatch(value) is None:
        raise ValidationError("signature has an invalid format")
    try:
        decoded = base64.b64decode(
            value + "==",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("signature has an invalid format") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != 64 or canonical != value:
        raise ValidationError("signature has an invalid format")
    return value


def sanitize_human_text(
    value: object,
    *,
    field: str,
    max_length: int,
) -> str:
    """Store displayable text without controls, markup, or obvious secrets."""

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _BEARER_RE.sub("Bearer [redacted]", normalized)
    normalized = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]", normalized
    )
    normalized = normalized.replace("<", "").replace(">", "")
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        raise ValidationError(f"{field} must not be empty")
    return normalized[:max_length].rstrip()


def normalize_utc_timestamp(value: object, *, field: str) -> str:
    """Accept and return a canonical UTC ISO-8601 timestamp ending in ``Z``."""

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{field} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError(f"{field} must be UTC")
    return format_utc_timestamp(parsed)


def format_utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PersistenceInvariantError("clock must return a timezone-aware datetime")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")

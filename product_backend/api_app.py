"""FastAPI application factory for the JARVIS product backend foundation.

The factory requires injected persistence/storage/challenge dependencies and
hashed admin credentials.  It does not create a deployment, load hardcoded
credentials, or claim the commercial release gates are cleared.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from core.product_api_client import (
    DEVICE_MISMATCH_ERROR_HEADER,
    DEVICE_MISMATCH_ERROR_VALUE,
)
from core.product_state import PaymentState
from core.product_version import PRODUCT_ID
from core.release_manifest import ArtifactKind

from .admin_web import mount_admin_web
from .api_activation import (
    ActivationDeviceMismatchError,
    ActivationNotAvailableError,
    ActivationRejectedError,
)
from .admin_mfa import SQLiteAdminMfaManager
from .admin_mfa_api import (
    complete_admin_login,
    register_admin_security_routes,
)
from .api_artifact_storage import ReleaseArtifactStorageError
from .api_auth import (
    AdminAuthSettings,
    AdminCredentialStore,
    AdminIpAllowlist,
    AdminSessionManager,
    AdminSessionRecord,
    AuthenticationCapacityError,
    BackendConfigurationError,
    BoundedAttemptLimiter,
    DeviceActionGrantManager,
    ReservedDeviceGrant,
    SessionAssurance,
    TrustedProxyConfig,
)
from .api_operational import (
    OperationalMiddleware,
    OperationalPolicy,
    ReadinessResult,
)
from .observability import MetricsRegistry, NullMetrics
from .api_ports import (
    ClientActivationPort,
    DeviceChallengePort,
    PrivatePaymentEvidenceStore,
    ProductReadStore,
    ReleaseArtifactStore,
    VerifiedReleaseArtifactStream,
)
from .api_queries import ProductReadNotAvailableError
from .api_service import (
    BackendServiceNotAvailableError,
    ProductBackendService,
)
from .api_updates import (
    ArtifactDownloadGrantManager,
    release_manifest_envelope,
)
from .device_challenges import (
    STATUS_ALREADY_USED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    DeviceChallengeAction,
    VerifiedDeviceChallenge,
)
from .initial_purchase import (
    InitialPurchaseAuthorizer,
    InitialPurchaseGrantReservation,
)
from .models import (
    MAX_PAYMENT_SCREENSHOT_BYTES,
    ArtifactVerificationError,
    CommerceError,
    ConflictError,
    InstallMode,
    InvalidTransitionError,
    NotFoundError,
    ReleaseState,
    ValidationError,
    normalize_target_architecture,
    normalize_target_platform,
)
from .payment_instructions import (
    PaymentInstructionsLoadResult,
    load_payment_instructions,
)
from .private_storage import (
    PrivateStorageIntegrityError,
    PrivateStorageNotAvailableError,
    PrivateStorageValidationError,
)
from .repository import CommerceRepository


_MAX_JSON_HTTP_BODY_BYTES = 256 * 1024
_MAX_PAYMENT_HTTP_BODY_BYTES = MAX_PAYMENT_SCREENSHOT_BYTES + (512 * 1024)
_UPLOAD_READ_CHUNK = 64 * 1024
_ARTIFACT_RESPONSE_CHUNK_BYTES = 256 * 1024
_ADMIN_LOGIN_CLIENT_MAX_ATTEMPTS = 20
_ADMIN_LOGIN_WINDOW_SECONDS = 300
_RESERVED_PAYMENT_GRANT_STATE = "jarvis_reserved_payment_grant"
_RESERVED_INITIAL_PURCHASE_GRANT_STATE = (
    "jarvis_reserved_initial_purchase_grant"
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminLoginBody(_StrictBody):
    subject: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    recovery_code: str | None = Field(default=None, min_length=8, max_length=32)


class ReleaseCreateBody(_StrictBody):
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    price_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    features_en: str = Field(default="", max_length=4000)
    features_ru: str = Field(default="", max_length=4000)
    fixes_en: str = Field(default="", max_length=4000)
    fixes_ru: str = Field(default="", max_length=4000)


class ArtifactCreateBody(_StrictBody):
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)
    artifact_kind: ArtifactKind
    build: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    storage_key: str = Field(min_length=1, max_length=512)
    signature: str = Field(min_length=86, max_length=86)
    signing_key_id: str = Field(min_length=3, max_length=128)
    compatible_source_versions: list[str] = Field(default_factory=list, max_length=100)


class PaymentRejectBody(_StrictBody):
    reason: str = Field(min_length=1, max_length=1000)


class AdminAccountCreateBody(_StrictBody):
    external_subject: str = Field(min_length=3, max_length=128)


class AdminDeviceBindBody(_StrictBody):
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)
    device_label: str | None = Field(default=None, min_length=1, max_length=120)


class AdminDeviceReplaceBody(_StrictBody):
    current_device_key_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    new_device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    new_platform: str = Field(min_length=2, max_length=32)
    new_architecture: str = Field(min_length=2, max_length=32)
    new_device_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    replacement_reason: str = Field(min_length=1, max_length=240)


class DeviceChallengeIssueBody(_StrictBody):
    license_id: str = Field(min_length=3, max_length=128)
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action: DeviceChallengeAction
    resource_id: str = Field(min_length=3, max_length=128)


class DeviceChallengeVerifyBody(_StrictBody):
    challenge_nonce: str = Field(min_length=43, max_length=43)
    public_key_base64: str = Field(min_length=43, max_length=43)
    signature_base64: str = Field(min_length=86, max_length=86)


class ActivationChallengeBody(_StrictBody):
    product_id: str = Field(min_length=3, max_length=64)
    license_key: str = Field(min_length=8, max_length=256)
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    device_public_key: str = Field(min_length=43, max_length=43)
    version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)


class ActivationCompleteBody(_StrictBody):
    product_id: str = Field(min_length=3, max_length=64)
    challenge_id: str = Field(min_length=3, max_length=128)
    challenge_nonce: str = Field(min_length=43, max_length=43)
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    device_public_key: str = Field(min_length=43, max_length=43)
    challenge_signature: str = Field(min_length=86, max_length=86)
    version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)


class UpdateCheckBody(_StrictBody):
    product_id: str = Field(min_length=3, max_length=64)
    license_id: str = Field(min_length=3, max_length=128)
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    installed_version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    installed_build: int = Field(gt=0)
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)


class InitialPurchaseChallengeBody(_StrictBody):
    product_id: str = Field(min_length=3, max_length=64)
    purchase_id: str = Field(pattern=r"^purchase_[0-9a-f]{32}$")
    version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    device_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: str = Field(min_length=2, max_length=32)
    architecture: str = Field(min_length=2, max_length=32)


class InitialPurchaseVerifyBody(_StrictBody):
    challenge_nonce: str = Field(min_length=43, max_length=43)
    public_key_base64: str = Field(min_length=43, max_length=43)
    signature_base64: str = Field(min_length=86, max_length=86)


class _RequestBodyTooLarge(Exception):
    pass


def _payment_upload_context(
    scope: dict[str, Any],
    headers: dict[bytes, bytes],
) -> tuple[str, str, str, str] | None:
    path = scope.get("path")
    parts = path.split("/") if isinstance(path, str) else []
    opaque_characters = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@+-"
    )

    def opaque(value: str) -> bool:
        return (
            3 <= len(value) <= 128
            and value[0].isalnum()
            and all(character in opaque_characters for character in value)
        )

    content_type = headers.get(b"content-type", b"").lower()
    device_grant = headers.get(b"x-device-grant", b"")
    purchase_grant = headers.get(b"x-purchase-grant", b"")

    def grant_valid(grant: bytes) -> bool:
        return 20 <= len(grant) <= 128 and all(
            character
            in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in grant
        )

    existing_shape = (
        scope.get("method") == "POST"
        and len(parts) == 8
        and parts[:4] == ["", "api", "customer", "licenses"]
        and opaque(parts[4])
        and parts[5] == "releases"
        and opaque(parts[6])
        and parts[7] == "payments"
        and content_type.startswith(b"multipart/form-data;")
        and grant_valid(device_grant)
    )
    if existing_shape:
        return "license", parts[4], parts[6], device_grant.decode("ascii")
    initial_shape = (
        scope.get("method") == "POST"
        and len(parts) == 7
        and parts[:3] == ["", "api", "purchases"]
        and parts[3].startswith("purchase_")
        and opaque(parts[3])
        and parts[4] == "releases"
        and opaque(parts[5])
        and parts[6] == "payments"
        and content_type.startswith(b"multipart/form-data;")
        and grant_valid(purchase_grant)
    )
    if initial_shape:
        return "purchase", parts[3], parts[5], purchase_grant.decode("ascii")
    return None


class _BoundedBodyMiddleware:
    """Apply a small default cap before parsing; enlarge only payment upload."""

    def __init__(
        self,
        app: Any,
        *,
        default_maximum_bytes: int,
        payment_maximum_bytes: int,
        payment_grants: DeviceActionGrantManager,
        initial_purchases: InitialPurchaseAuthorizer | None = None,
    ) -> None:
        self.app = app
        self.default_maximum_bytes = default_maximum_bytes
        self.payment_maximum_bytes = payment_maximum_bytes
        self.payment_grants = payment_grants
        self.initial_purchases = initial_purchases

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        maximum_bytes = self.default_maximum_bytes
        reservation: ReservedDeviceGrant | InitialPurchaseGrantReservation | None = None
        payment_context = _payment_upload_context(scope, headers)
        if payment_context is not None:
            kind, subject_id, release_id, grant = payment_context
            if kind == "license":
                reserved = self.payment_grants.reserve(
                    grant,
                    license_id=subject_id,
                    action=DeviceChallengeAction.SUBMIT_PAYMENT,
                    resource_id=release_id,
                )
            else:
                reserved = (
                    None
                    if self.initial_purchases is None
                    else self.initial_purchases.reserve_grant(
                        grant,
                        purchase_id=subject_id,
                        release_id=release_id,
                    )
                )
            state = scope.setdefault("state", {})
            if isinstance(
                reserved,
                (ReservedDeviceGrant, InitialPurchaseGrantReservation),
            ):
                reservation = reserved
                if isinstance(state, dict):
                    reservation_key = (
                        _RESERVED_PAYMENT_GRANT_STATE
                        if isinstance(reserved, ReservedDeviceGrant)
                        else _RESERVED_INITIAL_PURCHASE_GRANT_STATE
                    )
                    state[reservation_key] = reserved
                    maximum_bytes = self.payment_maximum_bytes
        try:
            raw_length = headers.get(b"content-length")
            if raw_length is not None:
                try:
                    declared_length = int(raw_length)
                    if declared_length < 0 or declared_length > maximum_bytes:
                        await self._reject(send)
                        return
                except ValueError:
                    await self._reject(send)
                    return

            consumed = 0
            response_started = False

            async def bounded_receive() -> dict[str, Any]:
                nonlocal consumed
                message = await receive()
                if message.get("type") == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > maximum_bytes:
                        raise _RequestBodyTooLarge
                return message

            async def tracked_send(message: dict[str, Any]) -> None:
                nonlocal response_started
                if message.get("type") == "http.response.start":
                    response_started = True
                await send(message)

            try:
                await self.app(scope, bounded_receive, tracked_send)
            except _RequestBodyTooLarge:
                if not response_started:
                    await self._reject(send)
        finally:
            if isinstance(reservation, ReservedDeviceGrant):
                self.payment_grants.release(reservation)
            elif isinstance(reservation, InitialPurchaseGrantReservation):
                if self.initial_purchases is not None:
                    self.initial_purchases.release_grant(reservation)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _release_payload(release: Any) -> dict[str, Any]:
    return {
        "id": release.id,
        "version": release.version,
        "state": release.state.value,
        "price_minor": release.price_minor,
        "currency": release.currency,
        "created_at": release.created_at,
        "published_at": release.published_at,
        "features_en": release.features_en,
        "features_ru": release.features_ru,
        "fixes_en": release.fixes_en,
        "fixes_ru": release.fixes_ru,
    }


def _admin_pagination_payload(page: Any) -> dict[str, Any]:
    returned = len(page.records)
    return {
        "limit": page.limit,
        "offset": page.offset,
        "returned": returned,
        "total": page.total,
        "has_more": page.offset + returned < page.total,
    }


def _admin_device_payload(device: Any) -> dict[str, Any]:
    return {
        "id": device.id,
        "license_id": device.license_id,
        "device_key_fingerprint": device.device_key_fingerprint,
        "platform": device.platform,
        "architecture": device.architecture,
        "device_label": device.device_label,
        "activated_at": device.activated_at,
    }


def _admin_entitlement_payload(entitlement: Any) -> dict[str, Any]:
    return {
        "id": entitlement.id,
        "release_id": entitlement.release_id,
        "version": entitlement.version,
        "granted_at": entitlement.granted_at,
    }


def _release_info_payload(release: Any, artifacts: Any) -> dict[str, Any]:
    supported_platforms = sorted(
        {
            artifact.identity.platform
            for artifact in artifacts
            if artifact.release_id == release.id
        }
    )
    return {
        "version": release.version,
        "price_minor": release.price_minor,
        "currency": release.currency,
        "supported_platforms": supported_platforms,
        "features": {"en": release.features_en, "ru": release.features_ru},
        "fixes": {"en": release.fixes_en, "ru": release.fixes_ru},
    }


def _has_verified_initial_target(
    artifacts: Any,
    *,
    platform: str,
    architecture: str,
) -> bool:
    return any(
        artifact.identity.platform == platform
        and artifact.identity.architecture == architecture
        and artifact.identity.artifact_kind is ArtifactKind.INITIAL_INSTALLER
        and bool(artifact.signature_verified_at)
        and artifact.verification_key_id == artifact.signing_key_id
        for artifact in artifacts
    )


def _payment_instructions_payload(
    result: PaymentInstructionsLoadResult,
) -> dict[str, Any]:
    if not result.configured or result.instructions is None:
        return {"status": "not_configured"}
    configured = result.instructions
    return {
        "status": "configured",
        "method": {
            "en": configured.method.en,
            "ru": configured.method.ru,
        },
        "recipient": configured.recipient,
        "instructions": {
            "en": configured.instructions.en,
            "ru": configured.instructions.ru,
        },
    }


def _artifact_payload(artifact: Any, *, admin: bool) -> dict[str, Any]:
    payload = {
        "id": artifact.id,
        "version": artifact.identity.version,
        "platform": artifact.identity.platform,
        "architecture": artifact.identity.architecture,
        "artifact_kind": artifact.identity.artifact_kind.value,
        "build": artifact.identity.build,
        "sha256": artifact.sha256,
        "byte_size": artifact.byte_size,
        "signature_verified_at": artifact.signature_verified_at,
        "verification_key_id": artifact.verification_key_id,
    }
    if admin:
        payload.update(
            {
                "storage_key": artifact.storage_key,
                "signing_key_id": artifact.signing_key_id,
                "compatible_source_versions": list(
                    artifact.compatible_source_versions
                ),
                "created_at": artifact.created_at,
            }
        )
    return payload


def _payment_payload(record: Any, *, admin: bool) -> dict[str, Any]:
    payment = record.payment if hasattr(record, "payment") else record
    payload = {
        "id": payment.id,
        "release_id": payment.release_id,
        "version": getattr(record, "version", None),
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
        "paid_at": payment.paid_at,
        "submitted_at": payment.submitted_at,
        "state": payment.state.value,
        "rejection_reason": payment.rejection_reason,
    }
    if admin:
        payload.update(
            {
                "license_id": payment.license_id,
                "review_started_at": payment.review_started_at,
                "review_started_by": payment.review_started_by,
                "decided_at": payment.decided_at,
                "decided_by": payment.decided_by,
                "evidence": {
                    "sha256": payment.screenshot_sha256,
                    "byte_size": payment.screenshot_byte_size,
                    "content_type": payment.screenshot_mime_type,
                },
            }
        )
    return payload


def _challenge_error_status(result_status: str) -> int:
    return {
        STATUS_NOT_FOUND: 404,
        STATUS_INVALID: 400,
        STATUS_EXPIRED: 410,
        STATUS_ALREADY_USED: 409,
        STATUS_NOT_AVAILABLE: 503,
        STATUS_FAILED: 503,
    }.get(result_status, 503)


async def _read_bounded_upload(upload: UploadFile) -> bytes:
    if upload.size is not None and upload.size > MAX_PAYMENT_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="payment evidence is too large")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await upload.read(_UPLOAD_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PAYMENT_SCREENSHOT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="payment evidence is too large",
                )
            chunks.append(chunk)
    finally:
        await upload.close()
    return b"".join(chunks)


def _iter_verified_artifact(
    stream: VerifiedReleaseArtifactStream,
) -> Iterator[bytes]:
    try:
        while True:
            chunk = stream.read(_ARTIFACT_RESPONSE_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    finally:
        stream.close()


def create_product_backend_app(
    *,
    commerce: CommerceRepository,
    reads: ProductReadStore,
    evidence_store: PrivatePaymentEvidenceStore,
    challenges: DeviceChallengePort,
    activation: ClientActivationPort | None = None,
    release_artifact_store: ReleaseArtifactStore | None = None,
    auth_settings: AdminAuthSettings | None = None,
    payment_instructions: PaymentInstructionsLoadResult | None = None,
    mfa: SQLiteAdminMfaManager | None = None,
    trusted_proxy: TrustedProxyConfig | None = None,
    admin_ip_allowlist: AdminIpAllowlist | None = None,
    admin_credential_store: AdminCredentialStore | None = None,
    allow_password_only_admin: bool = False,
    operational_policy: OperationalPolicy | None = None,
    metrics: MetricsRegistry | None = None,
    request_logger: logging.Logger | None = None,
    clock: Any = None,
) -> FastAPI:
    """Create an API process from explicit dependencies and security config.

    When ``auth_settings`` is omitted, only hash/session material from the
    documented ``JARVIS_*`` environment variables is accepted.  Missing values
    stop app creation rather than starting an unauthenticated admin surface.

    When ``mfa`` is provided the admin login enforces a second factor and the
    MFA/session-management routes are mounted.  ``trusted_proxy`` controls
    whether an ``X-Forwarded-For`` header may be honored for rate-limit
    identity; without it the direct socket peer is authoritative.
    """

    if mfa is not None and not isinstance(mfa, SQLiteAdminMfaManager):
        raise BackendConfigurationError("MFA manager is invalid")
    if type(allow_password_only_admin) is not bool:
        raise BackendConfigurationError("password-only admin override is invalid")
    if mfa is None and not allow_password_only_admin:
        raise BackendConfigurationError(
            "admin MFA is required unless password-only mode is explicitly enabled"
        )
    proxy_config = (
        TrustedProxyConfig(()) if trusted_proxy is None else trusted_proxy
    )
    if not isinstance(proxy_config, TrustedProxyConfig):
        raise BackendConfigurationError("trusted proxy configuration is invalid")
    ip_allowlist = (
        AdminIpAllowlist(())
        if admin_ip_allowlist is None
        else admin_ip_allowlist
    )
    if not isinstance(ip_allowlist, AdminIpAllowlist):
        raise BackendConfigurationError("admin IP allowlist is invalid")

    if not isinstance(activation, ClientActivationPort):
        raise BackendConfigurationError(
            "client activation authority and signer are required"
        )
    if not isinstance(release_artifact_store, ReleaseArtifactStore):
        raise BackendConfigurationError("private release artifact store is required")
    settings = AdminAuthSettings.from_env() if auth_settings is None else auth_settings
    payment_configuration = (
        load_payment_instructions(None)
        if payment_instructions is None
        else payment_instructions
    )
    if not isinstance(payment_configuration, PaymentInstructionsLoadResult):
        raise BackendConfigurationError("payment instructions configuration is invalid")
    now = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    service = ProductBackendService(commerce, reads, evidence_store, challenges)
    sessions = AdminSessionManager(
        settings,
        credential_store=admin_credential_store,
        clock=now,
    )
    grant_secret = hmac.new(
        settings.session_secret,
        b"jarvis-device-action-grants",
        hashlib.sha256,
    ).digest()
    grants = DeviceActionGrantManager(grant_secret, clock=now)
    initial_purchase_secret = hmac.new(
        settings.session_secret,
        b"jarvis-initial-purchase-grants",
        hashlib.sha256,
    ).digest()
    initial_purchases = InitialPurchaseAuthorizer(
        initial_purchase_secret,
        clock=now,
    )
    artifact_grant_secret = hmac.new(
        settings.session_secret,
        b"jarvis-artifact-download-grants",
        hashlib.sha256,
    ).digest()
    artifact_grants = ArtifactDownloadGrantManager(
        artifact_grant_secret,
        clock=now,
    )
    login_subject_limiter = BoundedAttemptLimiter(clock=now)
    login_client_limiter = BoundedAttemptLimiter(
        max_attempts=_ADMIN_LOGIN_CLIENT_MAX_ATTEMPTS,
        window_seconds=_ADMIN_LOGIN_WINDOW_SECONDS,
        max_keys=1024,
        clock=now,
    )
    challenge_limiter = BoundedAttemptLimiter(
        max_attempts=10,
        window_seconds=300,
        max_keys=2048,
        clock=now,
    )
    initial_purchase_limiter = BoundedAttemptLimiter(
        max_attempts=10,
        window_seconds=300,
        max_keys=2048,
        clock=now,
    )
    initial_purchase_client_limiter = BoundedAttemptLimiter(
        max_attempts=20,
        window_seconds=300,
        max_keys=2048,
        clock=now,
    )
    login_factor_limiter = BoundedAttemptLimiter(
        max_attempts=5,
        window_seconds=300,
        max_keys=1024,
        clock=now,
    )
    mfa_enrollment_limiter = BoundedAttemptLimiter(
        max_attempts=5,
        window_seconds=900,
        max_keys=1024,
        clock=now,
    )
    mfa_stepup_limiter = BoundedAttemptLimiter(
        max_attempts=5,
        window_seconds=300,
        max_keys=1024,
        clock=now,
    )
    admin_password_limiter = BoundedAttemptLimiter(
        max_attempts=5,
        window_seconds=900,
        max_keys=1024,
        clock=now,
    )

    app = FastAPI(
        title="JARVIS Product Backend Foundation",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
    app.add_middleware(
        _BoundedBodyMiddleware,
        default_maximum_bytes=_MAX_JSON_HTTP_BODY_BYTES,
        payment_maximum_bytes=_MAX_PAYMENT_HTTP_BODY_BYTES,
        payment_grants=grants,
        initial_purchases=initial_purchases,
    )
    app.state.product_backend_service = service
    app.state.admin_sessions = sessions
    app.state.device_grants = grants
    app.state.initial_purchase_authorizer = initial_purchases
    app.state.artifact_download_grants = artifact_grants
    app.state.client_activation = activation
    app.state.admin_mfa = mfa

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response

    @app.exception_handler(NotFoundError)
    async def commerce_not_found(_: Request, __: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": "resource not found"}, status_code=404)

    @app.exception_handler(ConflictError)
    async def commerce_conflict(_: Request, __: CommerceError) -> JSONResponse:
        return JSONResponse({"detail": "operation conflicts with current state"}, status_code=409)

    app.add_exception_handler(InvalidTransitionError, commerce_conflict)

    @app.exception_handler(ArtifactVerificationError)
    async def artifact_unverified(_: Request, __: ArtifactVerificationError) -> JSONResponse:
        return JSONResponse({"detail": "artifact verification failed"}, status_code=422)

    @app.exception_handler(ValidationError)
    async def commerce_invalid(_: Request, __: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": "request is invalid"}, status_code=400)

    @app.exception_handler(CommerceError)
    async def commerce_error(_: Request, __: CommerceError) -> JSONResponse:
        return JSONResponse({"detail": "commerce operation failed"}, status_code=400)

    @app.exception_handler(PrivateStorageValidationError)
    async def storage_invalid(_: Request, __: PrivateStorageValidationError) -> JSONResponse:
        return JSONResponse({"detail": "payment evidence is invalid"}, status_code=400)

    @app.exception_handler(PrivateStorageIntegrityError)
    async def storage_integrity(_: Request, __: PrivateStorageIntegrityError) -> JSONResponse:
        return JSONResponse({"detail": "payment evidence integrity failed"}, status_code=409)

    @app.exception_handler(PrivateStorageNotAvailableError)
    async def backend_unavailable(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse({"detail": "backend dependency is not available"}, status_code=503)

    app.add_exception_handler(ProductReadNotAvailableError, backend_unavailable)
    app.add_exception_handler(BackendServiceNotAvailableError, backend_unavailable)
    app.add_exception_handler(AuthenticationCapacityError, backend_unavailable)
    app.add_exception_handler(ActivationNotAvailableError, backend_unavailable)
    app.add_exception_handler(ReleaseArtifactStorageError, backend_unavailable)

    @app.exception_handler(ActivationRejectedError)
    async def activation_rejected(
        _: Request, __: ActivationRejectedError
    ) -> JSONResponse:
        return JSONResponse({"detail": "activation was not approved"}, status_code=401)

    @app.exception_handler(ActivationDeviceMismatchError)
    async def activation_device_mismatch(
        _: Request, __: ActivationDeviceMismatchError
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "activation conflicts with the active device"},
            status_code=409,
            headers={
                DEVICE_MISMATCH_ERROR_HEADER: DEVICE_MISMATCH_ERROR_VALUE,
            },
        )

    def _resolved_client_ip(request: Request) -> str:
        peer = None if request.client is None else request.client.host
        return proxy_config.client_ip(
            peer,
            request.headers.get("x-forwarded-for"),
        )

    def _client_attempt_key(request: Request) -> str:
        host = _resolved_client_ip(request)
        return hashlib.sha256(f"client|{host}".encode("utf-8")).hexdigest()

    def _attempt_key(request: Request, subject: str) -> str:
        return hashlib.sha256(
            f"{_client_attempt_key(request)}|{subject}".encode("utf-8")
        ).hexdigest()

    def _subject_attempt_key(subject: object) -> str:
        normalized = subject if isinstance(subject, str) else "invalid"
        return hashlib.sha256(
            f"admin-subject|{normalized}".encode("utf-8")
        ).hexdigest()

    def _enforce_admin_network(request: Request) -> None:
        if not ip_allowlist.allows(_resolved_client_ip(request)):
            raise HTTPException(status_code=403, detail="admin network is not allowed")

    def require_admin_any(request: Request) -> AdminSessionRecord:
        """Any live admin session, including a restricted enrollment session."""

        _enforce_admin_network(request)
        token = request.cookies.get(settings.cookie_name)
        record = sessions.resolve(token)
        if record is None:
            raise HTTPException(status_code=401, detail="admin authentication required")
        request.state.admin_session = record
        return record

    def require_admin(
        record: AdminSessionRecord = Depends(require_admin_any),
    ) -> AdminSessionRecord:
        """A fully authenticated session; enrollment-only sessions are refused."""

        if record.assurance is not SessionAssurance.MFA_SATISFIED:
            raise HTTPException(
                status_code=403, detail="multi-factor enrollment is required"
            )
        return record

    def require_admin_csrf(
        record: AdminSessionRecord = Depends(require_admin),
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> AdminSessionRecord:
        if not sessions.verify_csrf(record, csrf_token):
            raise HTTPException(status_code=403, detail="CSRF verification failed")
        return record

    def require_admin_any_csrf(
        record: AdminSessionRecord = Depends(require_admin_any),
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> AdminSessionRecord:
        if not sessions.verify_csrf(record, csrf_token):
            raise HTTPException(status_code=403, detail="CSRF verification failed")
        return record

    def require_recent_admin_csrf(
        record: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> AdminSessionRecord:
        if sessions.requires_reauth(record):
            raise HTTPException(
                status_code=403,
                detail="recent authentication is required",
            )
        return record

    @app.post("/api/admin/session")
    def admin_login(body: AdminLoginBody, request: Request, response: Response) -> dict[str, Any]:
        _enforce_admin_network(request)
        attempt_key = _subject_attempt_key(body.subject)
        client_key = _client_attempt_key(request)
        if not login_client_limiter.consume(
            client_key
        ) or not login_subject_limiter.consume(attempt_key):
            raise HTTPException(status_code=429, detail="too many authentication attempts")
        subject = sessions.verify_password(body.subject, body.password)
        if subject is None:
            raise HTTPException(status_code=401, detail="invalid admin credentials")
        login_subject_limiter.clear(attempt_key)
        result = complete_admin_login(
            sessions=sessions,
            mfa=mfa,
            subject=subject,
            totp=body.totp,
            recovery_code=body.recovery_code,
            response=response,
            cookie_name=settings.cookie_name,
            secure_cookie=settings.secure_cookie,
            session_ttl_seconds=settings.session_ttl_seconds,
            factor_limiter=login_factor_limiter,
            factor_key=attempt_key,
        )
        login_client_limiter.clear(client_key)
        return result

    @app.get("/api/admin/session")
    def admin_session(
        record: AdminSessionRecord = Depends(require_admin_any),
    ) -> dict[str, Any]:
        return {
            "subject": record.subject,
            "expires_at": record.expires_at.isoformat().replace("+00:00", "Z"),
            "assurance": record.assurance.value,
        }

    @app.delete("/api/admin/session")
    def admin_logout(
        request: Request,
        response: Response,
        admin_session_record: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> dict[str, str]:
        sessions.revoke(request.cookies.get(settings.cookie_name))
        response.delete_cookie(
            settings.cookie_name,
            path="/api/admin",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return {"status": "signed_out"}

    @app.get("/api/releases")
    def release_catalog(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict[str, Any]:
        records = service.list_catalog(limit=limit)
        return {
            "releases": [
                {
                    **_release_payload(record.release),
                    "artifacts": [
                        {
                            "id": artifact.id,
                            "platform": artifact.platform,
                            "architecture": artifact.architecture,
                            "artifact_kind": artifact.artifact_kind,
                            "build": artifact.build,
                            "byte_size": artifact.byte_size,
                            "sha256": artifact.sha256,
                            "signature_verified_at": artifact.signature_verified_at,
                            "verification_key_id": artifact.verification_key_id,
                        }
                        for artifact in record.artifacts
                    ],
                }
                for record in records
            ]
        }

    @app.post("/api/admin/accounts", status_code=201)
    def admin_create_account(
        body: AdminAccountCreateBody,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, str]:
        account = commerce.create_account(body.external_subject)
        return {
            "account_id": account.id,
            "external_subject": account.external_subject,
            "created_at": account.created_at,
        }

    @app.get("/api/admin/accounts")
    def admin_accounts(
        admin_session_record: AdminSessionRecord = Depends(require_admin),
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> dict[str, Any]:
        page = service.list_admin_accounts(limit=limit, offset=offset)
        return {
            "accounts": [
                {
                    "id": record.account.id,
                    "external_subject": record.account.external_subject,
                    "created_at": record.account.created_at,
                    "license_count": record.license_count,
                    "active_device_count": record.active_device_count,
                }
                for record in page.records
            ],
            "pagination": _admin_pagination_payload(page),
        }

    @app.post("/api/admin/accounts/{account_id}/licenses", status_code=201)
    def admin_issue_license(
        account_id: str,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, str]:
        license_record = commerce.issue_license(account_id)
        return {
            "license_id": license_record.id,
            "account_id": license_record.account_id,
            "plan_code": license_record.plan_code,
            "created_at": license_record.created_at,
        }

    @app.post("/api/admin/licenses/{license_id}/devices", status_code=201)
    def admin_bind_initial_device(
        license_id: str,
        body: AdminDeviceBindBody,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        binding = commerce.activate_device(
            license_id,
            body.device_key_fingerprint,
            platform=body.platform,
            architecture=body.architecture,
            device_label=body.device_label,
        )
        return {
            "device_binding_id": binding.id,
            "license_id": binding.license_id,
            "device_key_fingerprint": binding.device_key_fingerprint,
            "platform": binding.platform,
            "architecture": binding.architecture,
            "device_label": binding.device_label,
            "activated_at": binding.activated_at,
        }

    @app.get("/api/admin/licenses")
    def admin_licenses(
        admin_session_record: AdminSessionRecord = Depends(require_admin),
        account_id: Annotated[
            str | None,
            Query(min_length=3, max_length=128),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        entitlements_limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        page = service.list_admin_licenses(
            account_id=account_id,
            limit=limit,
            offset=offset,
            entitlements_limit=entitlements_limit,
        )
        return {
            "licenses": [
                {
                    "id": record.license.id,
                    "account_id": record.license.account_id,
                    "account_external_subject": record.account_external_subject,
                    "plan_code": record.license.plan_code,
                    "created_at": record.license.created_at,
                    "active_device": (
                        None
                        if record.active_device is None
                        else _admin_device_payload(record.active_device)
                    ),
                    "entitlements": [
                        _admin_entitlement_payload(item)
                        for item in record.entitlements
                    ],
                    "entitlement_count": record.entitlement_count,
                    "entitlements_truncated": record.entitlements_truncated,
                }
                for record in page.records
            ],
            "pagination": _admin_pagination_payload(page),
        }

    @app.post(
        "/api/admin/licenses/{license_id}/devices/replace",
        status_code=201,
    )
    def admin_replace_device(
        license_id: str,
        body: AdminDeviceReplaceBody,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        binding = commerce.replace_device(
            license_id,
            current_device_key_fingerprint=(
                body.current_device_key_fingerprint
            ),
            new_device_key_fingerprint=body.new_device_key_fingerprint,
            new_platform=body.new_platform,
            new_architecture=body.new_architecture,
            new_device_label=body.new_device_label,
            replacement_reason=body.replacement_reason,
        )
        return {
            "status": "replaced",
            "device_binding_id": binding.id,
            "license_id": binding.license_id,
            "device_key_fingerprint": binding.device_key_fingerprint,
            "platform": binding.platform,
            "architecture": binding.architecture,
            "device_label": binding.device_label,
            "activated_at": binding.activated_at,
        }

    @app.post("/api/admin/releases", status_code=201)
    def create_release(
        body: ReleaseCreateBody,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        return _release_payload(
            service.create_release(
                version=body.version,
                price_minor=body.price_minor,
                currency=body.currency,
                features_en=body.features_en,
                features_ru=body.features_ru,
                fixes_en=body.fixes_en,
                fixes_ru=body.fixes_ru,
            )
        )

    @app.get("/api/admin/releases")
    def admin_releases(
        admin_session_record: AdminSessionRecord = Depends(require_admin),
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> dict[str, Any]:
        page = service.list_admin_releases(limit=limit, offset=offset)
        return {
            "releases": [
                {
                    **_release_payload(record.release),
                    "artifact_count": record.artifact_count,
                }
                for record in page.records
            ],
            "pagination": _admin_pagination_payload(page),
        }

    @app.get("/api/admin/releases/{release_id}")
    def admin_release_detail(
        release_id: str,
        admin_session_record: AdminSessionRecord = Depends(require_admin),
    ) -> dict[str, Any]:
        detail = service.get_release_detail(release_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="release not found")
        return {
            **_release_payload(detail.release),
            "artifacts": [
                _artifact_payload(item, admin=True) for item in detail.artifacts
            ],
        }

    @app.post("/api/admin/releases/{release_id}/artifacts", status_code=201)
    def add_artifact(
        release_id: str,
        body: ArtifactCreateBody,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        artifact = service.add_release_artifact(
            release_id,
            platform=body.platform,
            architecture=body.architecture,
            artifact_kind=body.artifact_kind,
            build=body.build,
            sha256=body.sha256,
            byte_size=body.byte_size,
            storage_key=body.storage_key,
            signature=body.signature,
            signing_key_id=body.signing_key_id,
            compatible_source_versions=body.compatible_source_versions,
        )
        return _artifact_payload(artifact, admin=True)

    @app.post("/api/admin/releases/{release_id}/publish")
    def publish_release(
        release_id: str,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        return _release_payload(service.publish_release(release_id))

    @app.get("/api/admin/payments")
    def admin_payments(
        admin_session_record: AdminSessionRecord = Depends(require_admin),
        state: PaymentState | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        records = service.list_payments(state=state, limit=limit)
        return {"payments": [_payment_payload(item, admin=True) for item in records]}

    @app.post("/api/admin/payments/{payment_id}/review")
    def review_payment(
        payment_id: str,
        admin: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> dict[str, Any]:
        return _payment_payload(
            service.start_payment_review(payment_id, admin_subject=admin.subject),
            admin=True,
        )

    @app.post("/api/admin/payments/{payment_id}/approve")
    def approve_payment(
        payment_id: str,
        admin: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        result = service.approve_payment(payment_id, admin_subject=admin.subject)
        return {
            "payment": _payment_payload(result.payment, admin=True),
            "entitlement": {
                "id": result.entitlement.id,
                "version": result.entitlement.version,
                "granted_at": result.entitlement.granted_at,
            },
            "idempotent": result.idempotent,
        }

    @app.post("/api/admin/payments/{payment_id}/reject")
    def reject_payment(
        payment_id: str,
        body: PaymentRejectBody,
        admin: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        payment = service.reject_payment(
            payment_id,
            admin_subject=admin.subject,
            reason=body.reason,
        )
        return _payment_payload(payment, admin=True)

    @app.get("/api/admin/payments/{payment_id}/evidence")
    def payment_evidence(
        payment_id: str,
        admin_session_record: AdminSessionRecord = Depends(require_admin),
    ) -> Response:
        evidence = service.read_payment_evidence(payment_id)
        if evidence is None:
            raise HTTPException(status_code=404, detail="payment not found")
        return Response(
            evidence.content,
            media_type=evidence.content_type,
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/admin/audit")
    def admin_audit(
        admin_session_record: AdminSessionRecord = Depends(require_admin),
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return {
            "events": [
                {
                    "id": event.id,
                    "payment_id": event.payment_id,
                    "actor_admin_subject": event.actor_admin_subject,
                    "decision": event.decision.value,
                    "reason": event.reason,
                    "occurred_at": event.occurred_at,
                }
                for event in service.list_audit(limit=limit)
            ]
        }

    @app.post(
        "/api/admin/licenses/{license_id}/versions/{version}/activation-credentials",
        status_code=201,
    )
    def issue_activation_credential(
        license_id: str,
        version: str,
        admin_session_record: AdminSessionRecord = Depends(require_recent_admin_csrf),
    ) -> dict[str, Any]:
        issued = activation.issue_activation_credential(
            license_id=license_id,
            version=version,
        )
        return {
            "credential_id": issued.credential_id,
            "license_id": issued.license_id,
            "version": issued.version,
            "license_key": issued.license_key,
            "issued_at": issued.issued_at,
            "expires_at": issued.expires_at,
        }

    @app.post("/v1/client/activation/challenge")
    def client_activation_challenge(
        body: ActivationChallengeBody,
    ) -> dict[str, str]:
        issued = activation.create_activation_challenge(
            product_id=body.product_id,
            license_key=body.license_key,
            device_key_fingerprint=body.device_key_fingerprint,
            device_public_key=body.device_public_key,
            version=body.version,
            platform=body.platform,
            architecture=body.architecture,
        )
        return {
            "challenge_id": issued.challenge_id,
            "challenge_nonce": issued.challenge_nonce,
        }

    @app.post("/v1/client/activation/complete")
    def client_activation_complete(
        body: ActivationCompleteBody,
    ) -> dict[str, str]:
        completed = activation.complete_activation(
            product_id=body.product_id,
            challenge_id=body.challenge_id,
            challenge_nonce=body.challenge_nonce,
            device_key_fingerprint=body.device_key_fingerprint,
            device_public_key=body.device_public_key,
            challenge_signature=body.challenge_signature,
            version=body.version,
            platform=body.platform,
            architecture=body.architecture,
        )
        return {
            "license_id": completed.license_id,
            "entitlement_certificate": completed.entitlement_certificate,
        }

    @app.post("/v1/client/updates/check")
    def client_update_check(
        body: UpdateCheckBody,
        device_grant: Annotated[
            str | None, Header(alias="X-Device-Grant")
        ] = None,
    ) -> dict[str, Any]:
        if body.product_id != PRODUCT_ID:
            raise HTTPException(status_code=400, detail="product is invalid")
        artifact = reads.find_update_candidate(
            platform=body.platform,
            architecture=body.architecture,
            installed_version=body.installed_version,
            installed_build=body.installed_build,
        )
        if artifact is None:
            return {"state": "current"}
        release = reads.get_release(artifact.release_id)
        if release is None:
            raise BackendServiceNotAvailableError(
                "Update release metadata is unavailable."
            )
        release_info = _release_info_payload(
            release,
            reads.list_release_artifacts(release.id),
        )
        manifest = release_manifest_envelope(artifact)
        purchase_response = {
            "state": "purchase_required",
            "manifest": manifest,
            "artifact_id": artifact.id,
            "release_id": artifact.release_id,
            "release_info": release_info,
        }
        if device_grant is None:
            return purchase_response
        verified = grants.consume(
            device_grant,
            license_id=body.license_id,
            action=DeviceChallengeAction.AUTHORIZE_INSTALL,
            resource_id=artifact.id,
        )
        if verified is None:
            raise HTTPException(status_code=401, detail="valid device grant required")
        authorization = commerce.authorize_install(
            body.license_id,
            device_principal=verified.device_principal,
            artifact_id=artifact.id,
            install_mode=InstallMode.UPDATE,
            source_version=body.installed_version,
            source_build=body.installed_build,
        )
        if not authorization.allowed:
            return {
                **purchase_response,
                "payment_instructions": _payment_instructions_payload(
                    payment_configuration
                ),
            }
        certificate = activation.issue_entitlement_certificate(
            license_id=body.license_id,
            device_key_fingerprint=(
                verified.device_principal.device_key_fingerprint
            ),
            version=artifact.identity.version,
        )
        download_grant = artifact_grants.issue(artifact)
        return {
            "state": "entitled",
            "manifest": manifest,
            "artifact_id": artifact.id,
            "release_id": artifact.release_id,
            "release_info": release_info,
            "download_path": f"/v1/client/updates/download/{artifact.id}",
            # Keep the short-lived credential out of the URL. Request targets
            # are routinely captured by reverse proxies, browser history and
            # infrastructure error logs; a dedicated header is not.
            "download_grant": download_grant.token,
            "entitlement_certificate": certificate,
        }

    @app.get("/v1/client/updates/download/{artifact_id}")
    def client_update_download(
        artifact_id: str,
        download_grant: Annotated[
            str | None,
            Header(alias="X-Artifact-Grant"),
        ] = None,
    ) -> Response:
        artifact = artifact_grants.consume(
            download_grant,
            artifact_id=artifact_id,
        )
        if artifact is None:
            raise HTTPException(status_code=401, detail="download grant is invalid")
        stream = release_artifact_store.open_verified_release_artifact(
            storage_key=artifact.storage_key,
            expected_sha256=artifact.sha256,
            expected_byte_size=artifact.byte_size,
        )
        if (
            not isinstance(stream, VerifiedReleaseArtifactStream)
            or stream.byte_size != artifact.byte_size
            or not hmac.compare_digest(
                stream.sha256,
                artifact.sha256,
            )
        ):
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            raise BackendServiceNotAvailableError(
                "Release artifact integrity verification failed."
            )
        try:
            return StreamingResponse(
                _iter_verified_artifact(stream),
                media_type="application/octet-stream",
                headers={
                    "Content-Length": str(stream.byte_size),
                    "Content-Disposition": (
                        f'attachment; filename="jarvis-{artifact.sha256}.package"'
                    )
                },
                background=BackgroundTask(stream.close),
            )
        except BaseException:
            stream.close()
            raise

    @app.post("/api/purchases/challenges", status_code=201)
    def issue_initial_purchase_challenge(
        body: InitialPurchaseChallengeBody,
        request: Request,
    ) -> dict[str, Any]:
        if body.product_id != PRODUCT_ID:
            raise HTTPException(status_code=400, detail="product is invalid")
        if not initial_purchase_client_limiter.consume(
            _client_attempt_key(request)
        ):
            raise HTTPException(status_code=429, detail="too many purchase requests")
        limiter_key = _attempt_key(
            request,
            f"initial|{body.purchase_id}|{body.version}",
        )
        if not initial_purchase_limiter.consume(limiter_key):
            raise HTTPException(status_code=429, detail="too many purchase requests")
        try:
            platform = normalize_target_platform(body.platform)
            architecture = normalize_target_architecture(body.architecture)
        except ValidationError:
            raise HTTPException(
                status_code=400,
                detail="purchase target is invalid",
            ) from None
        release = reads.get_release_by_version(body.version)
        if release is None or release.state is not ReleaseState.PUBLISHED:
            raise HTTPException(status_code=404, detail="release is not available")
        artifacts = tuple(reads.list_release_artifacts(release.id))
        if not _has_verified_initial_target(
            artifacts,
            platform=platform,
            architecture=architecture,
        ):
            raise HTTPException(
                status_code=409,
                detail="initial purchase target is not available",
            )
        result = initial_purchases.issue_challenge(
            purchase_id=body.purchase_id,
            release_id=release.id,
            device_key_fingerprint=body.device_key_fingerprint,
            platform=platform,
            architecture=architecture,
        )
        if not result.ok or result.challenge is None:
            raise HTTPException(
                status_code=_challenge_error_status(result.status),
                detail="initial purchase challenge could not be issued",
            )
        issued = result.challenge
        return {
            "challenge_id": issued.id,
            "challenge_nonce": issued.challenge_nonce,
            "purchase_id": body.purchase_id,
            "release_id": release.id,
            "version": release.version,
            "issued_at": issued.issued_at,
            "expires_at": issued.expires_at,
        }

    @app.post("/api/purchases/challenges/{challenge_id}/verify")
    def verify_initial_purchase_challenge(
        challenge_id: str,
        body: InitialPurchaseVerifyBody,
    ) -> dict[str, Any]:
        result = initial_purchases.verify_and_issue_grant(
            challenge_id=challenge_id,
            challenge_nonce=body.challenge_nonce,
            public_key_base64=body.public_key_base64,
            signature_base64=body.signature_base64,
        )
        if not result.ok or result.grant is None:
            raise HTTPException(
                status_code=_challenge_error_status(result.status),
                detail="initial purchase proof could not be verified",
            )
        grant = result.grant
        verified = grant.verified
        release = reads.get_release(verified.release_id)
        if release is None or release.state is not ReleaseState.PUBLISHED:
            raise HTTPException(status_code=409, detail="release is not available")
        artifacts = tuple(reads.list_release_artifacts(release.id))
        if not _has_verified_initial_target(
            artifacts,
            platform=verified.device_principal.platform,
            architecture=verified.device_principal.architecture,
        ):
            raise HTTPException(
                status_code=409,
                detail="initial purchase target is not available",
            )
        return {
            "purchase_grant": grant.token,
            "purchase_id": verified.purchase_id,
            "release_id": release.id,
            "expires_at": grant.expires_at,
            "release_info": _release_info_payload(release, artifacts),
            "payment_instructions": _payment_instructions_payload(
                payment_configuration
            ),
        }

    @app.post(
        "/api/purchases/{purchase_id}/releases/{release_id}/payments",
        status_code=201,
    )
    async def submit_initial_purchase_payment(
        request: Request,
        purchase_id: str,
        release_id: str,
        purchase_grant: Annotated[str, Header(alias="X-Purchase-Grant")],
        paid_at: Annotated[str, Form(min_length=20, max_length=40)],
        client_submission_id: Annotated[
            str,
            Form(
                min_length=3,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}$",
            ),
        ],
        file: Annotated[UploadFile, File()],
        supersedes_payment_id: Annotated[
            str | None,
            Form(
                min_length=3,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        reservation = getattr(
            request.state,
            _RESERVED_INITIAL_PURCHASE_GRANT_STATE,
            None,
        )
        verified = (
            reservation.verified
            if isinstance(reservation, InitialPurchaseGrantReservation)
            else None
        )
        if (
            not isinstance(purchase_grant, str)
            or verified is None
            or verified.purchase_id != purchase_id
            or verified.release_id != release_id
            or not verified.device_principal.proof_verified
        ):
            await file.close()
            raise HTTPException(
                status_code=401,
                detail="valid purchase grant required",
            )
        content_type = file.content_type
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            await file.close()
            raise HTTPException(
                status_code=400,
                detail="payment evidence type is invalid",
            )
        release = reads.get_release(release_id)
        if release is None or release.state is not ReleaseState.PUBLISHED:
            await file.close()
            raise HTTPException(status_code=409, detail="release is not available")
        content = await _read_bounded_upload(file)
        try:
            initial = service.submit_initial_purchase_evidence(
                purchase_id=verified.purchase_id,
                release_id=verified.release_id,
                device_principal=verified.device_principal,
                content=content,
                content_type=content_type,
                paid_at=paid_at,
                client_submission_id=client_submission_id,
                supersedes_payment_id=supersedes_payment_id,
                now=now(),
            )
        except (ConflictError, InvalidTransitionError, NotFoundError):
            if not initial_purchases.commit_grant(reservation):
                raise BackendServiceNotAvailableError(
                    "Initial purchase authorization finalization failed."
                ) from None
            raise
        if not initial_purchases.commit_grant(reservation):
            raise BackendServiceNotAvailableError(
                "Initial purchase authorization finalization failed."
            )
        payment = _payment_payload(initial.payment, admin=False)
        return {
            **payment,
            "license_id": initial.license.id,
            "purchase_id": verified.purchase_id,
            "version": release.version,
            "idempotent": initial.idempotent,
        }

    @app.post("/api/device-challenges", status_code=201)
    def issue_device_challenge(body: DeviceChallengeIssueBody, request: Request) -> dict[str, Any]:
        if body.action not in {
            DeviceChallengeAction.AUTHORIZE_INSTALL,
            DeviceChallengeAction.SUBMIT_PAYMENT,
            DeviceChallengeAction.FETCH_ENTITLEMENT,
        }:
            raise HTTPException(status_code=400, detail="challenge action is not exposed")
        limiter_key = _attempt_key(
            request,
            f"{body.license_id}|{body.action.value}|{body.resource_id}",
        )
        if not challenge_limiter.allowed(limiter_key):
            raise HTTPException(status_code=429, detail="too many challenge requests")
        challenge_limiter.record_failure(limiter_key)
        result = service.issue_device_challenge(
            license_id=body.license_id,
            device_key_fingerprint=body.device_key_fingerprint,
            action=body.action,
            resource_id=body.resource_id,
        )
        if result.status != STATUS_SUCCESS or result.issued is None:
            raise HTTPException(
                status_code=_challenge_error_status(result.status),
                detail="device challenge could not be issued",
            )
        issued = result.issued
        return {
            "challenge_id": issued.id,
            "challenge_nonce": issued.challenge_nonce,
            "action": issued.action.value,
            "resource_id": issued.resource_id,
            "issued_at": issued.issued_at,
            "expires_at": issued.expires_at,
        }

    @app.post("/api/device-challenges/{challenge_id}/verify")
    def verify_device_challenge(
        challenge_id: str,
        body: DeviceChallengeVerifyBody,
    ) -> dict[str, Any]:
        result = service.verify_device_challenge(
            challenge_id=challenge_id,
            challenge_nonce=body.challenge_nonce,
            public_key_base64=body.public_key_base64,
            signature_base64=body.signature_base64,
        )
        if result.status != STATUS_SUCCESS or result.verified is None:
            raise HTTPException(
                status_code=_challenge_error_status(result.status),
                detail="device challenge verification failed",
            )
        grant = grants.issue(result.verified)
        return {
            "device_grant": grant.token,
            "action": result.verified.action.value,
            "resource_id": result.verified.resource_id,
            "expires_at": grant.expires_at,
        }

    @app.post(
        "/api/customer/licenses/{license_id}/releases/{release_id}/payments",
        status_code=201,
    )
    async def submit_payment(
        request: Request,
        license_id: str,
        release_id: str,
        device_grant: Annotated[str, Header(alias="X-Device-Grant")],
        paid_at: Annotated[str, Form(min_length=20, max_length=40)],
        client_submission_id: Annotated[
            str,
            Form(
                min_length=3,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}$",
            ),
        ],
        file: Annotated[UploadFile, File()],
        supersedes_payment_id: Annotated[
            str | None,
            Form(
                min_length=3,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        reservation = getattr(request.state, _RESERVED_PAYMENT_GRANT_STATE, None)
        verified = (
            reservation.verified
            if isinstance(reservation, ReservedDeviceGrant)
            else None
        )
        if (
            not isinstance(device_grant, str)
            or not isinstance(verified, VerifiedDeviceChallenge)
            or verified.license_id != license_id
            or verified.action is not DeviceChallengeAction.SUBMIT_PAYMENT
            or verified.resource_id != release_id
            or not verified.device_principal.proof_verified
        ):
            await file.close()
            raise HTTPException(status_code=401, detail="valid device grant required")
        content_type = file.content_type
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            await file.close()
            raise HTTPException(status_code=400, detail="payment evidence type is invalid")
        content = await _read_bounded_upload(file)
        try:
            payment = service.submit_payment_evidence(
                license_id,
                release_id,
                content=content,
                content_type=content_type,
                paid_at=paid_at,
                client_submission_id=client_submission_id,
                supersedes_payment_id=supersedes_payment_id,
                now=now(),
            )
        except (ConflictError, InvalidTransitionError, NotFoundError):
            if not grants.commit(reservation):
                raise BackendServiceNotAvailableError(
                    "Payment authorization finalization failed."
                ) from None
            raise
        if not grants.commit(reservation):
            raise BackendServiceNotAvailableError(
                "Payment authorization finalization failed."
            )
        return _payment_payload(payment, admin=False)

    @app.get("/api/customer/licenses/{license_id}/versions/{version}/status")
    def customer_status(
        license_id: str,
        version: str,
        device_grant: Annotated[str, Header(alias="X-Device-Grant")],
    ) -> dict[str, Any]:
        verified = grants.consume(
            device_grant,
            license_id=license_id,
            action=DeviceChallengeAction.FETCH_ENTITLEMENT,
            resource_id=version,
        )
        if verified is None:
            raise HTTPException(status_code=401, detail="valid device grant required")
        customer = service.customer_version_status(license_id, version)
        if customer is None:
            raise HTTPException(status_code=404, detail="version not found")
        entitlement_certificate = None
        if customer.entitled:
            entitlement_certificate = activation.issue_entitlement_certificate(
                license_id=license_id,
                device_key_fingerprint=(
                    verified.device_principal.device_key_fingerprint
                ),
                version=customer.version,
            )
        return {
            "version": customer.version,
            "release_id": customer.release_id,
            "release_state": customer.release_state,
            "price_minor": customer.price_minor,
            "currency": customer.currency,
            "features": {
                "en": customer.features_en,
                "ru": customer.features_ru,
            },
            "fixes": {
                "en": customer.fixes_en,
                "ru": customer.fixes_ru,
            },
            "entitled": customer.entitled,
            "entitlement_granted_at": customer.entitlement_granted_at,
            "payment_id": customer.payment_id,
            "payment_state": customer.payment_state,
            "rejection_reason": customer.rejection_reason,
            "active_device_bound": customer.active_device_bound,
            "entitlement_certificate": entitlement_certificate,
        }

    if mfa is not None:
        register_admin_security_routes(
            app,
            sessions=sessions,
            mfa=mfa,
            cookie_name=settings.cookie_name,
            secure_cookie=settings.secure_cookie,
            session_ttl_seconds=settings.session_ttl_seconds,
            require_admin_any=require_admin_any,
            require_admin=require_admin,
            require_admin_csrf=require_admin_csrf,
            require_admin_any_csrf=require_admin_any_csrf,
            attempt_key=_attempt_key,
            enrollment_limiter=mfa_enrollment_limiter,
            stepup_limiter=mfa_stepup_limiter,
            password_limiter=admin_password_limiter,
        )

    mount_admin_web(app)

    policy = (
        OperationalPolicy() if operational_policy is None else operational_policy
    )
    if not isinstance(policy, OperationalPolicy):
        raise BackendConfigurationError("operational policy is invalid")
    if metrics is not None and not isinstance(metrics, MetricsRegistry):
        raise BackendConfigurationError("metrics registry is invalid")
    operational_metrics: MetricsRegistry = metrics or NullMetrics()
    app.state.operational_metrics = operational_metrics

    def _readiness_probe() -> ReadinessResult:
        checks: dict[str, bool] = {}
        try:
            reads.list_published_releases(limit=1)
            checks["database"] = True
        except Exception:  # noqa: BLE001 - readiness must not raise
            checks["database"] = False
        return ReadinessResult(all(checks.values()), checks)

    # Install as the OUTERMOST layer so health/readiness probes bypass the
    # trusted-host and HTTPS checks, and the forwarded scheme is trusted only
    # from a configured proxy.
    app.add_middleware(
        OperationalMiddleware,
        policy=policy,
        readiness_probe=_readiness_probe,
        metrics=operational_metrics,
        logger=request_logger,
        proxy_config=proxy_config,
    )
    return app


__all__ = [
    "AdminLoginBody",
    "ArtifactCreateBody",
    "DeviceChallengeIssueBody",
    "DeviceChallengeVerifyBody",
    "InitialPurchaseChallengeBody",
    "InitialPurchaseVerifyBody",
    "PaymentRejectBody",
    "ReleaseCreateBody",
    "create_product_backend_app",
]

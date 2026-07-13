"""Client activation flow using a per-install device proof and signed cache."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from core.device_identity import (
    STATUS_NOT_AVAILABLE as IDENTITY_NOT_AVAILABLE,
    STATUS_SUCCESS as IDENTITY_SUCCESS,
    DeviceIdentityManager,
)
from core.entitlement_cache import (
    STATUS_NOT_AVAILABLE as CACHE_NOT_AVAILABLE,
    SignedEntitlementCache,
)
from core.entitlement_certificate import EntitlementCertificate
from core.product_api_client import ApiErrorCode, ProductApiClient, ProductApiError
from core.product_version import (
    PRODUCT_ID,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)


STATUS_SUCCESS: Final = "success"
STATUS_INVALID: Final = "invalid"
STATUS_REJECTED: Final = "rejected"
STATUS_DEVICE_MISMATCH: Final = "device_mismatch"
STATUS_OFFLINE: Final = "offline"
STATUS_SERVER_UNAVAILABLE: Final = "server_unavailable"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

ACTIVATION_CHALLENGE_PATH: Final = "/v1/client/activation/challenge"
ACTIVATION_COMPLETE_PATH: Final = "/v1/client/activation/complete"

_OPAQUE_ID_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])")
_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_INVALID,
        STATUS_REJECTED,
        STATUS_DEVICE_MISMATCH,
        STATUS_OFFLINE,
        STATUS_SERVER_UNAVAILABLE,
        STATUS_NOT_AVAILABLE,
        STATUS_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: str
    message: str = field(repr=False)
    license_id: str | None = field(default=None, repr=False)
    certificate: EntitlementCertificate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported activation status")
        has_claims = self.license_id is not None and self.certificate is not None
        if (self.status == STATUS_SUCCESS) != has_claims:
            raise ValueError("only successful activation may carry claims")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        claims = "verified" if self.certificate is not None else "none"
        return f"ActivationResult(status={self.status!r}, claims={claims!r})"


def _result(
    status: str,
    message: str,
    license_id: str | None = None,
    certificate: EntitlementCertificate | None = None,
) -> ActivationResult:
    return ActivationResult(status, message, license_id, certificate)


def _license_key(value: object) -> str:
    if (
        type(value) is not str
        or not 8 <= len(value) <= 256
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("license key is invalid")
    return value


def _target(value: object, normalizer) -> str:
    if type(value) is not str:
        raise ValueError("target is invalid")
    normalized = normalizer(value)
    if normalized == "unknown":
        raise ValueError("target is unsupported")
    return normalized


def _api_failure(error: ProductApiError) -> ActivationResult:
    if error.code is ApiErrorCode.NETWORK_UNAVAILABLE:
        return _result(STATUS_OFFLINE, "Activation requires a network connection.")
    if error.code is ApiErrorCode.SERVER_UNAVAILABLE:
        return _result(STATUS_SERVER_UNAVAILABLE, "Activation server is unavailable.")
    if error.code is ApiErrorCode.DEVICE_MISMATCH:
        return _result(
            STATUS_DEVICE_MISMATCH,
            "This license may already be bound to another device.",
        )
    if error.code in {
        ApiErrorCode.CONFLICT,
        ApiErrorCode.UNAUTHORIZED,
        ApiErrorCode.NOT_FOUND,
    }:
        return _result(STATUS_REJECTED, "Activation was not approved.")
    if error.code in {
        ApiErrorCode.RESPONSE_INVALID,
        ApiErrorCode.RESPONSE_TOO_LARGE,
    }:
        return _result(STATUS_INVALID, "Activation response is invalid.")
    return _result(STATUS_FAILED, "Activation failed.")


class ProductActivationService:
    """Perform the two-request activation protocol and cache verified authority."""

    __slots__ = ("_api", "_cache", "_identity_manager")

    def __init__(
        self,
        api_client: ProductApiClient,
        identity_manager: DeviceIdentityManager,
        entitlement_cache: SignedEntitlementCache,
    ) -> None:
        if not isinstance(api_client, ProductApiClient):
            raise TypeError("api_client must be a ProductApiClient")
        if not isinstance(identity_manager, DeviceIdentityManager):
            raise TypeError("identity_manager must be a DeviceIdentityManager")
        if not isinstance(entitlement_cache, SignedEntitlementCache):
            raise TypeError("entitlement_cache must be a SignedEntitlementCache")
        self._api = api_client
        self._identity_manager = identity_manager
        self._cache = entitlement_cache

    def __repr__(self) -> str:
        return "ProductActivationService(api=<configured>, identity=<secure>)"

    def activate(
        self,
        license_key: str,
        *,
        version: str | SemanticVersion,
        platform: str,
        architecture: str,
    ) -> ActivationResult:
        try:
            secret = _license_key(license_key)
            version_value = (
                version
                if isinstance(version, SemanticVersion)
                else SemanticVersion.parse(version)
            )
            platform_value = _target(platform, normalize_platform)
            architecture_value = _target(architecture, normalize_architecture)
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Activation request is invalid.")

        identity_result = self._identity_manager.get_or_create()
        if identity_result.status == IDENTITY_NOT_AVAILABLE:
            return _result(
                STATUS_NOT_AVAILABLE,
                "Secure device identity is not available.",
            )
        if identity_result.status != IDENTITY_SUCCESS or identity_result.identity is None:
            return _result(STATUS_FAILED, "Secure device identity could not be loaded.")
        identity = identity_result.identity

        try:
            challenge = self._api.request_json(
                "POST",
                ACTIVATION_CHALLENGE_PATH,
                payload={
                    "product_id": PRODUCT_ID,
                    "license_key": secret,
                    "device_key_fingerprint": identity.fingerprint,
                    "device_public_key": identity.public_key_base64,
                    "version": str(version_value),
                    "platform": platform_value,
                    "architecture": architecture_value,
                },
            )
            if frozenset(challenge) != {"challenge_id", "challenge_nonce"}:
                return _result(STATUS_INVALID, "Activation challenge is invalid.")
            challenge_id = challenge["challenge_id"]
            challenge_nonce = challenge["challenge_nonce"]
            if (
                type(challenge_id) is not str
                or _OPAQUE_ID_RE.fullmatch(challenge_id) is None
                or type(challenge_nonce) is not str
            ):
                return _result(STATUS_INVALID, "Activation challenge is invalid.")
            signature = identity.sign_challenge(challenge_nonce)
            completed = self._api.request_json(
                "POST",
                ACTIVATION_COMPLETE_PATH,
                payload={
                    "product_id": PRODUCT_ID,
                    "challenge_id": challenge_id,
                    "challenge_nonce": challenge_nonce,
                    "device_key_fingerprint": identity.fingerprint,
                    "device_public_key": identity.public_key_base64,
                    "challenge_signature": signature,
                    "version": str(version_value),
                    "platform": platform_value,
                    "architecture": architecture_value,
                },
            )
        except ProductApiError as exc:
            return _api_failure(exc)
        except (TypeError, ValueError, RuntimeError):
            return _result(STATUS_INVALID, "Activation proof is invalid.")

        if frozenset(completed) != {"license_id", "entitlement_certificate"}:
            return _result(STATUS_INVALID, "Activation response is invalid.")
        license_id = completed["license_id"]
        certificate_input = completed["entitlement_certificate"]
        if (
            type(license_id) is not str
            or _OPAQUE_ID_RE.fullmatch(license_id) is None
            or type(certificate_input) is not str
        ):
            return _result(STATUS_INVALID, "Activation response is invalid.")
        cached = self._cache.store_verified(
            certificate_input,
            license_id=license_id,
            device_fingerprint=identity.fingerprint,
            version=version_value,
        )
        if cached.ok and cached.certificate is not None:
            return _result(
                STATUS_SUCCESS,
                "Activation completed and offline entitlement was verified.",
                license_id,
                cached.certificate,
            )
        if cached.status == CACHE_NOT_AVAILABLE:
            return _result(
                STATUS_NOT_AVAILABLE,
                "Offline entitlement storage is not available.",
            )
        return _result(STATUS_INVALID, "Signed entitlement could not be verified.")


__all__ = [
    "ACTIVATION_CHALLENGE_PATH",
    "ACTIVATION_COMPLETE_PATH",
    "STATUS_FAILED",
    "STATUS_DEVICE_MISMATCH",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_OFFLINE",
    "STATUS_REJECTED",
    "STATUS_SERVER_UNAVAILABLE",
    "STATUS_SUCCESS",
    "ActivationResult",
    "ProductActivationService",
]

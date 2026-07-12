"""Proof-bound manual payment submission and signed entitlement polling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from core.device_identity import (
    STATUS_NOT_AVAILABLE as IDENTITY_NOT_AVAILABLE,
    STATUS_SUCCESS as IDENTITY_SUCCESS,
    DeviceIdentity,
    DeviceIdentityManager,
)
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import EntitlementCertificate
from core.product_api_client import (
    MAX_MULTIPART_FILE_BYTES,
    ApiErrorCode,
    ProductApiClient,
    ProductApiError,
)
from core.product_version import SemanticVersion


STATUS_SUBMITTED: Final = "submitted"
STATUS_PURCHASE_REQUIRED: Final = "purchase_required"
STATUS_PENDING: Final = "pending"
STATUS_UNDER_REVIEW: Final = "under_review"
STATUS_REJECTED: Final = "rejected"
STATUS_ENTITLED: Final = "entitled"
STATUS_INVALID: Final = "invalid"
STATUS_OFFLINE: Final = "offline"
STATUS_SERVER_UNAVAILABLE: Final = "server_unavailable"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

DEVICE_CHALLENGE_PATH: Final = "/api/device-challenges"

_OPAQUE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{2,127}")
_UTC_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]{1,6})?Z"
)
_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUBMITTED,
        STATUS_PURCHASE_REQUIRED,
        STATUS_PENDING,
        STATUS_UNDER_REVIEW,
        STATUS_REJECTED,
        STATUS_ENTITLED,
        STATUS_INVALID,
        STATUS_OFFLINE,
        STATUS_SERVER_UNAVAILABLE,
        STATUS_NOT_AVAILABLE,
        STATUS_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    status: str
    message: str = field(repr=False)
    release_id: str | None = field(default=None, repr=False)
    version: str | None = None
    payment_id: str | None = field(default=None, repr=False)
    payment_state: str | None = None
    price_minor: int | None = None
    currency: str | None = None
    rejection_reason: str | None = field(default=None, repr=False)
    certificate: EntitlementCertificate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported purchase status")
        if (self.status == STATUS_ENTITLED) != (self.certificate is not None):
            raise ValueError("only entitled status may carry signed claims")

    @property
    def entitled(self) -> bool:
        return self.status == STATUS_ENTITLED and self.certificate is not None

    def __repr__(self) -> str:
        authority = "verified" if self.certificate is not None else "none"
        return f"PurchaseResult(status={self.status!r}, authority={authority!r})"


class _PurchaseFlowFailure(RuntimeError):
    def __init__(self, result: PurchaseResult) -> None:
        self.result = result
        super().__init__("purchase flow failed")


def _result(status: str, message: str, **kwargs: object) -> PurchaseResult:
    return PurchaseResult(status, message, **kwargs)


def _api_failure(error: ProductApiError) -> PurchaseResult:
    if error.code is ApiErrorCode.NETWORK_UNAVAILABLE:
        return _result(STATUS_OFFLINE, "Purchase service is offline.")
    if error.code is ApiErrorCode.SERVER_UNAVAILABLE:
        return _result(STATUS_SERVER_UNAVAILABLE, "Purchase server is unavailable.")
    if error.code in {
        ApiErrorCode.UNAUTHORIZED,
        ApiErrorCode.NOT_FOUND,
        ApiErrorCode.CONFLICT,
    }:
        return _result(STATUS_FAILED, "Verified device authorization was rejected.")
    if error.code in {
        ApiErrorCode.RESPONSE_INVALID,
        ApiErrorCode.RESPONSE_TOO_LARGE,
    }:
        return _result(STATUS_INVALID, "Purchase response is invalid.")
    return _result(STATUS_FAILED, "Purchase operation failed.")


def _utc_timestamp(value: object) -> str:
    if type(value) is not str or _UTC_RE.fullmatch(value) is None:
        raise ValueError("timestamp is invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return value


class ProductPurchaseService:
    """Submit private payment evidence; trust only a signed entitlement later."""

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
        return "ProductPurchaseService(api=<configured>, identity=<secure>)"

    def _device_grant(
        self,
        *,
        license_id: str,
        action: str,
        resource_id: str,
    ) -> tuple[DeviceIdentity, str]:
        identity_result = self._identity_manager.load()
        if identity_result.status == IDENTITY_NOT_AVAILABLE:
            raise _PurchaseFlowFailure(
                _result(STATUS_NOT_AVAILABLE, "Secure device identity is unavailable.")
            )
        if identity_result.status != IDENTITY_SUCCESS or identity_result.identity is None:
            raise _PurchaseFlowFailure(
                _result(STATUS_FAILED, "Activated device identity is required.")
            )
        identity = identity_result.identity
        try:
            challenge = self._api.request_json(
                "POST",
                DEVICE_CHALLENGE_PATH,
                payload={
                    "license_id": license_id,
                    "device_key_fingerprint": identity.fingerprint,
                    "action": action,
                    "resource_id": resource_id,
                },
            )
            if frozenset(challenge) != {
                "challenge_id",
                "challenge_nonce",
                "action",
                "resource_id",
                "issued_at",
                "expires_at",
            }:
                raise ValueError("challenge schema mismatch")
            challenge_id = challenge["challenge_id"]
            nonce = challenge["challenge_nonce"]
            if (
                type(challenge_id) is not str
                or _OPAQUE_ID_RE.fullmatch(challenge_id) is None
                or type(nonce) is not str
                or challenge["action"] != action
                or challenge["resource_id"] != resource_id
            ):
                raise ValueError("challenge context mismatch")
            signature = identity.sign_challenge(nonce)
            grant = self._api.request_json(
                "POST",
                f"{DEVICE_CHALLENGE_PATH}/{challenge_id}/verify",
                payload={
                    "challenge_nonce": nonce,
                    "public_key_base64": identity.public_key_base64,
                    "signature_base64": signature,
                },
            )
            if frozenset(grant) != {
                "device_grant",
                "action",
                "resource_id",
                "expires_at",
            }:
                raise ValueError("grant schema mismatch")
            token = grant["device_grant"]
            if (
                type(token) is not str
                or not 16 <= len(token) <= 4096
                or any(character in token for character in "\x00\r\n")
                or grant["action"] != action
                or grant["resource_id"] != resource_id
            ):
                raise ValueError("grant context mismatch")
            return identity, token
        except ProductApiError as exc:
            raise _PurchaseFlowFailure(_api_failure(exc)) from None
        except (TypeError, ValueError, RuntimeError):
            raise _PurchaseFlowFailure(
                _result(STATUS_INVALID, "Device proof response is invalid.")
            ) from None

    def submit_payment(
        self,
        *,
        license_id: str,
        release_id: str,
        paid_at: str,
        screenshot: bytes,
        content_type: str,
    ) -> PurchaseResult:
        try:
            if (
                type(license_id) is not str
                or _OPAQUE_ID_RE.fullmatch(license_id) is None
                or type(release_id) is not str
                or _OPAQUE_ID_RE.fullmatch(release_id) is None
                or type(screenshot) is not bytes
                or not 1 <= len(screenshot) <= MAX_MULTIPART_FILE_BYTES
                or content_type not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise ValueError("payment request is invalid")
            paid_at = _utc_timestamp(paid_at)
            _identity, grant = self._device_grant(
                license_id=license_id,
                action="submit_payment",
                resource_id=release_id,
            )
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
            }[content_type]
            response = self._api.request_multipart_json(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                fields={"paid_at": paid_at},
                file_field="file",
                filename=f"payment.{extension}",
                content_type=content_type,
                content=screenshot,
                headers={"X-Device-Grant": grant},
            )
        except _PurchaseFlowFailure as exc:
            return exc.result
        except ProductApiError as exc:
            return _api_failure(exc)
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Payment request is invalid.")
        expected = {
            "id",
            "release_id",
            "version",
            "amount_minor",
            "currency",
            "paid_at",
            "submitted_at",
            "state",
            "rejection_reason",
        }
        if (
            frozenset(response) != expected
            or response["release_id"] != release_id
            or response["state"] != "pending"
            or type(response["id"]) is not str
            or _OPAQUE_ID_RE.fullmatch(response["id"]) is None
            or response["rejection_reason"] is not None
        ):
            return _result(STATUS_INVALID, "Payment response is invalid.")
        return _result(
            STATUS_SUBMITTED,
            "Payment evidence was submitted for manual review.",
            release_id=release_id,
            version=response["version"],
            payment_id=response["id"],
            payment_state="pending",
            price_minor=response["amount_minor"],
            currency=response["currency"],
        )

    def poll_status(
        self,
        *,
        license_id: str,
        version: str | SemanticVersion,
    ) -> PurchaseResult:
        try:
            if type(license_id) is not str or _OPAQUE_ID_RE.fullmatch(license_id) is None:
                raise ValueError("license is invalid")
            version_value = (
                version
                if isinstance(version, SemanticVersion)
                else SemanticVersion.parse(version)
            )
            identity, grant = self._device_grant(
                license_id=license_id,
                action="fetch_entitlement",
                resource_id=str(version_value),
            )
            response = self._api.request_json(
                "GET",
                f"/api/customer/licenses/{license_id}/versions/{version_value}/status",
                headers={"X-Device-Grant": grant},
            )
        except _PurchaseFlowFailure as exc:
            return exc.result
        except ProductApiError as exc:
            return _api_failure(exc)
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Purchase status request is invalid.")
        expected = {
            "version",
            "release_id",
            "release_state",
            "price_minor",
            "currency",
            "entitled",
            "entitlement_granted_at",
            "payment_id",
            "payment_state",
            "rejection_reason",
            "active_device_bound",
            "entitlement_certificate",
        }
        if (
            frozenset(response) != expected
            or response["version"] != str(version_value)
            or type(response["entitled"]) is not bool
            or type(response["active_device_bound"]) is not bool
        ):
            return _result(STATUS_INVALID, "Purchase status response is invalid.")
        certificate_input = response["entitlement_certificate"]
        if response["entitled"]:
            if type(certificate_input) is not str:
                return _result(STATUS_INVALID, "Signed entitlement is missing.")
            cached = self._cache.store_verified(
                certificate_input,
                license_id=license_id,
                device_fingerprint=identity.fingerprint,
                version=version_value,
            )
            if not cached.ok or cached.certificate is None:
                return _result(STATUS_INVALID, "Signed entitlement is invalid.")
            return _result(
                STATUS_ENTITLED,
                "Exact-version entitlement was verified.",
                release_id=response["release_id"],
                version=str(version_value),
                payment_id=response["payment_id"],
                payment_state=response["payment_state"],
                price_minor=response["price_minor"],
                currency=response["currency"],
                certificate=cached.certificate,
            )
        if certificate_input is not None:
            return _result(STATUS_INVALID, "Unexpected entitlement certificate.")
        payment_state = response["payment_state"]
        status_map = {
            None: STATUS_PURCHASE_REQUIRED,
            "pending": STATUS_PENDING,
            "under_review": STATUS_UNDER_REVIEW,
            "rejected": STATUS_REJECTED,
        }
        status = status_map.get(payment_state)
        if status is None:
            return _result(STATUS_INVALID, "Purchase state is invalid.")
        return _result(
            status,
            "Purchase status was verified without entitlement.",
            release_id=response["release_id"],
            version=str(version_value),
            payment_id=response["payment_id"],
            payment_state=payment_state,
            price_minor=response["price_minor"],
            currency=response["currency"],
            rejection_reason=response["rejection_reason"],
        )


__all__ = [
    "STATUS_ENTITLED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_OFFLINE",
    "STATUS_PENDING",
    "STATUS_PURCHASE_REQUIRED",
    "STATUS_REJECTED",
    "STATUS_SERVER_UNAVAILABLE",
    "STATUS_SUBMITTED",
    "STATUS_UNDER_REVIEW",
    "ProductPurchaseService",
    "PurchaseResult",
]

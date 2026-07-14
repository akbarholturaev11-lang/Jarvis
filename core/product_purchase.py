"""Proof-bound manual payment submission and signed entitlement polling."""

from __future__ import annotations

import re
import uuid
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
from core.product_offer import (
    MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
    MAX_PAYMENT_METHOD_TEXT_LENGTH,
    MAX_PAYMENT_RECIPIENT_TEXT_LENGTH,
)
from core.product_version import (
    PRODUCT_ID,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)


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
STATUS_NOT_CONFIGURED: Final = "not_configured"

DEVICE_CHALLENGE_PATH: Final = "/api/device-challenges"
INITIAL_PURCHASE_CHALLENGE_PATH: Final = "/api/purchases/challenges"

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
        STATUS_NOT_CONFIGURED,
    }
)


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    status: str
    message: str = field(repr=False)
    release_id: str | None = field(default=None, repr=False)
    license_id: str | None = field(default=None, repr=False)
    purchase_id: str | None = field(default=None, repr=False)
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


@dataclass(frozen=True, slots=True)
class InitialPurchaseOffer:
    purchase_id: str = field(repr=False)
    purchase_grant: str = field(repr=False)
    release_id: str = field(repr=False)
    version: str
    price_minor: int
    currency: str
    supported_platforms: tuple[str, ...]
    features_en: str
    features_ru: str
    fixes_en: str
    fixes_ru: str
    payment_status: str
    method_en: str | None = field(default=None, repr=False)
    method_ru: str | None = field(default=None, repr=False)
    recipient: str | None = field(default=None, repr=False)
    instructions_en: str | None = field(default=None, repr=False)
    instructions_ru: str | None = field(default=None, repr=False)
    expires_at: str = field(default="", repr=False)

    @property
    def configured(self) -> bool:
        return self.payment_status == "configured"

    def __repr__(self) -> str:
        return (
            f"InitialPurchaseOffer(version={self.version!r}, "
            f"price_minor={self.price_minor!r}, currency={self.currency!r}, "
            f"payment_status={self.payment_status!r}, grant=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class InitialPurchaseOfferResult:
    status: str
    message: str = field(repr=False)
    offer: InitialPurchaseOffer | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return (
            self.status == STATUS_PURCHASE_REQUIRED
            and self.offer is not None
            and self.offer.configured
        )

    def __repr__(self) -> str:
        return (
            f"InitialPurchaseOfferResult(status={self.status!r}, "
            f"offer={'available' if self.offer else 'none'!r})"
        )


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


def new_initial_purchase_id() -> str:
    return f"purchase_{uuid.uuid4().hex}"


def _bounded_text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("purchase text is invalid")
    return value


def _price_currency(price: object, currency: object) -> tuple[int, str]:
    if type(price) is not int or price <= 0:
        raise ValueError("purchase price is invalid")
    if type(currency) is not str or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("purchase currency is invalid")
    return price, currency


def _localized_text(
    value: object,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> tuple[str, str]:
    if type(value) is not dict or frozenset(value) != {"en", "ru"}:
        raise ValueError("localized purchase text is invalid")
    return (
        _bounded_text(value["en"], maximum, allow_empty=allow_empty),
        _bounded_text(value["ru"], maximum, allow_empty=allow_empty),
    )


def _initial_offer(response: dict[str, object], purchase_id: str) -> InitialPurchaseOffer:
    expected = {
        "purchase_grant",
        "purchase_id",
        "release_id",
        "expires_at",
        "release_info",
        "payment_instructions",
    }
    if frozenset(response) != expected or response["purchase_id"] != purchase_id:
        raise ValueError("initial purchase offer schema mismatch")
    grant = response["purchase_grant"]
    release_id = response["release_id"]
    expires_at = _utc_timestamp(response["expires_at"])
    if (
        type(grant) is not str
        or not 20 <= len(grant) <= 128
        or any(character in grant for character in "\x00\r\n")
        or type(release_id) is not str
        or _OPAQUE_ID_RE.fullmatch(release_id) is None
    ):
        raise ValueError("initial purchase grant is invalid")
    info = response["release_info"]
    if type(info) is not dict or frozenset(info) != {
        "version",
        "price_minor",
        "currency",
        "supported_platforms",
        "features",
        "fixes",
    }:
        raise ValueError("initial purchase release schema mismatch")
    version = str(SemanticVersion.parse(info["version"]))
    price, currency = _price_currency(info["price_minor"], info["currency"])
    platforms_input = info["supported_platforms"]
    if type(platforms_input) is not list or not platforms_input:
        raise ValueError("initial purchase platforms are invalid")
    platforms: list[str] = []
    for item in platforms_input:
        if type(item) is not str:
            raise ValueError("initial purchase platform is invalid")
        normalized = normalize_platform(item)
        if normalized == "unknown" or normalized in platforms:
            raise ValueError("initial purchase platform is invalid")
        platforms.append(normalized)
    features_en, features_ru = _localized_text(
        info["features"],
        4000,
        allow_empty=True,
    )
    fixes_en, fixes_ru = _localized_text(
        info["fixes"],
        4000,
        allow_empty=True,
    )
    instructions = response["payment_instructions"]
    if type(instructions) is not dict or "status" not in instructions:
        raise ValueError("payment instructions are invalid")
    payment_status = instructions["status"]
    if payment_status == "not_configured":
        if frozenset(instructions) != {"status"}:
            raise ValueError("payment instructions are invalid")
        method_en = method_ru = recipient = steps_en = steps_ru = None
    elif payment_status == "configured":
        if frozenset(instructions) != {
            "status",
            "method",
            "recipient",
            "instructions",
        }:
            raise ValueError("payment instructions are invalid")
        method_en, method_ru = _localized_text(
            instructions["method"],
            MAX_PAYMENT_METHOD_TEXT_LENGTH,
        )
        recipient = _bounded_text(
            instructions["recipient"],
            MAX_PAYMENT_RECIPIENT_TEXT_LENGTH,
        )
        steps_en, steps_ru = _localized_text(
            instructions["instructions"],
            MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
        )
    else:
        raise ValueError("payment instructions status is invalid")
    return InitialPurchaseOffer(
        purchase_id,
        grant,
        release_id,
        version,
        price,
        currency,
        tuple(platforms),
        features_en,
        features_ru,
        fixes_en,
        fixes_ru,
        payment_status,
        method_en,
        method_ru,
        recipient,
        steps_en,
        steps_ru,
        expires_at,
    )


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

    def prepare_initial_purchase(
        self,
        *,
        purchase_id: str,
        version: str | SemanticVersion,
        platform: str,
        architecture: str,
    ) -> InitialPurchaseOfferResult:
        """Prove a fresh device and fetch one server-controlled exact-version offer."""

        try:
            if type(purchase_id) is not str or not re.fullmatch(
                r"purchase_[0-9a-f]{32}", purchase_id
            ):
                raise ValueError("purchase id is invalid")
            version_value = (
                version
                if isinstance(version, SemanticVersion)
                else SemanticVersion.parse(version)
            )
            platform_value = normalize_platform(platform)
            architecture_value = normalize_architecture(architecture)
            if platform_value == "unknown" or architecture_value == "unknown":
                raise ValueError("purchase target is invalid")
            identity_result = self._identity_manager.get_or_create()
            if (
                identity_result.status == IDENTITY_NOT_AVAILABLE
                or identity_result.identity is None
            ):
                return InitialPurchaseOfferResult(
                    STATUS_NOT_AVAILABLE,
                    "Secure device identity is unavailable.",
                )
            if identity_result.status != IDENTITY_SUCCESS:
                return InitialPurchaseOfferResult(
                    STATUS_FAILED,
                    "Device identity could not be prepared.",
                )
            identity = identity_result.identity
            challenge = self._api.request_json(
                "POST",
                INITIAL_PURCHASE_CHALLENGE_PATH,
                payload={
                    "product_id": PRODUCT_ID,
                    "purchase_id": purchase_id,
                    "version": str(version_value),
                    "device_key_fingerprint": identity.fingerprint,
                    "platform": platform_value,
                    "architecture": architecture_value,
                },
            )
            if frozenset(challenge) != {
                "challenge_id",
                "challenge_nonce",
                "purchase_id",
                "release_id",
                "version",
                "issued_at",
                "expires_at",
            }:
                raise ValueError("initial challenge schema mismatch")
            challenge_id = challenge["challenge_id"]
            nonce = challenge["challenge_nonce"]
            if (
                type(challenge_id) is not str
                or _OPAQUE_ID_RE.fullmatch(challenge_id) is None
                or type(nonce) is not str
                or challenge["purchase_id"] != purchase_id
                or challenge["version"] != str(version_value)
                or type(challenge["release_id"]) is not str
                or _OPAQUE_ID_RE.fullmatch(challenge["release_id"]) is None
            ):
                raise ValueError("initial challenge context mismatch")
            _utc_timestamp(challenge["issued_at"])
            _utc_timestamp(challenge["expires_at"])
            response = self._api.request_json(
                "POST",
                f"{INITIAL_PURCHASE_CHALLENGE_PATH}/{challenge_id}/verify",
                payload={
                    "challenge_nonce": nonce,
                    "public_key_base64": identity.public_key_base64,
                    "signature_base64": identity.sign_challenge(nonce),
                },
            )
            offer = _initial_offer(response, purchase_id)
            if (
                offer.release_id != challenge["release_id"]
                or offer.version != str(version_value)
                or platform_value not in offer.supported_platforms
            ):
                raise ValueError("initial offer context mismatch")
            status = (
                STATUS_PURCHASE_REQUIRED
                if offer.configured
                else STATUS_NOT_CONFIGURED
            )
            return InitialPurchaseOfferResult(
                status,
                "Initial purchase offer was verified.",
                offer,
            )
        except ProductApiError as exc:
            failure = _api_failure(exc)
            return InitialPurchaseOfferResult(failure.status, failure.message)
        except (TypeError, ValueError, RuntimeError):
            return InitialPurchaseOfferResult(
                STATUS_INVALID,
                "Initial purchase offer is invalid.",
            )

    def submit_initial_payment(
        self,
        offer: InitialPurchaseOffer,
        *,
        paid_at: str,
        screenshot: bytes,
        content_type: str,
        submission_id: str,
        supersedes_payment_id: str | None = None,
    ) -> PurchaseResult:
        """Submit sanitized evidence with the offer's one-time bootstrap grant."""

        try:
            if not isinstance(offer, InitialPurchaseOffer) or not offer.configured:
                raise ValueError("initial purchase offer is invalid")
            if (
                type(screenshot) is not bytes
                or not 1 <= len(screenshot) <= MAX_MULTIPART_FILE_BYTES
                or content_type not in {"image/png", "image/jpeg", "image/webp"}
                or type(submission_id) is not str
                or re.fullmatch(r"purchase_[0-9a-f]{32}", submission_id) is None
            ):
                raise ValueError("payment evidence is invalid")
            paid_at = _utc_timestamp(paid_at)
            fields = {
                "paid_at": paid_at,
                "client_submission_id": submission_id,
            }
            if supersedes_payment_id is not None:
                if (
                    type(supersedes_payment_id) is not str
                    or _OPAQUE_ID_RE.fullmatch(supersedes_payment_id) is None
                ):
                    raise ValueError("superseded payment is invalid")
                fields["supersedes_payment_id"] = supersedes_payment_id
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
            }[content_type]
            response = self._api.request_multipart_json(
                f"/api/purchases/{offer.purchase_id}/releases/"
                f"{offer.release_id}/payments",
                fields=fields,
                file_field="file",
                filename=f"payment.{extension}",
                content_type=content_type,
                content=screenshot,
                headers={"X-Purchase-Grant": offer.purchase_grant},
            )
        except ProductApiError as exc:
            return _api_failure(exc)
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Initial payment request is invalid.")
        expected = {
            "id",
            "license_id",
            "purchase_id",
            "release_id",
            "version",
            "amount_minor",
            "currency",
            "paid_at",
            "submitted_at",
            "state",
            "rejection_reason",
            "idempotent",
        }
        try:
            if (
                frozenset(response) != expected
                or response["purchase_id"] != offer.purchase_id
                or response["release_id"] != offer.release_id
                or response["version"] != offer.version
                or response["state"] != "pending"
                or response["rejection_reason"] is not None
                or type(response["idempotent"]) is not bool
            ):
                raise ValueError("initial payment response mismatch")
            payment_id = response["id"]
            license_id = response["license_id"]
            if (
                type(payment_id) is not str
                or _OPAQUE_ID_RE.fullmatch(payment_id) is None
                or type(license_id) is not str
                or _OPAQUE_ID_RE.fullmatch(license_id) is None
            ):
                raise ValueError("initial payment identifiers are invalid")
            price, currency = _price_currency(
                response["amount_minor"],
                response["currency"],
            )
            if price != offer.price_minor or currency != offer.currency:
                raise ValueError("initial payment price changed")
            _utc_timestamp(response["paid_at"])
            _utc_timestamp(response["submitted_at"])
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Initial payment response is invalid.")
        return _result(
            STATUS_SUBMITTED,
            "Payment evidence was submitted for manual review.",
            purchase_id=offer.purchase_id,
            release_id=offer.release_id,
            license_id=license_id,
            version=offer.version,
            payment_id=payment_id,
            payment_state="pending",
            price_minor=price,
            currency=currency,
        )

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
        submission_id: str | None = None,
        supersedes_payment_id: str | None = None,
    ) -> PurchaseResult:
        try:
            submission_id = (
                new_initial_purchase_id()
                if submission_id is None
                else submission_id
            )
            if (
                type(license_id) is not str
                or _OPAQUE_ID_RE.fullmatch(license_id) is None
                or type(release_id) is not str
                or _OPAQUE_ID_RE.fullmatch(release_id) is None
                or type(submission_id) is not str
                or re.fullmatch(r"purchase_[0-9a-f]{32}", submission_id) is None
                or type(screenshot) is not bytes
                or not 1 <= len(screenshot) <= MAX_MULTIPART_FILE_BYTES
                or content_type not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise ValueError("payment request is invalid")
            if supersedes_payment_id is not None and (
                type(supersedes_payment_id) is not str
                or _OPAQUE_ID_RE.fullmatch(supersedes_payment_id) is None
            ):
                raise ValueError("superseded payment is invalid")
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
            fields = {
                "paid_at": paid_at,
                "client_submission_id": submission_id,
            }
            if supersedes_payment_id is not None:
                fields["supersedes_payment_id"] = supersedes_payment_id
            response = self._api.request_multipart_json(
                f"/api/customer/licenses/{license_id}/releases/{release_id}/payments",
                fields=fields,
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
        try:
            if response["version"] is not None:
                SemanticVersion.parse(response["version"])
            price, currency = _price_currency(
                response["amount_minor"],
                response["currency"],
            )
            _utc_timestamp(response["paid_at"])
            _utc_timestamp(response["submitted_at"])
        except (TypeError, ValueError):
            return _result(STATUS_INVALID, "Payment response is invalid.")
        return _result(
            STATUS_SUBMITTED,
            "Payment evidence was submitted for manual review.",
            release_id=release_id,
            version=response["version"],
            payment_id=response["id"],
            payment_state="pending",
            price_minor=price,
            currency=currency,
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
            "features",
            "fixes",
        }
        if (
            frozenset(response) != expected
            or response["version"] != str(version_value)
            or type(response["entitled"]) is not bool
            or type(response["active_device_bound"]) is not bool
        ):
            return _result(STATUS_INVALID, "Purchase status response is invalid.")
        try:
            price, currency = _price_currency(
                response["price_minor"],
                response["currency"],
            )
            _localized_text(response["features"], 4000, allow_empty=True)
            _localized_text(response["fixes"], 4000, allow_empty=True)
            release_id = response["release_id"]
            if (
                type(release_id) is not str
                or _OPAQUE_ID_RE.fullmatch(release_id) is None
                or response["release_state"] != "published"
            ):
                raise ValueError("purchase release is invalid")
        except (TypeError, ValueError):
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
                price_minor=price,
                currency=currency,
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
        rejection_reason = response["rejection_reason"]
        if payment_state == "rejected":
            try:
                rejection_reason = _bounded_text(rejection_reason, 500)
            except (TypeError, ValueError):
                return _result(STATUS_INVALID, "Purchase rejection is invalid.")
        elif rejection_reason is not None:
            return _result(STATUS_INVALID, "Purchase status response is invalid.")
        return _result(
            status,
            "Purchase status was verified without entitlement.",
            release_id=response["release_id"],
            version=str(version_value),
            payment_id=response["payment_id"],
            payment_state=payment_state,
            price_minor=price,
            currency=currency,
            rejection_reason=rejection_reason,
        )


__all__ = [
    "STATUS_ENTITLED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_CONFIGURED",
    "STATUS_OFFLINE",
    "STATUS_PENDING",
    "STATUS_PURCHASE_REQUIRED",
    "STATUS_REJECTED",
    "STATUS_SERVER_UNAVAILABLE",
    "STATUS_SUBMITTED",
    "STATUS_UNDER_REVIEW",
    "ProductPurchaseService",
    "InitialPurchaseOffer",
    "InitialPurchaseOfferResult",
    "PurchaseResult",
    "new_initial_purchase_id",
]

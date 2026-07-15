"""Desktop-facing product activation and update orchestration.

The runtime never blocks an already entitled version on network availability.
Local status is derived from the device key and a pinned, signed certificate;
network calls happen only for explicit activation or update checks.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

from core.app_paths import AppPaths, resolve_app_paths
from core.device_identity import DeviceIdentityManager
from core.entitlement_cache import SignedEntitlementCache
from core.product_activation import ActivationResult, ProductActivationService
from core.product_config import (
    STATUS_SUCCESS as CONFIG_SUCCESS,
    ProductConfigResult,
    load_product_client_config,
)
from core.product_purchase import (
    STATUS_ENTITLED as PURCHASE_ENTITLED,
    STATUS_NOT_AVAILABLE as PURCHASE_NOT_AVAILABLE,
    STATUS_PURCHASE_REQUIRED,
    InitialPurchaseOffer,
    InitialPurchaseOfferResult,
    ProductPurchaseService,
    PurchaseResult,
    new_initial_purchase_id,
)
from core.payment_request_store import (
    STATUS_CORRUPT as PAYMENT_REQUEST_CORRUPT,
    STATUS_NOT_AVAILABLE as PAYMENT_REQUEST_NOT_AVAILABLE,
    STATUS_NOT_FOUND as PAYMENT_REQUEST_NOT_FOUND,
    DurablePaymentRequestStore,
    PaymentRequestEnvelope,
    PaymentRequestKind,
    PaymentRequestState,
)
from core.product_version import SemanticVersion
from core.product_updates import (
    ProductUpdateService,
    UpdateCheckResult,
    UpdateDownloadResult,
)
from core.runtime_product import RuntimeProductIdentity, load_runtime_product_identity
from core.secure_store import STATUS_SUCCESS, SecureStore, create_secure_store
from core.update_transaction import (
    TransactionStatus,
    UpdateTransactionCoordinator,
    UpdateTransactionResult,
    UpdaterPlatformAdapter,
    create_updater_adapter,
)


STATUS_NOT_CONFIGURED: Final = "not_configured"
STATUS_NOT_ACTIVATED: Final = "not_activated"
STATUS_ENTITLED: Final = "entitled"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_INVALID: Final = "invalid"
STATUS_FAILED: Final = "failed"

LICENSE_STATE_SERVICE: Final = "com.jarvis.assistant.product"
LICENSE_ID_ACCOUNT: Final = "active-license-id-v1"
PENDING_PURCHASE_ACCOUNT: Final = "pending-purchase-v1"
_LICENSE_ID_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])")


@dataclass(frozen=True, slots=True)
class LocalProductState:
    status: str
    runtime: RuntimeProductIdentity | None = None
    license_id: str | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    @property
    def entitled(self) -> bool:
        return self.status == STATUS_ENTITLED


@dataclass(frozen=True, slots=True)
class ProductActivationOutcome:
    status: str
    activation: ActivationResult | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_ENTITLED


class ProductRuntimeService:
    """Compose config, device identity, signed cache, activation and updates."""

    __slots__ = (
        "_activation",
        "_cache",
        "_config_result",
        "_coordinator",
        "_identity",
        "_paths",
        "_packaged_runtime_expected",
        "_payment_requests",
        "_purchase",
        "_runtime",
        "_store",
        "_updates",
        "_last_update_candidate",
        "_last_staged_update",
        "_last_initial_offer",
        "_last_initial_purchase_id",
        "_last_initial_submission_id",
        "_last_update_submission_id",
        "_last_update_rejected_payment_id",
    )

    def __init__(
        self,
        *,
        app_paths: AppPaths | None = None,
        secure_store: SecureStore | None = None,
        config_result: ProductConfigResult | None = None,
        runtime_identity: RuntimeProductIdentity | None = None,
        updater_adapter: UpdaterPlatformAdapter | None = None,
    ) -> None:
        self._paths = resolve_app_paths() if app_paths is None else app_paths
        self._packaged_runtime_expected = (
            bool(getattr(sys, "frozen", False))
            if runtime_identity is None
            else runtime_identity.packaged
        )
        self._store = create_secure_store() if secure_store is None else secure_store
        self._config_result = (
            load_product_client_config(app_paths=self._paths)
            if config_result is None
            else config_result
        )
        try:
            self._runtime = (
                load_runtime_product_identity(
                    resource_root=self._paths.resource_root
                )
                if runtime_identity is None
                else runtime_identity
            )
        except RuntimeError:
            self._runtime = None
        self._identity = DeviceIdentityManager(
            self._store,
            creation_lock_path=str(
                self._paths.config_dir / "device-identity.lock"
            ),
        )
        self._coordinator = UpdateTransactionCoordinator(
            create_updater_adapter() if updater_adapter is None else updater_adapter,
            journal_path=self._paths.data_dir / "updates" / "rollback.json",
        )
        self._activation: ProductActivationService | None = None
        self._cache: SignedEntitlementCache | None = None
        self._purchase: ProductPurchaseService | None = None
        self._updates: ProductUpdateService | None = None
        self._payment_requests = DurablePaymentRequestStore(
            self._store,
            self._paths.data_dir / "payments",
        )
        self._last_update_candidate = None
        self._last_staged_update = None
        self._last_initial_offer: InitialPurchaseOffer | None = None
        self._last_initial_purchase_id: str | None = None
        self._last_initial_submission_id: str | None = None
        self._last_update_submission_id: str | None = None
        self._last_update_rejected_payment_id: str | None = None
        if self._config_result.status == CONFIG_SUCCESS and self._config_result.config:
            config = self._config_result.config
            cache = SignedEntitlementCache(
                self._paths.data_dir / "entitlements",
                trusted_public_keys=config.entitlement_public_keys,
            )
            api = config.api_client()
            self._cache = cache
            self._activation = ProductActivationService(api, self._identity, cache)
            self._purchase = ProductPurchaseService(api, self._identity, cache)
            self._updates = ProductUpdateService(
                api,
                cache,
                self._identity,
                self._paths.update_staging_dir,
                trusted_release_public_keys=config.release_public_keys,
            )

    def __repr__(self) -> str:
        return "ProductRuntimeService(config=<redacted>, local_state=<private>)"

    @property
    def packaged_runtime_expected(self) -> bool:
        return self._packaged_runtime_expected

    def local_state(self) -> LocalProductState:
        if self._runtime is None:
            return LocalProductState(
                STATUS_INVALID, message="Runtime product identity is invalid."
            )
        if self._activation is None or self._updates is None:
            return LocalProductState(
                STATUS_NOT_CONFIGURED,
                self._runtime,
                message="Product service is not configured.",
            )
        license_result = self._store.get(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT)
        if license_result.status != STATUS_SUCCESS or not license_result.value:
            status = (
                STATUS_NOT_ACTIVATED
                if license_result.status == "not_found"
                else STATUS_NOT_AVAILABLE
                if license_result.status == "not_available"
                else STATUS_FAILED
            )
            return LocalProductState(status, self._runtime, message="License is not active.")
        license_id = license_result.value
        if _LICENSE_ID_RE.fullmatch(license_id) is None:
            return LocalProductState(
                STATUS_INVALID,
                self._runtime,
                message="Stored license identity is invalid.",
            )
        identity = self._identity.load()
        if not identity.ok or identity.identity is None:
            return LocalProductState(
                STATUS_NOT_AVAILABLE,
                self._runtime,
                message="Secure device identity is not available.",
            )
        if self._cache is None:
            return LocalProductState(
                STATUS_NOT_CONFIGURED,
                self._runtime,
                message="Product service is not configured.",
            )
        verified = self._cache.load_verified(
            license_id=license_id,
            device_fingerprint=identity.identity.fingerprint,
            version=self._runtime.product_version.version,
        )
        if verified.ok:
            return LocalProductState(
                STATUS_ENTITLED,
                self._runtime,
                license_id,
                "Exact-version offline entitlement is verified.",
            )
        if verified.status == "not_found":
            return LocalProductState(
                STATUS_NOT_ACTIVATED,
                self._runtime,
                license_id,
                "This exact version is not activated.",
            )
        if verified.status == "not_available":
            return LocalProductState(
                STATUS_NOT_AVAILABLE,
                self._runtime,
                license_id,
                "Offline entitlement storage is not available.",
            )
        if verified.status == "failed":
            return LocalProductState(
                STATUS_FAILED,
                self._runtime,
                license_id,
                "Offline entitlement could not be loaded.",
            )
        return LocalProductState(
            STATUS_INVALID,
            self._runtime,
            license_id,
            "Offline entitlement is invalid.",
        )

    def device_fingerprint(self) -> str | None:
        """Return the public per-install identity, creating it securely if needed."""

        result = self._identity.get_or_create()
        if not result.ok or result.identity is None:
            return None
        return result.identity.fingerprint

    def activate(self, license_key: str) -> ProductActivationOutcome:
        if self._runtime is None:
            return ProductActivationOutcome(STATUS_INVALID, message="Runtime identity is invalid.")
        if self._activation is None:
            return ProductActivationOutcome(
                STATUS_NOT_CONFIGURED, message="Product service is not configured."
            )
        result = self._activation.activate(
            license_key,
            version=self._runtime.product_version.version,
            platform=self._runtime.platform,
            architecture=self._runtime.architecture,
        )
        if not result.ok or result.license_id is None:
            return ProductActivationOutcome(result.status, result, "Activation failed.")
        persisted = self._store.set(
            LICENSE_STATE_SERVICE,
            LICENSE_ID_ACCOUNT,
            result.license_id,
        )
        if persisted.status != STATUS_SUCCESS:
            return ProductActivationOutcome(
                STATUS_NOT_AVAILABLE,
                result,
                "Secure license state could not be stored.",
            )
        verified = self.local_state()
        if not verified.entitled:
            return ProductActivationOutcome(
                verified.status,
                result,
                "Persisted exact-version entitlement could not be verified.",
            )
        return ProductActivationOutcome(
            STATUS_ENTITLED, result, "Exact version activated."
        )

    @property
    def purchase_available(self) -> bool:
        return self._runtime is not None and self._purchase is not None

    def pending_initial_purchase(self) -> tuple[str, str, str] | None:
        stored = self._store.get(LICENSE_STATE_SERVICE, PENDING_PURCHASE_ACCOUNT)
        if stored.status != STATUS_SUCCESS or not stored.value:
            return None
        parts = stored.value.split("|")
        if len(parts) != 3:
            return None
        purchase_id, license_id, version = parts
        if (
            re.fullmatch(r"purchase_[0-9a-f]{32}", purchase_id) is None
            or _LICENSE_ID_RE.fullmatch(license_id) is None
        ):
            return None
        try:
            normalized = str(SemanticVersion.parse(version))
        except (TypeError, ValueError):
            return None
        return purchase_id, license_id, normalized

    def prepare_initial_purchase(
        self,
        *,
        purchase_id: str | None = None,
    ) -> InitialPurchaseOfferResult:
        if self._runtime is None or self._purchase is None:
            return InitialPurchaseOfferResult(
                STATUS_NOT_CONFIGURED,
                "Initial purchase service is not configured.",
            )
        if self.local_state().entitled:
            return InitialPurchaseOfferResult(
                PURCHASE_ENTITLED,
                "This exact version is already entitled.",
            )
        pending = self.pending_initial_purchase()
        selected_id = (
            purchase_id
            or (pending[0] if pending is not None else None)
            or new_initial_purchase_id()
        )
        result = self._purchase.prepare_initial_purchase(
            purchase_id=selected_id,
            version=self._runtime.product_version.version,
            platform=self._runtime.platform,
            architecture=self._runtime.architecture,
        )
        if result.offer is not None:
            self._last_initial_offer = result.offer
            self._last_initial_purchase_id = selected_id
            self._last_initial_submission_id = new_initial_purchase_id()
        return result

    @staticmethod
    def _utc_now_text() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def submit_initial_purchase(
        self,
        *,
        paid_at: str,
        screenshot: bytes,
        content_type: str,
        supersedes_payment_id: str | None = None,
    ) -> PurchaseResult:
        offer = self._last_initial_offer
        submission_id = self._last_initial_submission_id
        if self._purchase is None:
            return PurchaseResult(
                STATUS_NOT_CONFIGURED,
                "Initial purchase offer is not prepared.",
            )
        # If a durable request already exists, retry that exact submission
        # instead of creating a second payment.  The server matches an
        # idempotent retry on the stored idempotency key and evidence digest,
        # so a lost response can never turn into a duplicate purchase.
        existing = self._payment_requests.load()
        if existing.status == PAYMENT_REQUEST_NOT_AVAILABLE:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Secure payment storage is not available.",
            )
        if existing.status == PAYMENT_REQUEST_CORRUPT:
            return PurchaseResult(
                STATUS_INVALID,
                "A pending payment request is unreadable.",
            )
        if existing.pending and existing.envelope is not None:
            if (
                existing.envelope.kind is PaymentRequestKind.INITIAL
                and existing.screenshot is not None
            ):
                return self._resume_initial(existing.envelope, existing.screenshot)
            return PurchaseResult(
                STATUS_INVALID,
                "Another payment request is already pending.",
            )
        if offer is None or submission_id is None:
            return PurchaseResult(
                STATUS_NOT_CONFIGURED,
                "Initial purchase offer is not prepared.",
            )
        try:
            if type(screenshot) is not bytes or not screenshot:
                raise ValueError("payment evidence is invalid")
            now = self._utc_now_text()
            envelope = PaymentRequestEnvelope(
                idempotency_key=submission_id,
                kind=PaymentRequestKind.INITIAL,
                release_id=offer.release_id,
                device_fingerprint=self.device_fingerprint() or "unknown",
                proof_sha256=hashlib.sha256(screenshot).hexdigest(),
                content_type=content_type,
                paid_at=paid_at,
                version=offer.version,
                state=PaymentRequestState.PENDING,
                created_at=now,
                updated_at=now,
                purchase_id=offer.purchase_id,
                supersedes_payment_id=supersedes_payment_id,
            )
        except (TypeError, ValueError):
            return PurchaseResult(
                STATUS_INVALID,
                "Initial payment request is invalid.",
            )
        # Persist the request durably before the network call so a restart or a
        # lost response can resume the exact submission.
        saved = self._payment_requests.save(envelope, screenshot)
        if not saved.ok:
            if saved.status == PAYMENT_REQUEST_NOT_AVAILABLE:
                return PurchaseResult(
                    PURCHASE_NOT_AVAILABLE,
                    "Secure payment storage is not available.",
                )
            return PurchaseResult(
                STATUS_FAILED,
                "Payment request could not be stored securely.",
            )
        return self._submit_and_finalize_initial(offer, envelope, screenshot)

    def resume_pending_payment(self) -> PurchaseResult | None:
        """Re-submit a payment that a restart or lost response left in flight."""

        if self._purchase is None:
            return None
        loaded = self._payment_requests.load()
        if loaded.status == PAYMENT_REQUEST_NOT_FOUND:
            return None
        if loaded.status == PAYMENT_REQUEST_NOT_AVAILABLE:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Secure payment storage is not available.",
            )
        if loaded.status == PAYMENT_REQUEST_CORRUPT:
            return PurchaseResult(
                STATUS_INVALID,
                "A pending payment request is unreadable.",
            )
        if not loaded.ok:
            return PurchaseResult(
                STATUS_FAILED,
                "A pending payment request could not be read.",
            )
        if (
            not loaded.pending
            or loaded.envelope is None
            or loaded.screenshot is None
        ):
            return None
        if loaded.envelope.kind is PaymentRequestKind.INITIAL:
            return self._resume_initial(loaded.envelope, loaded.screenshot)
        if loaded.envelope.kind is PaymentRequestKind.UPDATE:
            return self._resume_update(loaded.envelope, loaded.screenshot)
        return None

    def _resume_initial(
        self,
        envelope: PaymentRequestEnvelope,
        screenshot: bytes,
    ) -> PurchaseResult:
        offer_result = self.prepare_initial_purchase(
            purchase_id=envelope.purchase_id
        )
        offer = offer_result.offer
        if offer is not None:
            if (
                offer.release_id != envelope.release_id
                or offer.version != envelope.version
            ):
                return PurchaseResult(
                    STATUS_INVALID,
                    "The pending payment no longer matches the current offer.",
                )
            # Reuse the exact idempotency key and sanitized bytes.
            self._last_initial_submission_id = envelope.idempotency_key
            return self._submit_and_finalize_initial(offer, envelope, screenshot)
        if offer_result.status == PURCHASE_ENTITLED:
            # The version was activated meanwhile; the request is obsolete.
            self._payment_requests.clear()
            return PurchaseResult(
                STATUS_NOT_CONFIGURED,
                "This exact version is already entitled.",
            )
        return PurchaseResult(offer_result.status, offer_result.message)

    def _submit_and_finalize_initial(
        self,
        offer: InitialPurchaseOffer,
        envelope: PaymentRequestEnvelope,
        screenshot: bytes,
    ) -> PurchaseResult:
        result = self._purchase.submit_initial_payment(
            offer,
            paid_at=envelope.paid_at,
            screenshot=screenshot,
            content_type=envelope.content_type,
            submission_id=envelope.idempotency_key,
            supersedes_payment_id=envelope.supersedes_payment_id,
        )
        if result.status != "submitted" or result.license_id is None:
            # Keep the durable request pending so a later retry stays idempotent.
            return result
        stored = self._store.set(
            LICENSE_STATE_SERVICE,
            PENDING_PURCHASE_ACCOUNT,
            f"{offer.purchase_id}|{result.license_id}|{offer.version}",
        )
        if stored.status != STATUS_SUCCESS:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Pending purchase could not be stored securely.",
                release_id=result.release_id,
                license_id=result.license_id,
                purchase_id=result.purchase_id,
                version=result.version,
                payment_id=result.payment_id,
                payment_state=result.payment_state,
                price_minor=result.price_minor,
                currency=result.currency,
            )
        # The submission is durably confirmed; drop the sensitive evidence.
        self._payment_requests.clear()
        return result

    def poll_initial_purchase(self) -> PurchaseResult:
        if self._purchase is None:
            return PurchaseResult(
                STATUS_NOT_CONFIGURED,
                "Initial purchase service is not configured.",
            )
        pending = self.pending_initial_purchase()
        if pending is None:
            return PurchaseResult(
                STATUS_PURCHASE_REQUIRED,
                "No pending initial purchase was found.",
            )
        _purchase_id, license_id, version = pending
        result = self._purchase.poll_status(
            license_id=license_id,
            version=version,
        )
        if not result.entitled:
            return PurchaseResult(
                result.status,
                result.message,
                release_id=result.release_id,
                license_id=license_id,
                version=result.version,
                payment_id=result.payment_id,
                payment_state=result.payment_state,
                price_minor=result.price_minor,
                currency=result.currency,
                rejection_reason=result.rejection_reason,
            )
        persisted = self._store.set(
            LICENSE_STATE_SERVICE,
            LICENSE_ID_ACCOUNT,
            license_id,
        )
        if persisted.status != STATUS_SUCCESS:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Approved license could not be stored securely.",
                release_id=result.release_id,
                license_id=license_id,
                version=result.version,
                payment_id=result.payment_id,
                payment_state=result.payment_state,
                price_minor=result.price_minor,
                currency=result.currency,
            )
        verified = self.local_state()
        if not verified.entitled:
            return PurchaseResult(
                STATUS_INVALID,
                "Approved exact-version entitlement failed local verification.",
                release_id=result.release_id,
                license_id=license_id,
                version=result.version,
                payment_id=result.payment_id,
                payment_state=result.payment_state,
                price_minor=result.price_minor,
                currency=result.currency,
            )
        self._store.delete(LICENSE_STATE_SERVICE, PENDING_PURCHASE_ACCOUNT)
        return PurchaseResult(
            PURCHASE_ENTITLED,
            "Approved exact-version entitlement is active.",
            release_id=result.release_id,
            license_id=license_id,
            version=result.version,
            payment_id=result.payment_id,
            payment_state=result.payment_state,
            price_minor=result.price_minor,
            currency=result.currency,
            certificate=result.certificate,
        )

    def check_updates(self) -> UpdateCheckResult | None:
        state = self.local_state()
        if (
            not state.entitled
            or state.license_id is None
            or state.runtime is None
            or self._updates is None
        ):
            return None
        identity = self._identity.load()
        if not identity.ok or identity.identity is None:
            return None
        previous_release_id = getattr(self._last_update_candidate, "release_id", None)
        result = self._updates.check(
            license_id=state.license_id,
            device_fingerprint=identity.identity.fingerprint,
            installed=state.runtime.product_version,
            platform=state.runtime.platform,
            architecture=state.runtime.architecture,
        )
        self._last_update_candidate = result.candidate
        next_release_id = getattr(result.candidate, "release_id", None)
        if next_release_id != previous_release_id:
            pending = self._payment_requests.peek()
            envelope = pending.envelope
            if (
                next_release_id is not None
                and envelope is not None
                and envelope.kind is PaymentRequestKind.UPDATE
                and envelope.release_id == next_release_id
                and result.candidate is not None
                and envelope.version == result.candidate.target.version
                and envelope.license_id == state.license_id
            ):
                self._last_update_submission_id = envelope.idempotency_key
                self._last_update_rejected_payment_id = (
                    envelope.supersedes_payment_id
                )
            else:
                self._last_update_submission_id = (
                    new_initial_purchase_id() if next_release_id is not None else None
                )
                self._last_update_rejected_payment_id = None
        return result

    def submit_update_payment(
        self,
        *,
        paid_at: str,
        screenshot: bytes,
        content_type: str,
    ) -> PurchaseResult | None:
        state = self.local_state()
        candidate = self._last_update_candidate
        if (
            not state.entitled
            or state.license_id is None
            or candidate is None
            or self._purchase is None
        ):
            return None
        existing = self._payment_requests.load()
        if existing.status == PAYMENT_REQUEST_NOT_AVAILABLE:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Secure payment storage is not available.",
            )
        if existing.status == PAYMENT_REQUEST_CORRUPT:
            return PurchaseResult(
                STATUS_INVALID,
                "A pending payment request is unreadable.",
            )
        if existing.pending and existing.envelope is not None:
            if (
                existing.envelope.kind is PaymentRequestKind.UPDATE
                and existing.screenshot is not None
            ):
                return self._resume_update(existing.envelope, existing.screenshot)
            return PurchaseResult(
                STATUS_INVALID,
                "Another payment request is already pending.",
            )
        if self._last_update_submission_id is None:
            self._last_update_submission_id = new_initial_purchase_id()
        identity = self._identity.load()
        if not identity.ok or identity.identity is None:
            return PurchaseResult(
                PURCHASE_NOT_AVAILABLE,
                "Secure device identity is not available.",
            )
        try:
            if type(screenshot) is not bytes or not screenshot:
                raise ValueError("payment evidence is invalid")
            now = self._utc_now_text()
            envelope = PaymentRequestEnvelope(
                idempotency_key=self._last_update_submission_id,
                kind=PaymentRequestKind.UPDATE,
                release_id=candidate.release_id,
                device_fingerprint=identity.identity.fingerprint,
                proof_sha256=hashlib.sha256(screenshot).hexdigest(),
                content_type=content_type,
                paid_at=paid_at,
                version=candidate.target.version,
                state=PaymentRequestState.PENDING,
                created_at=now,
                updated_at=now,
                license_id=state.license_id,
                supersedes_payment_id=self._last_update_rejected_payment_id,
            )
        except (TypeError, ValueError):
            return PurchaseResult(STATUS_INVALID, "Update payment request is invalid.")
        saved = self._payment_requests.save(envelope, screenshot)
        if not saved.ok:
            if saved.status == PAYMENT_REQUEST_NOT_AVAILABLE:
                return PurchaseResult(
                    PURCHASE_NOT_AVAILABLE,
                    "Secure payment storage is not available.",
                )
            return PurchaseResult(
                STATUS_FAILED,
                "Payment request could not be stored securely.",
            )
        return self._submit_and_finalize_update(envelope, screenshot)

    def _resume_update(
        self,
        envelope: PaymentRequestEnvelope,
        screenshot: bytes,
    ) -> PurchaseResult:
        state = self.local_state()
        if (
            not state.entitled
            or state.license_id != envelope.license_id
            or self._purchase is None
        ):
            return PurchaseResult(
                STATUS_INVALID,
                "The pending update payment no longer matches this license.",
            )
        self._last_update_submission_id = envelope.idempotency_key
        self._last_update_rejected_payment_id = envelope.supersedes_payment_id
        return self._submit_and_finalize_update(envelope, screenshot)

    def _submit_and_finalize_update(
        self,
        envelope: PaymentRequestEnvelope,
        screenshot: bytes,
    ) -> PurchaseResult:
        assert self._purchase is not None
        assert envelope.license_id is not None
        result = self._purchase.submit_payment(
            license_id=envelope.license_id,
            release_id=envelope.release_id,
            paid_at=envelope.paid_at,
            screenshot=screenshot,
            content_type=envelope.content_type,
            submission_id=envelope.idempotency_key,
            supersedes_payment_id=envelope.supersedes_payment_id,
        )
        if result.status == "submitted":
            self._last_update_rejected_payment_id = None
            self._payment_requests.clear()
        return result

    def poll_update_purchase(self) -> PurchaseResult | None:
        state = self.local_state()
        candidate = self._last_update_candidate
        if (
            not state.entitled
            or state.license_id is None
            or candidate is None
            or self._purchase is None
        ):
            return None
        result = self._purchase.poll_status(
            license_id=state.license_id,
            version=candidate.target.version,
        )
        if result.status == "rejected" and result.payment_id is not None:
            self._last_update_rejected_payment_id = result.payment_id
            self._last_update_submission_id = new_initial_purchase_id()
        if result.entitled:
            self._last_update_rejected_payment_id = None
            self.check_updates()
        return result

    def download_update(self) -> UpdateDownloadResult | None:
        state = self.local_state()
        candidate = self._last_update_candidate
        if (
            not state.entitled
            or state.license_id is None
            or state.runtime is None
            or candidate is None
            or self._updates is None
        ):
            return None
        identity = self._identity.load()
        if not identity.ok or identity.identity is None:
            return None
        result = self._updates.download(
            candidate,
            license_id=state.license_id,
            device_fingerprint=identity.identity.fingerprint,
            platform=state.runtime.platform,
            architecture=state.runtime.architecture,
        )
        self._last_staged_update = result.staged
        return result

    def apply_staged_update(self) -> UpdateTransactionResult | None:
        staged = self._last_staged_update
        if staged is None:
            return None
        state = self.local_state()
        if (
            not state.entitled
            or state.license_id is None
            or state.runtime is None
            or state.runtime.product_version != staged.source
        ):
            return UpdateTransactionResult(
                TransactionStatus.INVALID,
                "The staged update no longer matches the installed entitlement.",
                staged.source,
                staged.target,
            )
        identity = self._identity.load()
        if not identity.ok or identity.identity is None or self._cache is None:
            return UpdateTransactionResult(
                TransactionStatus.NOT_AVAILABLE,
                "Update entitlement verification is not available.",
                staged.source,
                staged.target,
            )
        target_entitlement = self._cache.load_verified(
            license_id=state.license_id,
            device_fingerprint=identity.identity.fingerprint,
            version=staged.target.version,
        )
        if not target_entitlement.ok:
            status = (
                TransactionStatus.NOT_AVAILABLE
                if target_entitlement.status == "not_available"
                else TransactionStatus.FAILED
                if target_entitlement.status == "failed"
                else TransactionStatus.INVALID
            )
            return UpdateTransactionResult(
                status,
                "Exact-version update entitlement could not be verified.",
                staged.source,
                staged.target,
            )
        result = self._coordinator.apply(staged)
        if result.status is TransactionStatus.INSTALLED:
            self._last_staged_update = None
        return result

    @property
    def staged_update_ready(self) -> bool:
        return self._last_staged_update is not None

    def recover_update(self) -> UpdateTransactionResult:
        return self._coordinator.recover()

    def recover_update_if_required(self) -> UpdateTransactionResult | None:
        return self._coordinator.recover_if_required()


__all__ = [
    "LICENSE_ID_ACCOUNT",
    "LICENSE_STATE_SERVICE",
    "PENDING_PURCHASE_ACCOUNT",
    "STATUS_ENTITLED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_ACTIVATED",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_CONFIGURED",
    "LocalProductState",
    "ProductActivationOutcome",
    "ProductRuntimeService",
]

"""Desktop-facing product activation and update orchestration.

The runtime never blocks an already entitled version on network availability.
Local status is derived from the device key and a pinned, signed certificate;
network calls happen only for explicit activation or update checks.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
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
from core.product_purchase import ProductPurchaseService, PurchaseResult
from core.product_updates import (
    ProductUpdateService,
    UpdateCheckResult,
    UpdateDownloadResult,
)
from core.runtime_product import RuntimeProductIdentity, load_runtime_product_identity
from core.secure_store import STATUS_SUCCESS, SecureStore, create_secure_store
from core.update_transaction import (
    UpdateTransactionCoordinator,
    UpdateTransactionResult,
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
        "_purchase",
        "_runtime",
        "_store",
        "_updates",
        "_last_update_candidate",
        "_last_staged_update",
    )

    def __init__(
        self,
        *,
        app_paths: AppPaths | None = None,
        secure_store: SecureStore | None = None,
        config_result: ProductConfigResult | None = None,
        runtime_identity: RuntimeProductIdentity | None = None,
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
            create_updater_adapter(),
            journal_path=self._paths.data_dir / "updates" / "rollback.json",
        )
        self._activation: ProductActivationService | None = None
        self._cache: SignedEntitlementCache | None = None
        self._purchase: ProductPurchaseService | None = None
        self._updates: ProductUpdateService | None = None
        self._last_update_candidate = None
        self._last_staged_update = None
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
        result = self._updates.check(
            license_id=state.license_id,
            device_fingerprint=identity.identity.fingerprint,
            installed=state.runtime.product_version,
            platform=state.runtime.platform,
            architecture=state.runtime.architecture,
        )
        self._last_update_candidate = result.candidate
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
        return self._purchase.submit_payment(
            license_id=state.license_id,
            release_id=candidate.release_id,
            paid_at=paid_at,
            screenshot=screenshot,
            content_type=content_type,
        )

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
        if result.entitled:
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
        if self._last_staged_update is None:
            return None
        return self._coordinator.apply(self._last_staged_update)

    def recover_update(self) -> UpdateTransactionResult:
        return self._coordinator.recover()


__all__ = [
    "LICENSE_ID_ACCOUNT",
    "LICENSE_STATE_SERVICE",
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

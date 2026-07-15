from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import core.product_runtime as product_runtime_module
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.app_paths import resolve_app_paths
from core.entitlement_cache import SignedEntitlementCache
from core.entitlement_certificate import CERTIFICATE_SCHEMA, ENVELOPE_SCHEMA, SCHEMA_VERSION
from core.product_config import (
    STATUS_NOT_CONFIGURED as CONFIG_NOT_CONFIGURED,
    STATUS_SUCCESS as CONFIG_SUCCESS,
    ProductClientConfig,
    ProductConfigResult,
)
from core.product_runtime import (
    LICENSE_ID_ACCOUNT,
    LICENSE_STATE_SERVICE,
    PENDING_PURCHASE_ACCOUNT,
    STATUS_ENTITLED,
    STATUS_INVALID,
    STATUS_NOT_ACTIVATED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_CONFIGURED,
    LocalProductState,
    ProductRuntimeService,
)
from core.payment_request_store import (
    DEFAULT_ENVELOPE_ACCOUNT,
    DEFAULT_ENVELOPE_SERVICE,
    PaymentRequestState,
)
from core.product_purchase import (
    STATUS_PENDING,
    STATUS_PURCHASE_REQUIRED,
    STATUS_REJECTED,
    STATUS_SERVER_UNAVAILABLE,
    STATUS_SUBMITTED,
    InitialPurchaseOffer,
    InitialPurchaseOfferResult,
    PurchaseResult,
)
from core.product_updates import VerifiedStagedUpdate
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion
from core.runtime_product import RuntimeProductIdentity
from core.secure_store import (
    STATUS_NOT_AVAILABLE as STORE_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS as STORE_SUCCESS,
    SecureStore,
    SecureStoreResult,
)
from core.update_transaction import TransactionStatus, UpdateTransactionResult


KEY_ID = "entitlement-key-runtime-001"
LICENSE_ID = "license_runtime_001"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _certificate(
    private_key: Ed25519PrivateKey,
    *,
    fingerprint: str,
    version: str,
) -> str:
    payload = json.dumps(
        {
            "schema": CERTIFICATE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "license_id": LICENSE_ID,
            "device_key_fingerprint": fingerprint,
            "version": version,
            "issued_at": "2026-07-14T00:00:00Z",
            "key_id": KEY_ID,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return json.dumps(
        {
            "schema": ENVELOPE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "payload": _b64url(payload),
            "signature": _b64url(private_key.sign(payload)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class MissingStore(SecureStore):
    def _get(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return SecureStoreResult(STATUS_NOT_FOUND)


class MemoryStore(SecureStore):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def _get(self, service: str, account: str) -> SecureStoreResult:
        value = self.values.get((service, account))
        if value is None:
            return SecureStoreResult(STATUS_NOT_FOUND)
        return SecureStoreResult(STORE_SUCCESS, value=value)

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        self.values[(service, account)] = secret
        return SecureStoreResult(STORE_SUCCESS)

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        self.values.pop((service, account), None)
        return SecureStoreResult(STORE_SUCCESS)


class ReadbackUnavailableStore(MemoryStore):
    def _get(self, service: str, account: str) -> SecureStoreResult:
        if (service, account) == (LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT):
            return SecureStoreResult(STORE_NOT_AVAILABLE)
        return super()._get(service, account)


class EnvelopeUnavailableStore(MemoryStore):
    """A store where only the durable payment-request slot is unavailable."""

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        if (service, account) == (DEFAULT_ENVELOPE_SERVICE, DEFAULT_ENVELOPE_ACCOUNT):
            return SecureStoreResult(STORE_NOT_AVAILABLE)
        return super()._set(service, account, secret)


def _offer(purchase_id: str) -> InitialPurchaseOffer:
    return InitialPurchaseOffer(
        purchase_id,
        "private-purchase-grant",
        "rel_runtime_001",
        "1.0.0",
        125_000,
        "UZS",
        ("macos",),
        "Feature",
        "Функция",
        "Fix",
        "Исправление",
        "configured",
        "Transfer",
        "Перевод",
        "Recipient",
        "Pay",
        "Оплатите",
        "2026-07-14T00:02:00Z",
    )


class _RecordingPurchase:
    """Purchase-service stub that records the exact submission it received."""

    def __init__(
        self,
        offer: InitialPurchaseOffer,
        *,
        results: list[PurchaseResult],
    ) -> None:
        self._offer = offer
        self._results = list(results)
        self.submissions: list[tuple[str, bytes, str | None]] = []

    def prepare_initial_purchase(self, **_: object) -> InitialPurchaseOfferResult:
        return InitialPurchaseOfferResult(
            STATUS_PURCHASE_REQUIRED,
            "offer",
            self._offer,
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
        self.submissions.append((submission_id, screenshot, supersedes_payment_id))
        if len(self.submissions) <= len(self._results):
            return self._results[len(self.submissions) - 1]
        return self._results[-1]


class _RecordingUpdatePurchase:
    def __init__(self, results: list[PurchaseResult]) -> None:
        self._results = list(results)
        self.submissions: list[dict[str, object]] = []

    def submit_payment(self, **kwargs: object) -> PurchaseResult:
        self.submissions.append(dict(kwargs))
        index = min(len(self.submissions) - 1, len(self._results) - 1)
        return self._results[index]


class _RecordingUpdateCoordinator:
    def __init__(self, result: UpdateTransactionResult) -> None:
        self.result = result
        self.applied: list[VerifiedStagedUpdate] = []
        self.rollback_required = False

    def apply(self, staged: VerifiedStagedUpdate) -> UpdateTransactionResult:
        self.applied.append(staged)
        return self.result

    def recover(self) -> UpdateTransactionResult:
        raise AssertionError("recovery was not requested")


class ProductRuntimeTests(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_app_paths(
            platform_name="macos",
            home=root,
            environ={},
            resource_root=root / "resources",
        )

    def _runtime(
        self,
        version: str = "0.3.1",
        *,
        packaged: bool = False,
    ) -> RuntimeProductIdentity:
        return RuntimeProductIdentity(
            ProductVersion.parse(version, 1), "macos", "arm64", packaged
        )

    def _config(self, public_key: bytes) -> ProductConfigResult:
        return ProductConfigResult(
            CONFIG_SUCCESS,
            ProductClientConfig(
                "https://product.example.com",
                False,
                {KEY_ID: public_key},
                {"release-key-runtime-001": b"r" * 32},
            ),
        )

    def test_missing_product_configuration_is_honest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_NOT_CONFIGURED),
                runtime_identity=self._runtime(),
            )
            state = service.local_state()
        self.assertEqual(state.status, STATUS_NOT_CONFIGURED)
        self.assertIn("<redacted>", repr(service))

    def test_configured_service_without_license_is_not_activated(self) -> None:
        config = ProductClientConfig(
            "https://product.example.com",
            False,
            {"ent-key-001": b"e" * 32},
            {"rel-key-001": b"r" * 32},
        )
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_SUCCESS, config),
                runtime_identity=self._runtime(),
            )
            state = service.local_state()
        self.assertEqual(state.status, STATUS_NOT_ACTIVATED)
        self.assertIsNone(service.check_updates())

    def test_invalid_frozen_build_identity_remains_packaged_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            product_runtime_module.sys,
            "frozen",
            True,
            create=True,
        ), mock.patch(
            "core.product_runtime.load_runtime_product_identity",
            side_effect=RuntimeError("invalid metadata"),
        ):
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=MissingStore(),
                config_result=ProductConfigResult(CONFIG_NOT_CONFIGURED),
            )
            state = service.local_state()
        self.assertTrue(service.packaged_runtime_expected)
        self.assertEqual(state.status, "invalid")

    def test_exact_version_entitlement_survives_offline_restart_but_not_paid_upgrade(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            config = self._config(public_key)
            old_runtime = self._runtime("1.0.0", packaged=True)
            first = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=old_runtime,
            )
            fingerprint = first.device_fingerprint()
            self.assertIsNotNone(fingerprint)
            cache = SignedEntitlementCache(
                paths.data_dir / "entitlements",
                trusted_public_keys={KEY_ID: public_key},
            )
            stored = cache.store_verified(
                _certificate(
                    signing_key,
                    fingerprint=fingerprint,
                    version="1.0.0",
                ),
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            self.assertTrue(stored.ok)
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)

            self.assertEqual(first.local_state().status, STATUS_ENTITLED)
            restarted_offline = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=old_runtime,
            )
            self.assertEqual(
                restarted_offline.local_state().status,
                STATUS_ENTITLED,
            )
            paid_new_release = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=self._runtime("1.1.0", packaged=True),
            )
            self.assertEqual(
                paid_new_release.local_state().status,
                STATUS_NOT_ACTIVATED,
            )
            self.assertEqual(first.local_state().status, STATUS_ENTITLED)

            certificate_path = cache.certificate_path(
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            certificate_path.write_text("corrupted", encoding="utf-8")
            self.assertEqual(first.local_state().status, STATUS_INVALID)

    def test_cache_dependency_failures_are_not_mislabeled_as_invalid(self) -> None:
        store = MemoryStore()
        public_key = b"e" * 32
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=self._runtime("1.0.0", packaged=True),
            )
            self.assertIsNotNone(service.device_fingerprint())
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)
            for cache_status, expected in (
                ("not_available", STATUS_NOT_AVAILABLE),
                ("failed", "failed"),
            ):
                with self.subTest(cache_status=cache_status):
                    service._cache = SimpleNamespace(
                        load_verified=lambda **_: SimpleNamespace(
                            status=cache_status,
                            ok=False,
                        )
                    )
                    self.assertEqual(service.local_state().status, expected)

    def test_activation_claim_is_not_success_until_local_readback_verifies(self) -> None:
        store = ReadbackUnavailableStore()
        with tempfile.TemporaryDirectory() as temp:
            service = ProductRuntimeService(
                app_paths=self._paths(Path(temp)),
                secure_store=store,
                config_result=self._config(b"e" * 32),
                runtime_identity=self._runtime("1.0.0", packaged=True),
            )
            service._activation = SimpleNamespace(
                activate=lambda *args, **kwargs: SimpleNamespace(
                    ok=True,
                    license_id=LICENSE_ID,
                )
            )
            outcome = service.activate("test-activation-key")
        self.assertEqual(outcome.status, STATUS_NOT_AVAILABLE)
        self.assertFalse(outcome.ok)

    def test_initial_purchase_persists_pending_then_unlocks_offline_restart(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            config = self._config(public_key)
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            fingerprint = service.device_fingerprint()
            self.assertIsNotNone(fingerprint)
            offer = InitialPurchaseOffer(
                "purchase_" + ("1" * 32),
                "private-purchase-grant",
                "rel_runtime_001",
                "1.0.0",
                125_000,
                "UZS",
                ("macos",),
                "Feature",
                "Функция",
                "Fix",
                "Исправление",
                "configured",
                "Transfer",
                "Перевод",
                "Recipient",
                "Pay",
                "Оплатите",
                "2026-07-14T00:02:00Z",
            )
            purchase = SimpleNamespace(
                prepare_initial_purchase=lambda **_: InitialPurchaseOfferResult(
                    STATUS_PURCHASE_REQUIRED,
                    "offer",
                    offer,
                ),
                submit_initial_payment=lambda *args, **kwargs: PurchaseResult(
                    STATUS_SUBMITTED,
                    "submitted",
                    release_id=offer.release_id,
                    license_id=LICENSE_ID,
                    purchase_id=offer.purchase_id,
                    version="1.0.0",
                    payment_id="pay_runtime_001",
                    payment_state="pending",
                    price_minor=125_000,
                    currency="UZS",
                ),
            )
            service._purchase = purchase
            prepared = service.prepare_initial_purchase(
                purchase_id=offer.purchase_id
            )
            self.assertTrue(prepared.ready)
            submitted = service.submit_initial_purchase(
                paid_at="2026-07-14T00:00:00Z",
                screenshot=b"sanitized-image",
                content_type="image/png",
            )
            self.assertEqual(submitted.status, STATUS_SUBMITTED)
            self.assertEqual(
                store.get(
                    LICENSE_STATE_SERVICE,
                    PENDING_PURCHASE_ACCOUNT,
                ).value,
                f"{offer.purchase_id}|{LICENSE_ID}|1.0.0",
            )

            purchase.poll_status = lambda **_: PurchaseResult(
                STATUS_REJECTED,
                "rejected",
                release_id=offer.release_id,
                version="1.0.0",
                payment_id="pay_runtime_001",
                payment_state="rejected",
                price_minor=125_000,
                currency="UZS",
                rejection_reason="Receipt is unreadable",
            )
            rejected = service.poll_initial_purchase()
            self.assertEqual(rejected.status, STATUS_REJECTED)
            self.assertEqual(rejected.rejection_reason, "Receipt is unreadable")
            self.assertIsNone(
                store.get(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT).value
            )

            cached = service._cache.store_verified(
                _certificate(
                    signing_key,
                    fingerprint=fingerprint,
                    version="1.0.0",
                ),
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            self.assertTrue(cached.ok)
            purchase.poll_status = lambda **_: PurchaseResult(
                "entitled",
                "approved",
                release_id=offer.release_id,
                version="1.0.0",
                payment_id="pay_runtime_002",
                payment_state="approved",
                price_minor=125_000,
                currency="UZS",
                certificate=cached.certificate,
            )
            approved = service.poll_initial_purchase()
            self.assertTrue(approved.entitled)
            self.assertEqual(service.local_state().status, STATUS_ENTITLED)
            self.assertIsNone(
                store.get(
                    LICENSE_STATE_SERVICE,
                    PENDING_PURCHASE_ACCOUNT,
                ).value
            )

            restarted = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            self.assertEqual(restarted.local_state().status, STATUS_ENTITLED)

    def _submitted(self, offer: InitialPurchaseOffer) -> PurchaseResult:
        return PurchaseResult(
            STATUS_SUBMITTED,
            "submitted",
            release_id=offer.release_id,
            license_id=LICENSE_ID,
            purchase_id=offer.purchase_id,
            version="1.0.0",
            payment_id="pay_runtime_001",
            payment_state="pending",
            price_minor=125_000,
            currency="UZS",
        )

    def test_response_loss_keeps_durable_request_and_restart_resumes_same_key(
        self,
    ) -> None:
        public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            config = self._config(public_key)
            offer = _offer("purchase_" + ("1" * 32))

            first = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            first_purchase = _RecordingPurchase(
                offer,
                results=[PurchaseResult(STATUS_SERVER_UNAVAILABLE, "lost")],
            )
            first._purchase = first_purchase
            self.assertTrue(
                first.prepare_initial_purchase(purchase_id=offer.purchase_id).ready
            )
            lost = first.submit_initial_purchase(
                paid_at="2026-07-14T00:00:00Z",
                screenshot=b"sanitized-image",
                content_type="image/png",
            )
            self.assertEqual(lost.status, STATUS_SERVER_UNAVAILABLE)
            self.assertEqual(len(first_purchase.submissions), 1)
            original_key = first_purchase.submissions[0][0]
            # The durable request survives; no pending purchase was recorded.
            peek = first._payment_requests.peek()
            assert peek.envelope is not None
            self.assertIs(peek.envelope.state, PaymentRequestState.PENDING)
            self.assertIsNone(
                store.get(LICENSE_STATE_SERVICE, PENDING_PURCHASE_ACCOUNT).value
            )

            # Restart: a fresh service instance sharing the same durable stores.
            restarted = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            restart_purchase = _RecordingPurchase(
                offer, results=[self._submitted(offer)]
            )
            restarted._purchase = restart_purchase
            resumed = restarted.resume_pending_payment()
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed.status, STATUS_SUBMITTED)
            # Exactly one submission, reusing the original key and bytes.
            self.assertEqual(len(restart_purchase.submissions), 1)
            self.assertEqual(restart_purchase.submissions[0][0], original_key)
            self.assertEqual(restart_purchase.submissions[0][1], b"sanitized-image")
            self.assertEqual(
                store.get(LICENSE_STATE_SERVICE, PENDING_PURCHASE_ACCOUNT).value,
                f"{offer.purchase_id}|{LICENSE_ID}|1.0.0",
            )
            self.assertIsNone(restarted._payment_requests.peek().envelope)

    def test_retry_reuses_durable_request_ignoring_a_new_screenshot(self) -> None:
        public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            config = self._config(public_key)
            offer = _offer("purchase_" + ("1" * 32))
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            purchase = _RecordingPurchase(
                offer,
                results=[
                    PurchaseResult(STATUS_SERVER_UNAVAILABLE, "lost"),
                    self._submitted(offer),
                ],
            )
            service._purchase = purchase
            service.prepare_initial_purchase(purchase_id=offer.purchase_id)
            lost = service.submit_initial_purchase(
                paid_at="2026-07-14T00:00:00Z",
                screenshot=b"original-image",
                content_type="image/png",
            )
            self.assertEqual(lost.status, STATUS_SERVER_UNAVAILABLE)
            original_key = purchase.submissions[0][0]

            # The user retries and picks a different file; the durable request
            # (same key, same original bytes) is reused, not the new pick.
            retried = service.submit_initial_purchase(
                paid_at="2026-07-14T00:10:00Z",
                screenshot=b"a-completely-different-pick",
                content_type="image/png",
            )
            self.assertEqual(retried.status, STATUS_SUBMITTED)
            self.assertEqual(len(purchase.submissions), 2)
            self.assertEqual(purchase.submissions[1][0], original_key)
            self.assertEqual(purchase.submissions[1][1], b"original-image")
            self.assertIsNone(service._payment_requests.peek().envelope)

    def test_update_response_loss_restart_reuses_same_key_and_evidence(self) -> None:
        public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            state = LocalProductState(STATUS_ENTITLED, runtime, LICENSE_ID)
            candidate = SimpleNamespace(
                release_id="rel_runtime_update_001",
                target=SimpleNamespace(version="1.1.0"),
            )
            first = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=runtime,
            )
            self.assertIsNotNone(first.device_fingerprint())
            first._last_update_candidate = candidate
            first_purchase = _RecordingUpdatePurchase(
                [PurchaseResult(STATUS_SERVER_UNAVAILABLE, "lost response")]
            )
            first._purchase = first_purchase
            with mock.patch.object(
                ProductRuntimeService, "local_state", return_value=state
            ):
                lost = first.submit_update_payment(
                    paid_at="2026-07-14T00:00:00Z",
                    screenshot=b"sanitized-update-image",
                    content_type="image/png",
                )
            self.assertIsNotNone(lost)
            assert lost is not None
            self.assertEqual(lost.status, STATUS_SERVER_UNAVAILABLE)
            original_key = first_purchase.submissions[0]["submission_id"]
            pending = first._payment_requests.peek()
            self.assertIsNotNone(pending.envelope)

            restarted = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=runtime,
            )
            restart_purchase = _RecordingUpdatePurchase(
                [PurchaseResult(STATUS_SUBMITTED, "submitted")]
            )
            restarted._purchase = restart_purchase
            with mock.patch.object(
                ProductRuntimeService, "local_state", return_value=state
            ):
                resumed = restarted.resume_pending_payment()
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed.status, STATUS_SUBMITTED)
            self.assertEqual(
                restart_purchase.submissions[0]["submission_id"], original_key
            )
            self.assertEqual(
                restart_purchase.submissions[0]["screenshot"],
                b"sanitized-update-image",
            )
            self.assertIsNone(restarted._payment_requests.peek().envelope)

    def test_secure_store_unavailable_blocks_durable_submit(self) -> None:
        public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        store = EnvelopeUnavailableStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            config = self._config(public_key)
            offer = _offer("purchase_" + ("1" * 32))
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=config,
                runtime_identity=runtime,
            )
            purchase = _RecordingPurchase(offer, results=[self._submitted(offer)])
            service._purchase = purchase
            service.prepare_initial_purchase(purchase_id=offer.purchase_id)
            result = service.submit_initial_purchase(
                paid_at="2026-07-14T00:00:00Z",
                screenshot=b"sanitized-image",
                content_type="image/png",
            )
            self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
            # No network submission is attempted without a durable record.
            self.assertEqual(len(purchase.submissions), 0)

    def test_install_rechecks_exact_target_entitlement_before_mutation(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=runtime,
            )
            fingerprint = service.device_fingerprint()
            self.assertIsNotNone(fingerprint)
            assert fingerprint is not None
            for version in ("1.0.0", "1.1.0"):
                stored = service._cache.store_verified(
                    _certificate(
                        signing_key,
                        fingerprint=fingerprint,
                        version=version,
                    ),
                    license_id=LICENSE_ID,
                    device_fingerprint=fingerprint,
                    version=version,
                )
                self.assertTrue(stored.ok)
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)
            content = b"verified macOS app archive"
            staged_path = paths.update_staging_dir / "JARVIS.app.zip"
            staged_path.parent.mkdir(parents=True)
            staged_path.write_bytes(content)
            staged = VerifiedStagedUpdate(
                staged_path,
                runtime.product_version,
                ProductVersion.parse("1.1.0", 2),
                hashlib.sha256(content).hexdigest(),
                len(content),
            )
            expected = UpdateTransactionResult(
                TransactionStatus.INSTALLED,
                "installed",
                staged.source,
                staged.target,
            )
            coordinator = _RecordingUpdateCoordinator(expected)
            service._coordinator = coordinator
            service._last_staged_update = staged

            result = service.apply_staged_update()

            self.assertIs(result, expected)
            self.assertEqual(coordinator.applied, [staged])
            self.assertFalse(service.staged_update_ready)

    def test_missing_target_entitlement_blocks_installer_and_preserves_stage(self) -> None:
        signing_key = Ed25519PrivateKey.generate()
        public_key = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=self._config(public_key),
                runtime_identity=runtime,
            )
            fingerprint = service.device_fingerprint()
            self.assertIsNotNone(fingerprint)
            assert fingerprint is not None
            current = service._cache.store_verified(
                _certificate(
                    signing_key,
                    fingerprint=fingerprint,
                    version="1.0.0",
                ),
                license_id=LICENSE_ID,
                device_fingerprint=fingerprint,
                version="1.0.0",
            )
            self.assertTrue(current.ok)
            store.set(LICENSE_STATE_SERVICE, LICENSE_ID_ACCOUNT, LICENSE_ID)
            content = b"verified macOS app archive"
            staged_path = paths.update_staging_dir / "JARVIS.app.zip"
            staged_path.parent.mkdir(parents=True)
            staged_path.write_bytes(content)
            staged = VerifiedStagedUpdate(
                staged_path,
                runtime.product_version,
                ProductVersion.parse("1.1.0", 2),
                hashlib.sha256(content).hexdigest(),
                len(content),
            )
            coordinator = _RecordingUpdateCoordinator(
                UpdateTransactionResult(
                    TransactionStatus.INSTALLED,
                    "must not run",
                    staged.source,
                    staged.target,
                )
            )
            service._coordinator = coordinator
            service._last_staged_update = staged

            result = service.apply_staged_update()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, TransactionStatus.INVALID)
            self.assertEqual(coordinator.applied, [])
            self.assertTrue(service.staged_update_ready)

    def test_staged_source_mismatch_blocks_installer(self) -> None:
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            paths = self._paths(Path(temp))
            runtime = self._runtime("1.0.0", packaged=True)
            service = ProductRuntimeService(
                app_paths=paths,
                secure_store=store,
                config_result=self._config(b"e" * 32),
                runtime_identity=runtime,
            )
            staged = VerifiedStagedUpdate(
                paths.update_staging_dir / "JARVIS.app.zip",
                ProductVersion.parse("0.9.0", 1),
                ProductVersion.parse("1.1.0", 2),
                "0" * 64,
                1,
            )
            coordinator = _RecordingUpdateCoordinator(
                UpdateTransactionResult(TransactionStatus.INSTALLED, "must not run")
            )
            service._coordinator = coordinator
            service._last_staged_update = staged
            entitled = LocalProductState(STATUS_ENTITLED, runtime, LICENSE_ID)

            with mock.patch.object(
                ProductRuntimeService,
                "local_state",
                return_value=entitled,
            ):
                result = service.apply_staged_update()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, TransactionStatus.INVALID)
            self.assertEqual(coordinator.applied, [])


if __name__ == "__main__":
    unittest.main()

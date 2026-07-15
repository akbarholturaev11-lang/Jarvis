"""Application services for the product backend API boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from core.product_state import PaymentState
from core.release_manifest import ArtifactKind

from .api_ports import (
    AdminAccountPage,
    AdminLicensePage,
    AdminReleasePage,
    DeviceChallengePort,
    PaymentStatusRecord,
    PrivatePaymentEvidenceStore,
    ProductReadStore,
    ReleaseCatalogRecord,
)
from .device_challenges import (
    DeviceChallengeAction,
    DeviceChallengeResult,
)
from .models import (
    MAX_PAYMENT_SCREENSHOT_BYTES,
    AdminDecisionAudit,
    ApprovalResult,
    Entitlement,
    InitialPurchaseResult,
    PaymentSubmission,
    Release,
    ReleaseArtifact,
    VerifiedDevicePrincipal,
)
from .private_storage import PrivateObjectMetadata
from .repository import CommerceRepository


class BackendServiceNotAvailableError(RuntimeError):
    """A required backend dependency or compensating action failed."""


@dataclass(frozen=True, slots=True)
class ReleaseAdminDetail:
    release: Release
    artifacts: tuple[ReleaseArtifact, ...]


@dataclass(frozen=True, slots=True)
class CustomerVersionStatus:
    version: str
    release_id: str
    release_state: str
    price_minor: int
    currency: str
    entitled: bool
    entitlement_granted_at: str | None
    payment_id: str | None
    payment_state: str | None
    rejection_reason: str | None
    active_device_bound: bool
    features_en: str
    features_ru: str
    fixes_en: str
    fixes_ru: str


@dataclass(frozen=True, slots=True)
class PaymentEvidence:
    content: bytes
    content_type: str


class ProductBackendService:
    def __init__(
        self,
        commerce: CommerceRepository,
        reads: ProductReadStore,
        evidence_store: PrivatePaymentEvidenceStore,
        challenges: DeviceChallengePort,
    ) -> None:
        if not isinstance(commerce, CommerceRepository):
            raise TypeError("commerce must implement CommerceRepository")
        if not isinstance(reads, ProductReadStore):
            raise TypeError("reads must implement ProductReadStore")
        if not isinstance(evidence_store, PrivatePaymentEvidenceStore):
            raise TypeError(
                "evidence_store must implement PrivatePaymentEvidenceStore"
            )
        if not isinstance(challenges, DeviceChallengePort):
            raise TypeError("challenges must implement DeviceChallengePort")
        self._commerce = commerce
        self._reads = reads
        self._evidence_store = evidence_store
        self._challenges = challenges

    def list_catalog(self, *, limit: int = 50) -> Sequence[ReleaseCatalogRecord]:
        return self._reads.list_published_releases(limit=limit)

    def list_admin_accounts(
        self,
        *,
        limit: int,
        offset: int,
    ) -> AdminAccountPage:
        return self._reads.list_admin_accounts(limit=limit, offset=offset)

    def list_admin_licenses(
        self,
        *,
        account_id: str | None,
        limit: int,
        offset: int,
        entitlements_limit: int,
    ) -> AdminLicensePage:
        return self._reads.list_admin_licenses(
            account_id=account_id,
            limit=limit,
            offset=offset,
            entitlements_limit=entitlements_limit,
        )

    def list_admin_releases(
        self,
        *,
        limit: int,
        offset: int,
    ) -> AdminReleasePage:
        return self._reads.list_admin_releases(limit=limit, offset=offset)

    def create_release(
        self,
        *,
        version: str,
        price_minor: int,
        currency: str,
        features_en: str = "",
        features_ru: str = "",
        fixes_en: str = "",
        fixes_ru: str = "",
    ) -> Release:
        return self._commerce.create_release(
            version,
            price_minor=price_minor,
            currency=currency,
            features_en=features_en,
            features_ru=features_ru,
            fixes_en=fixes_en,
            fixes_ru=fixes_ru,
        )

    def get_release_detail(self, release_id: str) -> ReleaseAdminDetail | None:
        release = self._reads.get_release(release_id)
        if release is None:
            return None
        return ReleaseAdminDetail(
            release,
            tuple(self._reads.list_release_artifacts(release_id)),
        )

    def add_release_artifact(
        self,
        release_id: str,
        *,
        platform: str,
        architecture: str,
        artifact_kind: ArtifactKind,
        build: int,
        sha256: str,
        byte_size: int,
        storage_key: str,
        signature: str,
        signing_key_id: str,
        compatible_source_versions: Sequence[str],
    ) -> ReleaseArtifact:
        return self._commerce.add_release_artifact(
            release_id,
            platform=platform,
            architecture=architecture,
            artifact_kind=artifact_kind,
            build=build,
            sha256=sha256,
            byte_size=byte_size,
            storage_key=storage_key,
            signature=signature,
            signing_key_id=signing_key_id,
            compatible_source_versions=compatible_source_versions,
        )

    def publish_release(self, release_id: str) -> Release:
        return self._commerce.publish_release(release_id)

    def list_payments(
        self,
        *,
        state: PaymentState | None,
        limit: int,
    ) -> Sequence[PaymentStatusRecord]:
        return self._reads.list_payments(state=state, limit=limit)

    def submit_payment_evidence(
        self,
        license_id: str,
        release_id: str,
        *,
        content: bytes,
        content_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
        now: datetime | None = None,
    ) -> PaymentSubmission:
        timestamp = datetime.now(timezone.utc) if now is None else now
        metadata = self._evidence_store.store_payment_screenshot(
            content,
            content_type=content_type,
            now=timestamp,
        )
        try:
            payment = self._commerce.submit_payment(
                license_id,
                release_id,
                screenshot_storage_key=metadata.storage_key,
                screenshot_sha256=metadata.sha256,
                screenshot_byte_size=metadata.byte_size,
                screenshot_mime_type=metadata.content_type,
                paid_at=paid_at,
                client_submission_id=client_submission_id,
                supersedes_payment_id=supersedes_payment_id,
            )
        except Exception:
            self._discard_payment_evidence(metadata)
            raise
        if payment.screenshot_storage_key != metadata.storage_key:
            # An exact idempotent retry returns the original row. The newly
            # stored object is not authoritative and must not become an orphan.
            self._discard_payment_evidence(metadata)
        return payment

    def submit_initial_purchase_evidence(
        self,
        *,
        purchase_id: str,
        release_id: str,
        device_principal: VerifiedDevicePrincipal,
        content: bytes,
        content_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
        now: datetime | None = None,
    ) -> InitialPurchaseResult:
        """Persist private evidence and atomically enroll a proved purchase.

        Evidence is written first because the database stores only its private
        reference. Any rejected transaction, or any idempotent retry that reuses
        the original reference, compensates by deleting the newly written object.
        """

        timestamp = datetime.now(timezone.utc) if now is None else now
        metadata = self._evidence_store.store_payment_screenshot(
            content,
            content_type=content_type,
            now=timestamp,
        )
        try:
            result = self._commerce.submit_initial_purchase(
                purchase_id=purchase_id,
                release_id=release_id,
                device_principal=device_principal,
                screenshot_storage_key=metadata.storage_key,
                screenshot_sha256=metadata.sha256,
                screenshot_byte_size=metadata.byte_size,
                screenshot_mime_type=metadata.content_type,
                paid_at=paid_at,
                client_submission_id=client_submission_id,
                supersedes_payment_id=supersedes_payment_id,
            )
        except Exception:
            self._discard_payment_evidence(metadata)
            raise
        if result.payment.screenshot_storage_key != metadata.storage_key:
            self._discard_payment_evidence(metadata)
        return result

    def _discard_payment_evidence(self, metadata: PrivateObjectMetadata) -> None:
        try:
            self._evidence_store.discard_payment_screenshot(metadata)
        except Exception as discard_error:
            raise BackendServiceNotAvailableError(
                "Payment evidence compensation failed."
            ) from discard_error

    def start_payment_review(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> PaymentSubmission:
        return self._commerce.start_payment_review(
            payment_id, admin_subject=admin_subject
        )

    def approve_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> ApprovalResult:
        return self._commerce.approve_payment(
            payment_id, admin_subject=admin_subject
        )

    def reject_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
        reason: str,
    ) -> PaymentSubmission:
        return self._commerce.reject_payment(
            payment_id,
            admin_subject=admin_subject,
            reason=reason,
        )

    def list_audit(self, *, limit: int) -> tuple[AdminDecisionAudit, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        decisions = tuple(self._commerce.list_admin_decisions())
        return tuple(reversed(decisions[-limit:]))

    def read_payment_evidence(self, payment_id: str) -> PaymentEvidence | None:
        record = self._reads.get_payment(payment_id)
        if record is None:
            return None
        payment = record.payment
        metadata = PrivateObjectMetadata(
            payment.screenshot_storage_key,
            payment.screenshot_sha256,
            payment.screenshot_byte_size,
            payment.screenshot_mime_type,
            payment.submitted_at,
        )
        content = self._evidence_store.read_private_object(
            metadata,
            maximum_bytes=MAX_PAYMENT_SCREENSHOT_BYTES,
        )
        return PaymentEvidence(content, payment.screenshot_mime_type)

    def customer_version_status(
        self,
        license_id: str,
        version: str,
    ) -> CustomerVersionStatus | None:
        release = self._reads.get_release_by_version(version)
        if release is None:
            return None
        entitlement: Entitlement | None = self._commerce.get_entitlement(
            license_id, version
        )
        payment = self._reads.get_latest_payment_for_release(
            license_id, release.id
        )
        active_device = self._commerce.get_active_device(license_id)
        return CustomerVersionStatus(
            release.version,
            release.id,
            release.state.value,
            release.price_minor,
            release.currency,
            entitlement is not None,
            None if entitlement is None else entitlement.granted_at,
            None if payment is None else payment.payment.id,
            None if payment is None else payment.payment.state.value,
            None if payment is None else payment.payment.rejection_reason,
            active_device is not None,
            release.features_en,
            release.features_ru,
            release.fixes_en,
            release.fixes_ru,
        )

    def issue_device_challenge(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> DeviceChallengeResult:
        return self._challenges.issue(
            license_id=license_id,
            device_key_fingerprint=device_key_fingerprint,
            action=action,
            resource_id=resource_id,
        )

    def verify_device_challenge(
        self,
        *,
        challenge_id: str,
        challenge_nonce: str,
        public_key_base64: str,
        signature_base64: str,
    ) -> DeviceChallengeResult:
        return self._challenges.verify_and_consume(
            challenge_id=challenge_id,
            challenge_nonce=challenge_nonce,
            public_key_base64=public_key_base64,
            signature_base64=signature_base64,
        )


__all__ = [
    "BackendServiceNotAvailableError",
    "CustomerVersionStatus",
    "PaymentEvidence",
    "ProductBackendService",
    "ReleaseAdminDetail",
]

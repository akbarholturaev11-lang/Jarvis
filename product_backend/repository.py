"""Repository contract for the JARVIS commerce domain.

The interface is intentionally independent of SQLite.  A production PostgreSQL
adapter can implement this contract without changing admin/client domain flows;
private object storage remains an upstream concern represented by opaque keys.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from core.release_manifest import ArtifactKind

from .models import (
    Account,
    AdminDecisionAudit,
    ApprovalResult,
    DeviceBinding,
    Entitlement,
    InstallAuthorization,
    InstallMode,
    InitialPurchaseResult,
    License,
    PaymentSubmission,
    Release,
    ReleaseArtifact,
    VerifiedDevicePrincipal,
)


@runtime_checkable
class CommerceRepository(Protocol):
    def create_account(self, external_subject: str) -> Account: ...

    def issue_license(self, account_id: str) -> License: ...

    def activate_device(
        self,
        license_id: str,
        device_key_fingerprint: str,
        *,
        platform: str,
        architecture: str,
        device_label: str | None = None,
    ) -> DeviceBinding: ...

    def replace_device(
        self,
        license_id: str,
        *,
        current_device_key_fingerprint: str,
        new_device_key_fingerprint: str,
        new_platform: str,
        new_architecture: str,
        replacement_reason: str,
        new_device_label: str | None = None,
    ) -> DeviceBinding: ...

    def list_device_history(self, license_id: str) -> Sequence[DeviceBinding]: ...

    def get_active_device(self, license_id: str) -> DeviceBinding | None: ...

    def create_release(
        self,
        version: str,
        *,
        price_minor: int,
        currency: str,
        features_en: str = "",
        features_ru: str = "",
        fixes_en: str = "",
        fixes_ru: str = "",
    ) -> Release: ...

    def add_release_artifact(
        self,
        release_id: str,
        *,
        platform: str,
        architecture: str,
        artifact_kind: ArtifactKind = ArtifactKind.INITIAL_INSTALLER,
        build: int,
        sha256: str,
        byte_size: int,
        storage_key: str,
        signature: str,
        signing_key_id: str,
        compatible_source_versions: Sequence[str] = (),
    ) -> ReleaseArtifact: ...

    def publish_release(self, release_id: str) -> Release: ...

    def submit_payment(
        self,
        license_id: str,
        release_id: str,
        *,
        screenshot_storage_key: str,
        screenshot_sha256: str,
        screenshot_byte_size: int,
        screenshot_mime_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
    ) -> PaymentSubmission: ...

    def submit_initial_purchase(
        self,
        *,
        purchase_id: str,
        release_id: str,
        device_principal: VerifiedDevicePrincipal,
        screenshot_storage_key: str,
        screenshot_sha256: str,
        screenshot_byte_size: int,
        screenshot_mime_type: str,
        paid_at: str,
        client_submission_id: str,
        supersedes_payment_id: str | None = None,
    ) -> InitialPurchaseResult: ...

    def start_payment_review(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> PaymentSubmission: ...

    def approve_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
    ) -> ApprovalResult: ...

    def reject_payment(
        self,
        payment_id: str,
        *,
        admin_subject: str,
        reason: str,
    ) -> PaymentSubmission: ...

    def get_entitlement(
        self,
        license_id: str,
        version: str,
    ) -> Entitlement | None: ...

    def authorize_install(
        self,
        license_id: str,
        *,
        device_principal: VerifiedDevicePrincipal,
        artifact_id: str,
        install_mode: InstallMode,
        source_version: str | None = None,
        source_build: int | None = None,
    ) -> InstallAuthorization: ...

    def list_admin_decisions(self) -> Sequence[AdminDecisionAudit]: ...

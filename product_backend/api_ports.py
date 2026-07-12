"""Explicit dependency ports for the product backend HTTP/service layer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from core.product_state import PaymentState

from .device_challenges import (
    DeviceChallengeAction,
    DeviceChallengeResult,
)
from .models import (
    PaymentSubmission,
    Release,
    ReleaseArtifact,
)
from .private_storage import PrivateObjectMetadata


@dataclass(frozen=True, slots=True)
class ArtifactTargetSummary:
    id: str
    platform: str
    architecture: str
    artifact_kind: str
    build: int
    byte_size: int
    sha256: str
    signature_verified_at: str
    verification_key_id: str


@dataclass(frozen=True, slots=True)
class ReleaseCatalogRecord:
    release: Release
    artifacts: tuple[ArtifactTargetSummary, ...]


@dataclass(frozen=True, slots=True)
class PaymentStatusRecord:
    payment: PaymentSubmission
    version: str


@runtime_checkable
class ProductReadStore(Protocol):
    def list_published_releases(
        self,
        *,
        limit: int,
    ) -> Sequence[ReleaseCatalogRecord]: ...

    def get_release(self, release_id: str) -> Release | None: ...

    def get_release_by_version(self, version: str) -> Release | None: ...

    def list_release_artifacts(
        self,
        release_id: str,
    ) -> Sequence[ReleaseArtifact]: ...

    def list_payments(
        self,
        *,
        state: PaymentState | None,
        limit: int,
    ) -> Sequence[PaymentStatusRecord]: ...

    def get_payment(
        self,
        payment_id: str,
        *,
        license_id: str | None = None,
    ) -> PaymentStatusRecord | None: ...

    def get_latest_payment_for_release(
        self,
        license_id: str,
        release_id: str,
    ) -> PaymentStatusRecord | None: ...

    def find_update_candidate(
        self,
        *,
        platform: str,
        architecture: str,
        installed_version: str,
        installed_build: int,
    ) -> ReleaseArtifact | None: ...


@runtime_checkable
class PrivatePaymentEvidenceStore(Protocol):
    """Private evidence storage with mandatory compensating deletion."""

    def store_payment_screenshot(
        self,
        content: bytes,
        *,
        content_type: str,
        now: datetime | None = None,
    ) -> PrivateObjectMetadata: ...

    def read_private_object(
        self,
        metadata: PrivateObjectMetadata,
        *,
        maximum_bytes: int,
    ) -> bytes: ...

    def discard_payment_screenshot(
        self,
        metadata: PrivateObjectMetadata,
    ) -> None: ...


@runtime_checkable
class DeviceChallengePort(Protocol):
    def issue(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> DeviceChallengeResult: ...

    def verify_and_consume(
        self,
        *,
        challenge_id: str,
        challenge_nonce: str,
        public_key_base64: str,
        signature_base64: str,
    ) -> DeviceChallengeResult: ...


@runtime_checkable
class EntitlementCertificateSigner(Protocol):
    def sign_entitlement_certificate(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        version: str,
        issued_at: str,
    ) -> str: ...


@runtime_checkable
class ClientActivationPort(Protocol):
    def issue_activation_credential(
        self,
        *,
        license_id: str,
        version: str,
    ) -> object: ...

    def create_activation_challenge(self, **kwargs: object) -> object: ...

    def complete_activation(self, **kwargs: object) -> object: ...

    def issue_entitlement_certificate(
        self,
        *,
        license_id: str,
        device_key_fingerprint: str,
        version: str,
    ) -> str: ...


@runtime_checkable
class VerifiedReleaseArtifactStream(Protocol):
    @property
    def byte_size(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    def read(self, maximum_bytes: int) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class ReleaseArtifactStore(Protocol):
    def open_verified_release_artifact(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_byte_size: int,
    ) -> VerifiedReleaseArtifactStream: ...


__all__ = [
    "ArtifactTargetSummary",
    "ClientActivationPort",
    "DeviceChallengePort",
    "EntitlementCertificateSigner",
    "PaymentStatusRecord",
    "PrivatePaymentEvidenceStore",
    "ProductReadStore",
    "ReleaseArtifactStore",
    "ReleaseCatalogRecord",
    "VerifiedReleaseArtifactStream",
]

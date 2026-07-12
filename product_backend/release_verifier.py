"""Pinned-public-key verifier for commerce release artifact candidates.

The verifier reconstructs the exact canonical manifest payload from the complete
candidate and verifies its detached Ed25519 signature through
``core.release_manifest``.  It never loads a private key and never signs data.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import datetime

from core.release_manifest import (
    ENVELOPE_SCHEMA,
    MANIFEST_SCHEMA,
    SCHEMA_VERSION,
    STATUS_SUCCESS,
    verify_release_manifest,
)

from .models import (
    ArtifactVerificationCandidate,
    ArtifactVerificationError,
    ArtifactVerificationReceipt,
    format_utc_timestamp,
)


_VERIFICATION_ERROR = "release artifact verification failed"


def _canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _candidate_payload(candidate: ArtifactVerificationCandidate) -> dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": candidate.product_id,
        "bundle_id": candidate.bundle_id,
        "version": candidate.release_version,
        "build": candidate.build,
        "platform": candidate.platform,
        "architecture": candidate.architecture,
        "artifact_kind": candidate.artifact_kind.value,
        "sha256": candidate.sha256,
        "byte_size": candidate.byte_size,
        "storage_key": candidate.storage_key,
        "signing_key_id": candidate.signing_key_id,
        "compatible_source_versions": list(candidate.compatible_source_versions),
    }


class PinnedReleaseArtifactVerifier:
    """Verify complete artifact metadata with a snapshot of pinned public keys."""

    __slots__ = ("_clock", "_trusted_public_keys")

    def __init__(
        self,
        trusted_public_keys: Mapping[str, object],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._trusted_public_keys = (
            dict(trusted_public_keys)
            if isinstance(trusted_public_keys, Mapping)
            else {}
        )
        self._clock = clock

    def __repr__(self) -> str:
        return "PinnedReleaseArtifactVerifier(trusted_public_keys=<pinned>)"

    def verify(
        self,
        candidate: ArtifactVerificationCandidate,
    ) -> ArtifactVerificationReceipt:
        if not isinstance(candidate, ArtifactVerificationCandidate):
            raise ArtifactVerificationError(_VERIFICATION_ERROR)
        try:
            payload_bytes = _canonical_json_bytes(_candidate_payload(candidate))
            envelope = _canonical_json_bytes(
                {
                    "schema": ENVELOPE_SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "payload": _base64url(payload_bytes),
                    "signature": candidate.signature,
                }
            )
            result = verify_release_manifest(
                envelope,
                trusted_public_keys=self._trusted_public_keys,
                expected_platform=candidate.platform,
                expected_architecture=candidate.architecture,
                expected_version=candidate.release_version,
                expected_build=candidate.build,
                expected_artifact_kind=candidate.artifact_kind,
                expected_sha256=candidate.sha256,
                expected_byte_size=candidate.byte_size,
                expected_storage_key=candidate.storage_key,
            )
            claims = result.claims
            if result.status != STATUS_SUCCESS or claims is None:
                raise ArtifactVerificationError(_VERIFICATION_ERROR)
            if (
                claims.product_id != candidate.product_id
                or claims.bundle_id != candidate.bundle_id
                or claims.signing_key_id != candidate.signing_key_id
                or tuple(str(item) for item in claims.compatible_source_versions)
                != candidate.compatible_source_versions
            ):
                raise ArtifactVerificationError(_VERIFICATION_ERROR)
            verified_at = format_utc_timestamp(self._clock())
        except ArtifactVerificationError:
            raise
        except Exception as exc:
            raise ArtifactVerificationError(_VERIFICATION_ERROR) from exc
        return ArtifactVerificationReceipt(
            verified_at=verified_at,
            verification_key_id=candidate.signing_key_id,
        )


__all__ = ["PinnedReleaseArtifactVerifier"]

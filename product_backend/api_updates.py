"""Deterministic signed-manifest envelopes and single-use artifact grants."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.release_manifest import ENVELOPE_SCHEMA, MANIFEST_SCHEMA, SCHEMA_VERSION

from .api_auth import (
    AuthenticationCapacityError,
    BackendConfigurationError,
)
from .models import ReleaseArtifact, format_utc_timestamp


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _canonical_json(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def release_manifest_envelope(artifact: ReleaseArtifact) -> str:
    """Reconstruct the exact manifest whose signature was verified on publish."""

    if not isinstance(artifact, ReleaseArtifact):
        raise TypeError("artifact must be a ReleaseArtifact")
    payload = _canonical_json(
        {
            "schema": MANIFEST_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "bundle_id": BUNDLE_ID,
            "version": artifact.identity.version,
            "build": artifact.identity.build,
            "platform": artifact.identity.platform,
            "architecture": artifact.identity.architecture,
            "artifact_kind": artifact.identity.artifact_kind.value,
            "sha256": artifact.sha256,
            "byte_size": artifact.byte_size,
            "storage_key": artifact.storage_key,
            "signing_key_id": artifact.signing_key_id,
            "compatible_source_versions": list(
                artifact.compatible_source_versions
            ),
        }
    )
    return _canonical_json(
        {
            "schema": ENVELOPE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "payload": _base64url(payload),
            "signature": artifact.signature,
        }
    ).decode("utf-8")


@dataclass(frozen=True, slots=True)
class IssuedArtifactDownloadGrant:
    token: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True)
class _ArtifactDownloadRecord:
    token_digest: bytes = field(repr=False)
    artifact: ReleaseArtifact = field(repr=False)
    expires_at: datetime


class ArtifactDownloadGrantManager:
    """Bounded, short-lived, single-use artifact download authorization."""

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = 120,
        max_grants: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(secret) is not bytes or len(secret) < 32:
            raise BackendConfigurationError("artifact grant secret is invalid")
        if not 30 <= ttl_seconds <= 300 or not 1 <= max_grants <= 4096:
            raise BackendConfigurationError("artifact grant bounds are invalid")
        self._secret = secret
        self._ttl_seconds = ttl_seconds
        self._max_grants = max_grants
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: OrderedDict[bytes, _ArtifactDownloadRecord] = OrderedDict()
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BackendConfigurationError("artifact grant clock is invalid")
        return value.astimezone(timezone.utc)

    def _digest(self, token: str) -> bytes:
        return hmac.new(
            self._secret,
            b"artifact-download-grant\x00" + token.encode("ascii", errors="ignore"),
            hashlib.sha256,
        ).digest()

    def issue(self, artifact: ReleaseArtifact) -> IssuedArtifactDownloadGrant:
        if not isinstance(artifact, ReleaseArtifact):
            raise TypeError("verified artifact is required")
        token = _base64url(secrets.token_bytes(32))
        digest = self._digest(token)
        now = self._now()
        expires = now + timedelta(seconds=self._ttl_seconds)
        with self._lock:
            self._prune_locked(now)
            if len(self._records) >= self._max_grants:
                raise AuthenticationCapacityError(
                    "artifact download grant capacity reached"
                )
            self._records[digest] = _ArtifactDownloadRecord(
                digest, artifact, expires
            )
        return IssuedArtifactDownloadGrant(token, format_utc_timestamp(expires))

    def consume(self, token: object, *, artifact_id: str) -> ReleaseArtifact | None:
        if (
            not isinstance(token, str)
            or not 20 <= len(token) <= 128
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in token
            )
        ):
            return None
        digest = self._digest(token)
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            record = self._records.pop(digest, None)
        if (
            record is None
            or not secrets.compare_digest(digest, record.token_digest)
            or record.artifact.id != artifact_id
        ):
            return None
        return record.artifact

    def _prune_locked(self, now: datetime) -> None:
        for digest in [
            key
            for key, record in self._records.items()
            if now >= record.expires_at
        ]:
            self._records.pop(digest, None)


__all__ = [
    "ArtifactDownloadGrantManager",
    "IssuedArtifactDownloadGrant",
    "release_manifest_envelope",
]

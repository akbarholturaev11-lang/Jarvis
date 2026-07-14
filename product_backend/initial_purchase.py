"""Proof-bound bootstrap authority for a device's first product purchase.

The manager owns only short-lived challenge and upload-grant state.  Durable
accounts, licenses, payment records, and entitlements remain in the commerce
repository.  Raw nonces and grant tokens are returned once and are represented
internally only by keyed digests.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final

from core.device_identity import (
    STATUS_NOT_AVAILABLE as DEVICE_PROOF_NOT_AVAILABLE,
    verify_device_challenge,
)

from .models import (
    VerifiedDevicePrincipal,
    format_utc_timestamp,
    normalize_target_architecture,
    normalize_target_platform,
    validate_device_key_fingerprint,
    validate_opaque_identifier,
)


STATUS_SUCCESS: Final = "success"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID: Final = "invalid"
STATUS_EXPIRED: Final = "expired"
STATUS_ALREADY_USED: Final = "already_used"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

DEFAULT_CHALLENGE_TTL_SECONDS: Final = 120
DEFAULT_GRANT_TTL_SECONDS: Final = 120
MIN_AUTHORITY_SECRET_BYTES: Final = 32
_NONCE_BYTES: Final = 32
_TOKEN_BYTES: Final = 32
_PUBLIC_KEY_TEXT_LENGTH: Final = 43
_SIGNATURE_TEXT_LENGTH: Final = 86


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("initial purchase clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _base64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _purchase_id(value: object) -> str:
    normalized = validate_opaque_identifier(value, field="purchase_id")
    if not normalized.startswith("purchase_") or len(normalized) != 41:
        raise ValueError("purchase_id is invalid")
    suffix = normalized.removeprefix("purchase_")
    if any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("purchase_id is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class IssuedInitialPurchaseChallenge:
    id: str
    purchase_id: str = field(repr=False)
    release_id: str = field(repr=False)
    challenge_nonce: str = field(repr=False)
    issued_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class VerifiedInitialPurchase:
    challenge_id: str
    purchase_id: str = field(repr=False)
    release_id: str = field(repr=False)
    device_principal: VerifiedDevicePrincipal = field(repr=False)
    verified_at: str


@dataclass(frozen=True, slots=True)
class IssuedInitialPurchaseGrant:
    token: str = field(repr=False)
    verified: VerifiedInitialPurchase = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True)
class InitialPurchaseGrantReservation:
    token_digest: bytes = field(repr=False)
    reservation_id: str = field(repr=False)
    verified: VerifiedInitialPurchase = field(repr=False)


@dataclass(frozen=True, slots=True)
class InitialPurchaseAuthorityResult:
    status: str
    message: str = field(repr=False)
    challenge: IssuedInitialPurchaseChallenge | None = field(
        default=None,
        repr=False,
    )
    grant: IssuedInitialPurchaseGrant | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        payload = "challenge" if self.challenge else "grant" if self.grant else "none"
        return (
            f"InitialPurchaseAuthorityResult(status={self.status!r}, "
            f"payload={payload!r})"
        )


@dataclass(frozen=True, slots=True)
class _ChallengeRecord:
    nonce_digest: bytes = field(repr=False)
    purchase_id: str = field(repr=False)
    release_id: str = field(repr=False)
    device_key_fingerprint: str = field(repr=False)
    platform: str
    architecture: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _GrantRecord:
    token_digest: bytes = field(repr=False)
    verified: VerifiedInitialPurchase = field(repr=False)
    expires_at: datetime


class InitialPurchaseAuthorizer:
    """Bounded challenge/proof and reservable upload-grant authority.

    Grant expiry gates admission when a request first ``reserve_grant``s an
    upload grant.  A reserved grant is then held for that request and is
    deliberately not time-pruned behind it, so ``commit_grant`` stays
    deterministic after the payment row is persisted and never converts an
    accepted payment into a spurious failure.  The request owner finalizes
    every reservation through ``commit_grant`` or ``release_grant``.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        challenge_ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
        grant_ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        maximum_records: int = 512,
        clock: Callable[[], datetime] = _utc_now,
        nonce_factory: Callable[[int], bytes] = secrets.token_bytes,
        token_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if type(secret) is not bytes or len(secret) < MIN_AUTHORITY_SECRET_BYTES:
            raise ValueError("initial purchase authority secret is invalid")
        if (
            type(challenge_ttl_seconds) is not int
            or not 30 <= challenge_ttl_seconds <= 300
            or type(grant_ttl_seconds) is not int
            or not 30 <= grant_ttl_seconds <= 300
            or type(maximum_records) is not int
            or not 1 <= maximum_records <= 4096
        ):
            raise ValueError("initial purchase authority bounds are invalid")
        self._secret = secret
        self._challenge_ttl = challenge_ttl_seconds
        self._grant_ttl = grant_ttl_seconds
        self._maximum_records = maximum_records
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._token_factory = token_factory
        self._challenges: OrderedDict[str, _ChallengeRecord] = OrderedDict()
        self._grants: OrderedDict[bytes, _GrantRecord] = OrderedDict()
        self._reservations: dict[bytes, str] = {}
        self._lock = threading.RLock()

    def _digest(self, domain: bytes, value: bytes) -> bytes:
        return hmac.new(self._secret, domain + b"\x00" + value, hashlib.sha256).digest()

    def issue_challenge(
        self,
        *,
        purchase_id: str,
        release_id: str,
        device_key_fingerprint: str,
        platform: str,
        architecture: str,
    ) -> InitialPurchaseAuthorityResult:
        try:
            purchase_id = _purchase_id(purchase_id)
            release_id = validate_opaque_identifier(release_id, field="release_id")
            fingerprint = validate_device_key_fingerprint(device_key_fingerprint)
            platform = normalize_target_platform(platform)
            architecture = normalize_target_architecture(architecture)
            nonce = self._nonce_factory(_NONCE_BYTES)
            if type(nonce) is not bytes or len(nonce) != _NONCE_BYTES:
                raise RuntimeError("nonce factory failed")
            now = _aware_utc(self._clock())
        except (TypeError, ValueError):
            return InitialPurchaseAuthorityResult(
                STATUS_INVALID,
                "Initial purchase challenge request is invalid.",
            )
        except Exception:
            return InitialPurchaseAuthorityResult(
                STATUS_FAILED,
                "Initial purchase challenge is unavailable.",
            )
        challenge_id = f"pchl_{uuid.uuid4().hex}"
        expires_at = now + timedelta(seconds=self._challenge_ttl)
        record = _ChallengeRecord(
            self._digest(b"initial-purchase-nonce", nonce),
            purchase_id,
            release_id,
            fingerprint,
            platform,
            architecture,
            expires_at,
        )
        with self._lock:
            self._prune_locked(now)
            if len(self._challenges) + len(self._grants) >= self._maximum_records:
                return InitialPurchaseAuthorityResult(
                    STATUS_NOT_AVAILABLE,
                    "Initial purchase authority is at capacity.",
                )
            self._challenges[challenge_id] = record
        issued = IssuedInitialPurchaseChallenge(
            challenge_id,
            purchase_id,
            release_id,
            _base64url(nonce),
            format_utc_timestamp(now),
            format_utc_timestamp(expires_at),
        )
        return InitialPurchaseAuthorityResult(
            STATUS_SUCCESS,
            "Initial purchase challenge issued.",
            challenge=issued,
        )

    def verify_and_issue_grant(
        self,
        *,
        challenge_id: str,
        challenge_nonce: str,
        public_key_base64: str,
        signature_base64: str,
    ) -> InitialPurchaseAuthorityResult:
        if (
            type(challenge_id) is not str
            or not challenge_id.startswith("pchl_")
            or type(challenge_nonce) is not str
            or type(public_key_base64) is not str
            or len(public_key_base64) != _PUBLIC_KEY_TEXT_LENGTH
            or type(signature_base64) is not str
            or len(signature_base64) != _SIGNATURE_TEXT_LENGTH
        ):
            return InitialPurchaseAuthorityResult(
                STATUS_INVALID,
                "Initial purchase proof is invalid.",
            )
        now = _aware_utc(self._clock())
        with self._lock:
            self._prune_locked(now)
            record = self._challenges.pop(challenge_id, None)
        if record is None:
            return InitialPurchaseAuthorityResult(
                STATUS_NOT_FOUND,
                "Initial purchase challenge was not found or was already used.",
            )
        if now >= record.expires_at:
            return InitialPurchaseAuthorityResult(
                STATUS_EXPIRED,
                "Initial purchase challenge expired.",
            )
        try:
            import base64
            import binascii

            padding = "=" * (-len(challenge_nonce) % 4)
            nonce = base64.b64decode(
                challenge_nonce + padding,
                altchars=b"-_",
                validate=True,
            )
            if _base64url(nonce) != challenge_nonce or len(nonce) != _NONCE_BYTES:
                raise ValueError("nonce is not canonical")
        except (ValueError, binascii.Error):
            return InitialPurchaseAuthorityResult(
                STATUS_INVALID,
                "Initial purchase proof is invalid.",
            )
        expected_nonce = self._digest(b"initial-purchase-nonce", nonce)
        if not secrets.compare_digest(expected_nonce, record.nonce_digest):
            return InitialPurchaseAuthorityResult(
                STATUS_INVALID,
                "Initial purchase proof is invalid.",
            )
        proof = verify_device_challenge(
            public_key_base64=public_key_base64,
            device_key_fingerprint=record.device_key_fingerprint,
            challenge_nonce=challenge_nonce,
            signature_base64=signature_base64,
        )
        if proof.status == DEVICE_PROOF_NOT_AVAILABLE:
            return InitialPurchaseAuthorityResult(
                STATUS_NOT_AVAILABLE,
                "Device proof verification is unavailable.",
            )
        if not proof.ok:
            return InitialPurchaseAuthorityResult(
                STATUS_INVALID,
                "Initial purchase proof is invalid.",
            )
        verified = VerifiedInitialPurchase(
            challenge_id,
            record.purchase_id,
            record.release_id,
            VerifiedDevicePrincipal(
                record.device_key_fingerprint,
                record.platform,
                record.architecture,
                True,
            ),
            format_utc_timestamp(now),
        )
        try:
            token_bytes = self._token_factory(_TOKEN_BYTES)
            if type(token_bytes) is not bytes or len(token_bytes) != _TOKEN_BYTES:
                raise RuntimeError("token factory failed")
        except Exception:
            return InitialPurchaseAuthorityResult(
                STATUS_FAILED,
                "Initial purchase grant is unavailable.",
            )
        token = _base64url(token_bytes)
        digest = self._digest(b"initial-purchase-grant", token_bytes)
        expires_at = now + timedelta(seconds=self._grant_ttl)
        with self._lock:
            self._prune_locked(now)
            if len(self._challenges) + len(self._grants) >= self._maximum_records:
                return InitialPurchaseAuthorityResult(
                    STATUS_NOT_AVAILABLE,
                    "Initial purchase authority is at capacity.",
                )
            self._grants[digest] = _GrantRecord(digest, verified, expires_at)
        return InitialPurchaseAuthorityResult(
            STATUS_SUCCESS,
            "Initial purchase proof verified.",
            grant=IssuedInitialPurchaseGrant(
                token,
                verified,
                format_utc_timestamp(expires_at),
            ),
        )

    def reserve_grant(
        self,
        token: object,
        *,
        purchase_id: str,
        release_id: str,
    ) -> InitialPurchaseGrantReservation | None:
        try:
            purchase_id = _purchase_id(purchase_id)
            release_id = validate_opaque_identifier(release_id, field="release_id")
        except (TypeError, ValueError):
            return None
        if type(token) is not str or not 20 <= len(token) <= 128:
            return None
        try:
            import base64
            import binascii

            padding = "=" * (-len(token) % 4)
            token_bytes = base64.b64decode(
                token + padding,
                altchars=b"-_",
                validate=True,
            )
            if _base64url(token_bytes) != token or len(token_bytes) != _TOKEN_BYTES:
                return None
        except (ValueError, binascii.Error):
            return None
        digest = self._digest(b"initial-purchase-grant", token_bytes)
        now = _aware_utc(self._clock())
        with self._lock:
            self._prune_locked(now)
            record = self._grants.get(digest)
            if record is None or digest in self._reservations:
                return None
            verified = record.verified
            if (
                verified.purchase_id != purchase_id
                or verified.release_id != release_id
                or not verified.device_principal.proof_verified
            ):
                self._grants.pop(digest, None)
                return None
            reservation_id = uuid.uuid4().hex
            self._reservations[digest] = reservation_id
            return InitialPurchaseGrantReservation(
                digest,
                reservation_id,
                verified,
            )

    def commit_grant(self, reservation: object) -> bool:
        if not isinstance(reservation, InitialPurchaseGrantReservation):
            return False
        now = _aware_utc(self._clock())
        with self._lock:
            self._prune_locked(now)
            reserved = self._reservations.get(reservation.token_digest)
            record = self._grants.get(reservation.token_digest)
            if (
                record is None
                or reserved != reservation.reservation_id
                or not secrets.compare_digest(
                    record.token_digest,
                    reservation.token_digest,
                )
            ):
                return False
            self._reservations.pop(reservation.token_digest, None)
            self._grants.pop(reservation.token_digest, None)
            return True

    def release_grant(self, reservation: object) -> bool:
        if not isinstance(reservation, InitialPurchaseGrantReservation):
            return False
        now = _aware_utc(self._clock())
        with self._lock:
            self._prune_locked(now)
            if (
                self._reservations.get(reservation.token_digest)
                != reservation.reservation_id
            ):
                return False
            self._reservations.pop(reservation.token_digest, None)
            return reservation.token_digest in self._grants

    def _prune_locked(self, now: datetime) -> None:
        expired_challenges = [
            key
            for key, record in self._challenges.items()
            if now >= record.expires_at
        ]
        # A grant that an in-flight request has reserved must survive expiry
        # while that request runs.  Otherwise a large or slow upload could push
        # the wall clock past ``expires_at`` after the payment row is already
        # persisted, and the closing ``commit_grant`` would then fail as if the
        # authorization were lost -- turning an accepted, durably stored payment
        # into a spurious 503.  Grant expiry gates admission at ``reserve_grant``
        # time; once reserved, the request owner alone finalizes the grant via
        # ``commit_grant``/``release_grant``, which the ASGI middleware always
        # calls in a ``finally`` block.  This mirrors ``DeviceActionGrantManager``.
        expired_grants = [
            key
            for key, record in self._grants.items()
            if now >= record.expires_at and key not in self._reservations
        ]
        for key in expired_challenges:
            self._challenges.pop(key, None)
        for key in expired_grants:
            self._grants.pop(key, None)
            self._reservations.pop(key, None)

    def __repr__(self) -> str:
        return "InitialPurchaseAuthorizer(state=<private>, secret=<configured>)"


__all__ = [
    "STATUS_ALREADY_USED",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "InitialPurchaseAuthorityResult",
    "InitialPurchaseAuthorizer",
    "InitialPurchaseGrantReservation",
    "IssuedInitialPurchaseChallenge",
    "IssuedInitialPurchaseGrant",
    "VerifiedInitialPurchase",
]

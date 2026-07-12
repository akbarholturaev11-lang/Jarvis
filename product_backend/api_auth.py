"""Fail-closed admin sessions, CSRF, rate limits, and device action grants."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final

from .device_challenges import DeviceChallengeAction, VerifiedDeviceChallenge
from .models import format_utc_timestamp, validate_opaque_identifier


MIN_PBKDF2_ITERATIONS: Final = 200_000
MAX_PBKDF2_ITERATIONS: Final = 2_000_000
MIN_SESSION_SECRET_BYTES: Final = 32


class BackendConfigurationError(RuntimeError):
    """Security configuration is absent or invalid; startup must stop."""


class AuthenticationCapacityError(RuntimeError):
    """A bounded session/grant store is full after expired entries were pruned."""


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url(value: object, *, field_name: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BackendConfigurationError(f"{field_name} is invalid")
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = base64.b64decode(
            padded,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise BackendConfigurationError(f"{field_name} is invalid") from exc
    if _base64url(decoded) != value.rstrip("="):
        raise BackendConfigurationError(f"{field_name} is invalid")
    return decoded


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BackendConfigurationError("security clock must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AdminPasswordCredential:
    subject: str
    salt: bytes = field(repr=False)
    password_digest: bytes = field(repr=False)
    iterations: int

    def __post_init__(self) -> None:
        validate_opaque_identifier(self.subject, field="admin_subject")
        if type(self.salt) is not bytes or not 16 <= len(self.salt) <= 64:
            raise BackendConfigurationError("admin password salt is invalid")
        if type(self.password_digest) is not bytes or len(self.password_digest) != 32:
            raise BackendConfigurationError("admin password digest is invalid")
        if (
            type(self.iterations) is not int
            or not MIN_PBKDF2_ITERATIONS
            <= self.iterations
            <= MAX_PBKDF2_ITERATIONS
        ):
            raise BackendConfigurationError("PBKDF2 iteration count is invalid")

    @classmethod
    def from_encoded(
        cls,
        *,
        subject: str,
        salt_base64url: str,
        password_digest_base64url: str,
        iterations: int,
    ) -> AdminPasswordCredential:
        return cls(
            subject,
            _decode_base64url(salt_base64url, field_name="admin password salt"),
            _decode_base64url(
                password_digest_base64url,
                field_name="admin password digest",
            ),
            iterations,
        )

    @classmethod
    def derive_for_configuration(
        cls,
        *,
        subject: str,
        password: str,
        salt: bytes | None = None,
        iterations: int = MIN_PBKDF2_ITERATIONS,
    ) -> AdminPasswordCredential:
        """Create a hash record for configuration tooling/tests, never storage."""

        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("admin password length is invalid")
        salt_value = secrets.token_bytes(32) if salt is None else salt
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt_value,
            iterations,
            dklen=32,
        )
        return cls(subject, salt_value, digest, iterations)

    def verify(self, password: object) -> bool:
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            self.salt,
            self.iterations,
            dklen=32,
        )
        return secrets.compare_digest(candidate, self.password_digest)


@dataclass(frozen=True, slots=True)
class AdminAuthSettings:
    credentials: tuple[AdminPasswordCredential, ...]
    session_secret: bytes = field(repr=False)
    allowed_hosts: tuple[str, ...]
    session_ttl_seconds: int = 1800
    max_sessions: int = 64
    cookie_name: str = "jarvis_admin_session"
    secure_cookie: bool = True

    def __post_init__(self) -> None:
        if not self.credentials or len(self.credentials) > 32:
            raise BackendConfigurationError("admin credentials are not configured")
        subjects = [credential.subject for credential in self.credentials]
        if len(subjects) != len(set(subjects)):
            raise BackendConfigurationError("admin subjects must be unique")
        if (
            type(self.session_secret) is not bytes
            or len(self.session_secret) < MIN_SESSION_SECRET_BYTES
        ):
            raise BackendConfigurationError("session secret is invalid")
        if not 300 <= self.session_ttl_seconds <= 28_800:
            raise BackendConfigurationError("session TTL is invalid")
        if not 1 <= self.max_sessions <= 256:
            raise BackendConfigurationError("session bound is invalid")
        if (
            not self.allowed_hosts
            or len(self.allowed_hosts) > 32
            or any(
                not isinstance(host, str)
                or not host
                or len(host) > 253
                or host == "*"
                or "/" in host
                for host in self.allowed_hosts
            )
        ):
            raise BackendConfigurationError("allowed hosts are invalid")
        validate_opaque_identifier(self.cookie_name, field="cookie_name")
        if type(self.secure_cookie) is not bool:
            raise BackendConfigurationError("secure_cookie must be boolean")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AdminAuthSettings:
        source = os.environ if environ is None else environ
        required = (
            "JARVIS_ADMIN_SUBJECT",
            "JARVIS_ADMIN_PASSWORD_SALT_B64URL",
            "JARVIS_ADMIN_PASSWORD_HASH_B64URL",
            "JARVIS_ADMIN_PBKDF2_ITERATIONS",
            "JARVIS_ADMIN_SESSION_SECRET_B64URL",
            "JARVIS_API_ALLOWED_HOSTS",
        )
        if any(not source.get(name) for name in required):
            raise BackendConfigurationError(
                "required backend security configuration is missing"
            )
        try:
            iterations = int(source["JARVIS_ADMIN_PBKDF2_ITERATIONS"])
        except (TypeError, ValueError) as exc:
            raise BackendConfigurationError(
                "PBKDF2 iteration count is invalid"
            ) from exc
        credential = AdminPasswordCredential.from_encoded(
            subject=source["JARVIS_ADMIN_SUBJECT"],
            salt_base64url=source["JARVIS_ADMIN_PASSWORD_SALT_B64URL"],
            password_digest_base64url=source[
                "JARVIS_ADMIN_PASSWORD_HASH_B64URL"
            ],
            iterations=iterations,
        )
        session_secret = _decode_base64url(
            source["JARVIS_ADMIN_SESSION_SECRET_B64URL"],
            field_name="session secret",
        )
        hosts = tuple(
            item.strip()
            for item in source["JARVIS_API_ALLOWED_HOSTS"].split(",")
            if item.strip()
        )
        return cls((credential,), session_secret, hosts)


@dataclass(frozen=True, slots=True)
class AdminSessionRecord:
    subject: str
    token_digest: bytes = field(repr=False)
    csrf_digest: bytes = field(repr=False)
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedAdminSession:
    subject: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: str


class AdminSessionManager:
    def __init__(
        self,
        settings: AdminAuthSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(settings, AdminAuthSettings):
            raise BackendConfigurationError("admin auth settings are invalid")
        self._settings = settings
        self._clock = clock
        self._credentials = {
            credential.subject: credential for credential in settings.credentials
        }
        self._sessions: OrderedDict[bytes, AdminSessionRecord] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def settings(self) -> AdminAuthSettings:
        return self._settings

    def _digest(self, label: bytes, token: str) -> bytes:
        return hmac.new(
            self._settings.session_secret,
            label + token.encode("ascii", errors="ignore"),
            hashlib.sha256,
        ).digest()

    def authenticate_and_issue(
        self,
        subject: object,
        password: object,
    ) -> IssuedAdminSession | None:
        credential = self._credentials.get(subject) if isinstance(subject, str) else None
        if credential is None:
            dummy = next(iter(self._credentials.values()))
            dummy.verify(password)
            return None
        if not credential.verify(password):
            return None
        now = _aware_utc(self._clock)
        session_token = _base64url(secrets.token_bytes(32))
        csrf_token = _base64url(secrets.token_bytes(32))
        token_digest = self._digest(b"session\x00", session_token)
        record = AdminSessionRecord(
            credential.subject,
            token_digest,
            self._digest(b"csrf\x00", csrf_token),
            now,
            now + timedelta(seconds=self._settings.session_ttl_seconds),
        )
        with self._lock:
            self._prune_locked(now)
            if len(self._sessions) >= self._settings.max_sessions:
                raise AuthenticationCapacityError("admin session capacity reached")
            self._sessions[token_digest] = record
        return IssuedAdminSession(
            record.subject,
            session_token,
            csrf_token,
            format_utc_timestamp(record.expires_at),
        )

    def resolve(self, session_token: object) -> AdminSessionRecord | None:
        if not isinstance(session_token, str) or not 20 <= len(session_token) <= 128:
            return None
        digest = self._digest(b"session\x00", session_token)
        now = _aware_utc(self._clock)
        with self._lock:
            self._prune_locked(now)
            record = self._sessions.get(digest)
            if record is None or not secrets.compare_digest(
                digest, record.token_digest
            ):
                return None
            return record

    def verify_csrf(
        self,
        record: AdminSessionRecord,
        csrf_token: object,
    ) -> bool:
        if not isinstance(record, AdminSessionRecord):
            return False
        if not isinstance(csrf_token, str) or not 20 <= len(csrf_token) <= 128:
            return False
        candidate = self._digest(b"csrf\x00", csrf_token)
        return secrets.compare_digest(candidate, record.csrf_digest)

    def revoke(self, session_token: object) -> None:
        if not isinstance(session_token, str) or len(session_token) > 128:
            return
        digest = self._digest(b"session\x00", session_token)
        with self._lock:
            self._sessions.pop(digest, None)

    def _prune_locked(self, now: datetime) -> None:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if now >= record.expires_at
        ]
        for digest in expired:
            self._sessions.pop(digest, None)


class BoundedAttemptLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_keys: int = 1024,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not 1 <= max_attempts <= 20 or not 30 <= window_seconds <= 3600:
            raise BackendConfigurationError("attempt limiter settings are invalid")
        if not 1 <= max_keys <= 4096:
            raise BackendConfigurationError("attempt limiter bound is invalid")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.RLock()

    def allowed(self, key: str) -> bool:
        now = _aware_utc(self._clock).timestamp()
        with self._lock:
            attempts = self._attempts.get(key)
            if attempts is None:
                return True
            self._prune_attempts(attempts, now)
            if not attempts:
                self._attempts.pop(key, None)
                return True
            return len(attempts) < self._max_attempts

    def consume(self, key: str) -> bool:
        """Atomically reserve one bounded attempt before expensive work."""

        now = _aware_utc(self._clock).timestamp()
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            self._prune_attempts(attempts, now)
            if len(attempts) >= self._max_attempts:
                return False
            attempts.append(now)
            self._attempts.move_to_end(key)
            while len(self._attempts) > self._max_keys:
                self._attempts.popitem(last=False)
            return True

    def record_failure(self, key: str) -> None:
        now = _aware_utc(self._clock).timestamp()
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            self._prune_attempts(attempts, now)
            attempts.append(now)
            self._attempts.move_to_end(key)
            while len(self._attempts) > self._max_keys:
                self._attempts.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _prune_attempts(self, attempts: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()


@dataclass(frozen=True, slots=True)
class IssuedDeviceGrant:
    token: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True)
class _DeviceGrantRecord:
    token_digest: bytes = field(repr=False)
    verified: VerifiedDeviceChallenge = field(repr=False)
    expires_at: datetime


class DeviceActionGrantManager:
    """Bounded, short-lived, single-use grants from verified challenge results."""

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = 60,
        max_grants: int = 512,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(secret) is not bytes or len(secret) < MIN_SESSION_SECRET_BYTES:
            raise BackendConfigurationError("device grant secret is invalid")
        if not 15 <= ttl_seconds <= 180 or not 1 <= max_grants <= 4096:
            raise BackendConfigurationError("device grant bounds are invalid")
        self._secret = secret
        self._ttl_seconds = ttl_seconds
        self._max_grants = max_grants
        self._clock = clock
        self._grants: OrderedDict[bytes, _DeviceGrantRecord] = OrderedDict()
        self._lock = threading.RLock()

    def _digest(self, token: str) -> bytes:
        return hmac.new(
            self._secret,
            b"device-grant\x00" + token.encode("ascii", errors="ignore"),
            hashlib.sha256,
        ).digest()

    def issue(self, verified: VerifiedDeviceChallenge) -> IssuedDeviceGrant:
        if not isinstance(verified, VerifiedDeviceChallenge):
            raise TypeError("verified challenge is required")
        token = _base64url(secrets.token_bytes(32))
        digest = self._digest(token)
        now = _aware_utc(self._clock)
        record = _DeviceGrantRecord(
            digest,
            verified,
            now + timedelta(seconds=self._ttl_seconds),
        )
        with self._lock:
            self._prune_locked(now)
            if len(self._grants) >= self._max_grants:
                raise AuthenticationCapacityError("device grant capacity reached")
            self._grants[digest] = record
        return IssuedDeviceGrant(token, format_utc_timestamp(record.expires_at))

    def consume(
        self,
        token: object,
        *,
        license_id: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> VerifiedDeviceChallenge | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        digest = self._digest(token)
        now = _aware_utc(self._clock)
        with self._lock:
            self._prune_locked(now)
            record = self._grants.pop(digest, None)
        if record is None or not secrets.compare_digest(
            digest, record.token_digest
        ):
            return None
        verified = record.verified
        if (
            verified.license_id != license_id
            or verified.action is not action
            or verified.resource_id != resource_id
            or not verified.device_principal.proof_verified
        ):
            return None
        return verified

    def _prune_locked(self, now: datetime) -> None:
        expired = [
            digest
            for digest, record in self._grants.items()
            if now >= record.expires_at
        ]
        for digest in expired:
            self._grants.pop(digest, None)


__all__ = [
    "AdminAuthSettings",
    "AdminPasswordCredential",
    "AdminSessionManager",
    "AdminSessionRecord",
    "AuthenticationCapacityError",
    "BackendConfigurationError",
    "BoundedAttemptLimiter",
    "DeviceActionGrantManager",
    "IssuedAdminSession",
    "IssuedDeviceGrant",
]

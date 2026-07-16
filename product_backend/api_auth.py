"""Fail-closed admin sessions, CSRF, rate limits, and device action grants."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final, Protocol

from .device_challenges import DeviceChallengeAction, VerifiedDeviceChallenge
from .models import format_utc_timestamp, validate_opaque_identifier


MIN_PBKDF2_ITERATIONS: Final = 200_000
MAX_PBKDF2_ITERATIONS: Final = 2_000_000
MIN_SESSION_SECRET_BYTES: Final = 32


class SessionAssurance(StrEnum):
    """How strongly the current admin session was authenticated.

    ``MFA_PENDING`` is a password-only session issued only so an operator whose
    account still requires a second factor can complete enrollment or step-up.
    It must never authorize protected admin mutations.  ``MFA_SATISFIED`` is a
    fully authenticated session.
    """

    MFA_PENDING = "mfa_pending"
    MFA_SATISFIED = "mfa_satisfied"


class PasswordChangeResult(StrEnum):
    """Outcome of a password rotation without exposing credential details."""

    CHANGED = "changed"
    INVALID_CURRENT_PASSWORD = "invalid_current_password"
    NOT_AVAILABLE = "not_available"


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


class AdminCredentialStore(Protocol):
    """Persistent password-hash storage injected into the session manager."""

    def load_credentials(self) -> tuple[AdminPasswordCredential, ...]: ...

    def replace_credential(self, credential: AdminPasswordCredential) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminAuthSettings:
    credentials: tuple[AdminPasswordCredential, ...]
    session_secret: bytes = field(repr=False)
    allowed_hosts: tuple[str, ...]
    session_ttl_seconds: int = 1800
    max_sessions: int = 64
    cookie_name: str = "jarvis_admin_session"
    secure_cookie: bool = True
    idle_timeout_seconds: int = 900
    reauth_window_seconds: int = 300

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
            type(self.idle_timeout_seconds) is not int
            or not 60 <= self.idle_timeout_seconds <= self.session_ttl_seconds
        ):
            raise BackendConfigurationError("idle timeout is invalid")
        if (
            type(self.reauth_window_seconds) is not int
            or not 30 <= self.reauth_window_seconds <= self.session_ttl_seconds
        ):
            raise BackendConfigurationError("re-authentication window is invalid")
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
        overrides: dict[str, int] = {}
        for env_name, field_name in (
            ("JARVIS_ADMIN_SESSION_TTL_SECONDS", "session_ttl_seconds"),
            ("JARVIS_ADMIN_SESSION_IDLE_SECONDS", "idle_timeout_seconds"),
            ("JARVIS_ADMIN_REAUTH_WINDOW_SECONDS", "reauth_window_seconds"),
        ):
            raw = source.get(env_name)
            if raw:
                try:
                    overrides[field_name] = int(raw)
                except (TypeError, ValueError) as exc:
                    raise BackendConfigurationError(
                        f"{env_name} must be an integer"
                    ) from exc
        return cls((credential,), session_secret, hosts, **overrides)


@dataclass(frozen=True, slots=True)
class AdminSessionRecord:
    subject: str
    session_id: str
    token_digest: bytes = field(repr=False)
    csrf_digest: bytes = field(repr=False)
    created_at: datetime
    expires_at: datetime
    authenticated_at: datetime
    assurance: SessionAssurance = SessionAssurance.MFA_SATISFIED

    @property
    def mfa_satisfied(self) -> bool:
        return self.assurance is SessionAssurance.MFA_SATISFIED


@dataclass(frozen=True, slots=True)
class IssuedAdminSession:
    subject: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: str
    assurance: SessionAssurance = SessionAssurance.MFA_SATISFIED
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class AdminSessionSummary:
    """Non-secret description of one active session for management screens."""

    session_id: str
    created_at: str
    expires_at: str
    last_seen_at: str
    assurance: SessionAssurance
    current: bool


@dataclass(frozen=True, slots=True)
class TrustedProxyConfig:
    """Networks whose ``X-Forwarded-For`` header may be trusted, if any.

    With no configured proxy the forwarded header is ignored entirely and the
    direct socket peer is authoritative, so a client cannot spoof its identity
    for rate limiting by sending its own ``X-Forwarded-For``.
    """

    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_spec(cls, spec: object) -> TrustedProxyConfig:
        if not spec:
            return cls(())
        if not isinstance(spec, str) or len(spec) > 2048:
            raise BackendConfigurationError("trusted proxy configuration is invalid")
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in spec.split(","):
            token = item.strip()
            if not token:
                continue
            try:
                networks.append(ipaddress.ip_network(token, strict=False))
            except ValueError as exc:
                raise BackendConfigurationError(
                    "trusted proxy configuration is invalid"
                ) from exc
            if len(networks) > 32:
                raise BackendConfigurationError(
                    "too many trusted proxy networks are configured"
                )
        return cls(tuple(networks))

    def _is_trusted(self, value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def is_trusted_peer(self, value: object) -> bool:
        """Public: is a direct socket peer a configured trusted proxy?

        Returns ``False`` when no proxy networks are configured so forwarded
        headers are never honored by default.
        """

        if not self.networks or not isinstance(value, str) or not value:
            return False
        return self._is_trusted(value)

    def client_ip(self, peer: object, forwarded_for: object) -> str:
        """Resolve the caller IP, honoring forwarded headers only from proxies."""

        peer_text = peer if isinstance(peer, str) and peer else "unknown"
        if not self.networks or not self._is_trusted(peer_text):
            return peer_text
        if not isinstance(forwarded_for, str) or len(forwarded_for) > 1024:
            return peer_text
        hops = [item.strip() for item in forwarded_for.split(",") if item.strip()]
        for candidate in reversed(hops):
            if not self._is_trusted(candidate):
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    return peer_text
                return candidate
        return peer_text


@dataclass(frozen=True, slots=True)
class AdminIpAllowlist:
    """Optional network boundary for every admin API request.

    An empty allowlist leaves network filtering disabled so deployments that
    enforce a VPN at the edge remain supported.  Once any network is supplied,
    unknown, malformed, and out-of-range client addresses fail closed.
    """

    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_spec(cls, spec: object) -> AdminIpAllowlist:
        if spec is None or spec == "":
            return cls(())
        if not isinstance(spec, str) or len(spec) > 2048:
            raise BackendConfigurationError("admin IP allowlist is invalid")
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in spec.split(","):
            token = item.strip()
            if not token:
                continue
            try:
                networks.append(ipaddress.ip_network(token, strict=False))
            except ValueError as exc:
                raise BackendConfigurationError(
                    "admin IP allowlist is invalid"
                ) from exc
            if len(networks) > 64:
                raise BackendConfigurationError(
                    "too many admin IP networks are configured"
                )
        if not networks:
            raise BackendConfigurationError("admin IP allowlist is invalid")
        return cls(tuple(networks))

    @property
    def enabled(self) -> bool:
        return bool(self.networks)

    def allows(self, value: object) -> bool:
        if not self.networks:
            return True
        if not isinstance(value, str):
            return False
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in self.networks)


class AdminSessionManager:
    def __init__(
        self,
        settings: AdminAuthSettings,
        *,
        credential_store: AdminCredentialStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(settings, AdminAuthSettings):
            raise BackendConfigurationError("admin auth settings are invalid")
        self._settings = settings
        self._clock = clock
        self._credential_store = credential_store
        credentials = (
            settings.credentials
            if credential_store is None
            else credential_store.load_credentials()
        )
        configured_subjects = {item.subject for item in settings.credentials}
        stored_subjects = {item.subject for item in credentials}
        if not credentials or stored_subjects != configured_subjects:
            raise BackendConfigurationError(
                "stored admin credential subjects do not match configuration"
            )
        self._credentials = {
            credential.subject: credential for credential in credentials
        }
        self._sessions: OrderedDict[bytes, AdminSessionRecord] = OrderedDict()
        self._last_seen: dict[bytes, datetime] = {}
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

    def verify_password(self, subject: object, password: object) -> str | None:
        """Constant-work password check; a dummy hash runs for unknown subjects."""

        credential = (
            self._credentials.get(subject) if isinstance(subject, str) else None
        )
        if credential is None:
            next(iter(self._credentials.values())).verify(password)
            return None
        if not credential.verify(password):
            return None
        return credential.subject

    @property
    def password_change_available(self) -> bool:
        return self._credential_store is not None

    def change_password(
        self,
        subject: object,
        current_password: object,
        new_password: object,
    ) -> PasswordChangeResult:
        """Persist a new salted password hash without retaining plaintext.

        The persistent store is updated before the in-process credential is
        swapped.  A process interruption therefore converges on the new hash
        at restart instead of reporting a successful but non-durable change.
        Live sessions are revoked by the HTTP boundary only after this method
        returns ``CHANGED``.
        """

        if self._credential_store is None:
            return PasswordChangeResult.NOT_AVAILABLE
        if not isinstance(subject, str):
            return PasswordChangeResult.INVALID_CURRENT_PASSWORD
        if not isinstance(new_password, str) or not 12 <= len(new_password) <= 1024:
            return PasswordChangeResult.INVALID_CURRENT_PASSWORD
        with self._lock:
            current = self._credentials.get(subject)
            if current is None or not current.verify(current_password):
                return PasswordChangeResult.INVALID_CURRENT_PASSWORD
            if current.verify(new_password):
                return PasswordChangeResult.INVALID_CURRENT_PASSWORD
            replacement = AdminPasswordCredential.derive_for_configuration(
                subject=subject,
                password=new_password,
                iterations=current.iterations,
            )
            self._credential_store.replace_credential(replacement)
            self._credentials[subject] = replacement
        return PasswordChangeResult.CHANGED

    def issue_session(
        self,
        subject: str,
        *,
        assurance: SessionAssurance = SessionAssurance.MFA_SATISFIED,
        now: datetime | None = None,
    ) -> IssuedAdminSession:
        """Mint a fresh session with rotated token and CSRF material."""

        if subject not in self._credentials:
            raise BackendConfigurationError("unknown admin subject")
        moment = _aware_utc(self._clock) if now is None else now
        session_token = _base64url(secrets.token_bytes(32))
        csrf_token = _base64url(secrets.token_bytes(32))
        token_digest = self._digest(b"session\x00", session_token)
        record = AdminSessionRecord(
            subject,
            f"sess_{uuid.uuid4().hex}",
            token_digest,
            self._digest(b"csrf\x00", csrf_token),
            moment,
            moment + timedelta(seconds=self._settings.session_ttl_seconds),
            moment,
            assurance,
        )
        with self._lock:
            self._prune_locked(moment)
            if len(self._sessions) >= self._settings.max_sessions:
                raise AuthenticationCapacityError("admin session capacity reached")
            self._sessions[token_digest] = record
            self._last_seen[token_digest] = moment
        return IssuedAdminSession(
            record.subject,
            session_token,
            csrf_token,
            format_utc_timestamp(record.expires_at),
            assurance,
            record.session_id,
        )

    def authenticate_and_issue(
        self,
        subject: object,
        password: object,
        *,
        assurance: SessionAssurance = SessionAssurance.MFA_SATISFIED,
    ) -> IssuedAdminSession | None:
        verified = self.verify_password(subject, password)
        if verified is None:
            return None
        return self.issue_session(verified, assurance=assurance)

    def rotate(
        self,
        session_token: object,
        *,
        assurance: SessionAssurance | None = None,
    ) -> IssuedAdminSession | None:
        """Revoke the presented session and issue a fresh one for its subject.

        Session rotation defeats fixation: the pre-authentication or pre-step-up
        token can never be reused.  Passing ``assurance`` raises the new session
        to that level (used when a second factor is completed).
        """

        if not isinstance(session_token, str) or not 20 <= len(session_token) <= 128:
            return None
        now = _aware_utc(self._clock)
        digest = self._digest(b"session\x00", session_token)
        with self._lock:
            self._prune_locked(now)
            record = self._sessions.get(digest)
            if record is None or not secrets.compare_digest(
                digest, record.token_digest
            ):
                return None
            self._sessions.pop(digest, None)
            self._last_seen.pop(digest, None)
            target = record.assurance if assurance is None else assurance
            return self.issue_session(record.subject, assurance=target, now=now)

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
            self._last_seen[digest] = now
            return record

    def requires_reauth(self, record: object, *, now: datetime | None = None) -> bool:
        """True when a sensitive action needs fresh authentication or step-up."""

        if not isinstance(record, AdminSessionRecord):
            return True
        if record.assurance is not SessionAssurance.MFA_SATISFIED:
            return True
        moment = _aware_utc(self._clock) if now is None else now
        window = timedelta(seconds=self._settings.reauth_window_seconds)
        return moment - record.authenticated_at > window

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
            self._last_seen.pop(digest, None)

    def revoke_all_for_subject(self, subject: object) -> int:
        """Revoke every session for a subject (password change, revoke-all)."""

        if not isinstance(subject, str):
            return 0
        with self._lock:
            targets = [
                digest
                for digest, record in self._sessions.items()
                if record.subject == subject
            ]
            for digest in targets:
                self._sessions.pop(digest, None)
                self._last_seen.pop(digest, None)
        return len(targets)

    def revoke_session_id(self, subject: object, session_id: object) -> bool:
        """Revoke one named session belonging to ``subject`` (management UI)."""

        if not isinstance(subject, str) or not isinstance(session_id, str):
            return False
        with self._lock:
            for digest, record in list(self._sessions.items()):
                if (
                    record.subject == subject
                    and secrets.compare_digest(record.session_id, session_id)
                ):
                    self._sessions.pop(digest, None)
                    self._last_seen.pop(digest, None)
                    return True
        return False

    def list_sessions_for_subject(
        self,
        subject: object,
        *,
        current_token: object = None,
    ) -> tuple[AdminSessionSummary, ...]:
        if not isinstance(subject, str):
            return ()
        current_digest = (
            self._digest(b"session\x00", current_token)
            if isinstance(current_token, str) and 20 <= len(current_token) <= 128
            else b""
        )
        now = _aware_utc(self._clock)
        with self._lock:
            self._prune_locked(now)
            summaries = [
                AdminSessionSummary(
                    record.session_id,
                    format_utc_timestamp(record.created_at),
                    format_utc_timestamp(record.expires_at),
                    format_utc_timestamp(self._last_seen.get(digest, record.created_at)),
                    record.assurance,
                    bool(current_digest)
                    and secrets.compare_digest(digest, current_digest),
                )
                for digest, record in self._sessions.items()
                if record.subject == subject
            ]
        summaries.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(summaries)

    def _prune_locked(self, now: datetime) -> None:
        idle = timedelta(seconds=self._settings.idle_timeout_seconds)
        expired = [
            digest
            for digest, record in self._sessions.items()
            if now >= record.expires_at
            or now - self._last_seen.get(digest, record.created_at) >= idle
        ]
        for digest in expired:
            self._sessions.pop(digest, None)
            self._last_seen.pop(digest, None)


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
class ReservedDeviceGrant:
    """Opaque request-scoped hold on one single-use device grant."""

    reservation_token: str = field(repr=False)
    verified: VerifiedDeviceChallenge = field(repr=False)

    def __repr__(self) -> str:
        return "ReservedDeviceGrant(authorization=<private>)"


@dataclass(frozen=True, slots=True)
class _DeviceGrantRecord:
    token_digest: bytes = field(repr=False)
    verified: VerifiedDeviceChallenge = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ReservedDeviceGrantRecord:
    reservation_digest: bytes = field(repr=False)
    grant_digest: bytes = field(repr=False)
    grant: _DeviceGrantRecord = field(repr=False)


class DeviceActionGrantManager:
    """Bounded single-use grants with an atomic request reservation phase.

    A reservation removes a grant from the replayable pool while an upload is
    parsed and validated.  The request owner must then either ``commit`` it
    after an accepted operation or ``release`` it on invalid/retriable input.
    Reserved records remain bounded by ``max_grants`` and are deliberately not
    time-pruned behind an active request; the ASGI middleware finalizes every
    reservation in a ``finally`` block.
    """

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
        self._reservations: OrderedDict[
            bytes, _ReservedDeviceGrantRecord
        ] = OrderedDict()
        self._lock = threading.RLock()

    def _digest(self, token: str) -> bytes:
        return hmac.new(
            self._secret,
            b"device-grant\x00" + token.encode("ascii", errors="ignore"),
            hashlib.sha256,
        ).digest()

    def _reservation_digest(self, token: str) -> bytes:
        return hmac.new(
            self._secret,
            b"device-grant-reservation\x00"
            + token.encode("ascii", errors="ignore"),
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
            if len(self._grants) + len(self._reservations) >= self._max_grants:
                raise AuthenticationCapacityError("device grant capacity reached")
            self._grants[digest] = record
        return IssuedDeviceGrant(token, format_utc_timestamp(record.expires_at))

    def reserve(
        self,
        token: object,
        *,
        license_id: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> ReservedDeviceGrant | None:
        """Atomically hold a matching grant without consuming invalid context."""

        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        digest = self._digest(token)
        now = _aware_utc(self._clock)
        with self._lock:
            self._prune_locked(now)
            record = self._grants.get(digest)
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

            reservation_token = _base64url(secrets.token_bytes(32))
            reservation_digest = self._reservation_digest(reservation_token)
            if reservation_digest in self._reservations:
                raise AuthenticationCapacityError(
                    "device grant reservation could not be created"
                )
            self._grants.pop(digest)
            self._reservations[reservation_digest] = _ReservedDeviceGrantRecord(
                reservation_digest,
                digest,
                record,
            )
        return ReservedDeviceGrant(reservation_token, verified)

    def commit(self, reservation: object) -> bool:
        """Permanently consume one request reservation."""

        if type(reservation) is not ReservedDeviceGrant:
            return False
        digest = self._reservation_digest(reservation.reservation_token)
        with self._lock:
            record = self._reservations.get(digest)
            if (
                record is None
                or not secrets.compare_digest(digest, record.reservation_digest)
                or reservation.verified != record.grant.verified
            ):
                return False
            self._reservations.pop(digest)
        return True

    def release(self, reservation: object) -> bool:
        """Return a request reservation only while the original grant is valid."""

        if type(reservation) is not ReservedDeviceGrant:
            return False
        digest = self._reservation_digest(reservation.reservation_token)
        with self._lock:
            record = self._reservations.get(digest)
            if (
                record is None
                or not secrets.compare_digest(digest, record.reservation_digest)
                or reservation.verified != record.grant.verified
            ):
                return False
            now = _aware_utc(self._clock)
            self._reservations.pop(digest)
            if now < record.grant.expires_at:
                self._grants[record.grant_digest] = record.grant
        return True

    def consume(
        self,
        token: object,
        *,
        license_id: str,
        action: DeviceChallengeAction,
        resource_id: str,
    ) -> VerifiedDeviceChallenge | None:
        reservation = self.reserve(
            token,
            license_id=license_id,
            action=action,
            resource_id=resource_id,
        )
        if reservation is None or not self.commit(reservation):
            return None
        return reservation.verified

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
    "AdminCredentialStore",
    "AdminIpAllowlist",
    "AdminPasswordCredential",
    "AdminSessionManager",
    "AdminSessionRecord",
    "AdminSessionSummary",
    "AuthenticationCapacityError",
    "BackendConfigurationError",
    "BoundedAttemptLimiter",
    "DeviceActionGrantManager",
    "IssuedAdminSession",
    "IssuedDeviceGrant",
    "PasswordChangeResult",
    "ReservedDeviceGrant",
    "SessionAssurance",
    "TrustedProxyConfig",
]

# JARVIS Admin Authentication and MFA

## Evidence status

The admin authentication, RFC 6238 TOTP MFA, recovery codes, durable password
rotation, and hardened session/CSRF boundary are **implemented**, mechanically
**enforced** (fail-closed), and **tested locally** with positive and negative
suites. A local Chromium smoke exercised the MFA session boundary on
2026-07-17. **No component is production-verified**: real operator enrollment on
a deployed domain/TLS endpoint is an **external blocker**, and transactional MFA
master-key rotation is an **internal gap** (currently `not_available`). The
status terms are defined in [`../SECURITY.md`](../SECURITY.md).

Cross-references: [`../THREAT_MODEL.md`](../THREAT_MODEL.md) (admin threats),
[`PRODUCT_BACKEND_OPERATIONS.md`](PRODUCT_BACKEND_OPERATIONS.md) (environment
configuration), [`MOBILE_ADMIN.md`](MOBILE_ADMIN.md) (Admin PWA boundary), and
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) (admin security gate + matrix).

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| TOTP primitives | `product_backend/api_totp.py` | RFC 6238 HMAC-SHA1 codes, provisioning URI, recovery-code generation |
| MFA store/cipher | `product_backend/admin_mfa.py` | AES-256-GCM sealed TOTP secrets, hashed single-use recovery codes, replay/audit state |
| Password store | `product_backend/admin_credentials.py` | Durable salted PBKDF2 password hashes in `admin-credentials.sqlite3` |
| Sessions/CSRF | `product_backend/api_auth.py` | Bounded sessions, rotation, CSRF, rate limits, proxy/CIDR resolution |
| MFA HTTP surface | `product_backend/admin_mfa_api.py` | Enrollment QR, activation, step-up, password change, session list/revoke, MFA reset |
| Admin web shell | `product_backend/admin_web/` | Static same-origin `/admin/` login/console; no auth material in the browser |

## Authentication model

- **Two factors are required in production.** `AdminMfaSettings` rejects a
  configuration where MFA is mandatory and a password-only bypass is also
  enabled. `JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY` is a development-only opt-in
  and must never be set in a customer-facing deployment. The production factory
  will not start without `JARVIS_ADMIN_MFA_KEY_FILE`.
- **TOTP is RFC 6238.** `api_totp.py` uses HMAC-SHA1 dynamic truncation, a 30
  second period, and 6 digits, from the Python standard library only. A bounded
  drift window (`TOTP_DRIFT_STEPS`) tolerates clock skew; the last accepted step
  is persisted so a used code cannot be replayed inside its own window.
- **TOTP secrets are sealed at rest.** `MfaSecretCipher` derives an encryption
  subkey and a recovery HMAC pepper from an operator master key
  (`JARVIS_ADMIN_MFA_KEY_FILE`) and seals each secret with AES-256-GCM. The raw
  shared secret and provisioning URI are returned exactly once at enrollment and
  are never logged, never stored in plaintext, and never placed in a `__repr__`.
- **Recovery codes are single-use.** They are generated once, shown once, and
  stored only as keyed HMAC-SHA256 digests in `admin_recovery_codes`. Each code
  is consumed atomically; an exhausted or reused code is rejected. The remaining
  count is reported without exposing any code.
- **Passwords are durable and salted.** `admin_credentials.py` persists only
  PBKDF2 salt/digest/iteration records. The environment password hash is a
  one-time bootstrap; an authenticated change (requiring the current password
  and recent MFA) writes a fresh hash that survives restart. Changing only the
  bootstrap environment hash does not silently replace a rotated hash.

## Session and CSRF boundary

- Session cookies are `Secure` + `HttpOnly` + `SameSite=Strict`
  (`secure_cookie` defaults to `True`).
- Sessions have both a bounded absolute TTL and an idle timeout (default 900 s),
  configured through `JARVIS_ADMIN_SESSION_TTL_SECONDS` and
  `JARVIS_ADMIN_SESSION_IDLE_SECONDS`.
- Each session carries a rotated CSRF token; mutations verify it with a
  constant-time comparison of stored digests. A read-only restored session must
  re-authenticate before it can mint a new mutation token.
- Sensitive actions (payment, release, license and device operations) require a
  recent step-up within `JARVIS_ADMIN_REAUTH_WINDOW_SECONDS`.
- `revoke`, `revoke_all_for_subject`, and `revoke_session_id` back logout,
  remote revoke, and the full revoke triggered by a password change.
- Password and MFA attempts are bounded per client IP **and** account-globally.
  `JARVIS_TRUSTED_PROXIES` gates whether `X-Forwarded-For` is trusted, and
  `JARVIS_ADMIN_ALLOWED_NETWORKS` optionally restricts admin access to operator
  or VPN CIDRs; an unknown, malformed, or out-of-range client fails closed.

## Audit

MFA lifecycle events (`MfaAuditEvent`: enrollment, activation, TOTP/recovery
use, recovery regeneration, session revoke, resets) are persisted. Payment
approval/rejection decisions are append-only and exposed through the Admin PWA.
Device replacement history is persisted separately. A single unified,
product-wide audit query surface across payment, MFA, and device events is an
**internal gap**, not a production-verified capability.

## Local evidence

| Suite | Focus |
| --- | --- |
| `tests/test_product_backend_totp.py` | RFC 6238 vectors, drift window, normalization, recovery-code generation |
| `tests/test_product_backend_mfa_sessions.py` | Enrollment, replay defence, recovery single-use, rotation, revoke, CSRF, expiry |
| `tests/test_product_backend_auth.py` | Bounded sessions, rate limits, trusted-proxy/CIDR resolution, fail-closed config |
| `tests/test_admin_web_static.py` | Static same-origin shell, no auth material cached in the browser |

Stage 9 E2E scenario 8 ("Admin logs in with MFA") and scenario 9 ("Admin
reviews private evidence") map to this evidence; see
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md).

## Residual risk

- Phishing, a stolen unlocked authenticator, and operator-endpoint compromise
  are outside application control (see [`../THREAT_MODEL.md`](../THREAT_MODEL.md)).
- Transactional MFA master-key rotation is not implemented and is honestly
  `not_available`; a compromised master key currently requires re-enrollment.
- In-memory session/grant/rate-limit state is single-process; a multi-instance
  deployment needs a shared bounded store before more than one backend process
  runs.
- No production browser/proxy enrollment drill has been performed; real-domain
  TLS admin evidence remains an external blocker.

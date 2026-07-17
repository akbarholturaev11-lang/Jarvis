# JARVIS Product System Architecture

## Implemented boundary

The repository contains a tested, platform-neutral product foundation for one
paid plan. It does not introduce subscriptions or lifetime updates. Entitlement
authority is the exact semantic version; a higher semantic version requires a
separate payment and approval, while a higher build of the same semantic version
uses the existing entitlement.

Unless a row explicitly says otherwise, the status below means **implemented**,
**enforced**, and **tested locally**. No component is
**production-verified**. `not_available` identifies an honest runtime result,
`internal gap` identifies repository work still missing, `external blocker`
identifies operator/platform evidence still required, and `legal blocker`
identifies the unresolved distribution-rights gates. See
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md),
[`../SECURITY.md`](../SECURITY.md), and
[`../THREAT_MODEL.md`](../THREAT_MODEL.md) for the evidence and threat boundary.

```text
Desktop app
  ├─ OS secure store: Gemini key, device private key, active license ID
  ├─ signed offline entitlement cache (exact version + device fingerprint)
  ├─ HTTPS product client + one-time Ed25519 device proof
  └─ private update staging + verified install/rollback transaction
             │
             ▼
FastAPI product backend
  ├─ TOTP MFA + durable password hashes + hardened session/CSRF boundary
  ├─ account/license/device provisioning + explicit replacement history
  ├─ release price, EN/RU features/fixes and artifact publishing
  ├─ private payment evidence + manual approval audit
  ├─ one-time activation credentials + entitlement signer
  ├─ signed update metadata + header-carried single-use download grants
  └─ HTTPS/host/proxy policy + health/readiness/metrics + redacted JSON logs
```

Mobile Admin Phase 1 is the separate installable `/admin/` PWA: MFA/session
cookies stay server-owned, the service worker caches only the public shell, and
all admin records/evidence remain live same-origin HTTPS reads. Native mobile
wrappers and push providers are `not_available`.

## Desktop layers

- `core/credential_service.py` stores Gemini credentials in the platform secure
  store. The historical `config/api_keys.json` path is a bounded, one-time
  migration source only: a verified migration removes the legacy key and the
  secure store becomes authoritative. It is not a continuing plaintext fallback,
  and no new onboarding write goes there.
- `core/device_identity.py` owns a generated Ed25519 per-install key. Only the
  private key is stored securely; the public fingerprint is the server binding.
- `core/entitlement_certificate.py` verifies canonical signed exact-version
  certificates. There is no routine expiry or remote revocation check.
- `core/entitlement_cache.py` verifies before atomic storage and again on every
  offline load.
- `core/product_api_client.py` enforces HTTPS, bounded payloads/downloads and
  same-origin/no-sensitive-header redirect rules.
- `core/product_activation.py`, `core/product_purchase.py` and
  `core/product_updates.py` implement activation, manual screenshot payment,
  status polling, update authority and byte-for-byte staging.
- `core/update_transaction.py` pins the verified staged descriptor through the
  install call, requires a private re-hashed copy, and coordinates persisted
  backup, atomic mutation, bounded health proof and a durable rollback journal.
  `core/macos_update.py` implements strict `.app` ZIP extraction, app/tree
  identity, fresh-nonce health proof and a real temporary-filesystem macOS
  development adapter. The production adapter assesses a fixed signed/notarized
  helper but remains fail-closed until its privileged shutdown/swap protocol is
  implemented and audited.
- `core/product_runtime.py` is the desktop facade used by `main.py` and `ui.py`.
  Source development is not gated. Frozen builds require a locally verified
  signed entitlement for their exact version before Gemini runtime startup;
  installation separately rechecks target-version authority. Startup recovery
  runs before licensing/onboarding and blocks on an unresolved journal.

## Backend layers and schema

- `product_backend/sqlite_repository.py` is the command repository. Tables cover
  accounts, one-plan licenses, one active device binding with replacement
  history, releases, target-specific artifacts, compatible update sources,
  payment submissions, exact-version entitlements and append-only admin
  decisions.
- `product_backend/api_activation.py` uses dedicated SQLite tables for hashed,
  expiring, one-time activation credentials and nonce-digest-only challenges.
- `product_backend/device_challenges.py` stores nonce digests and consumes every
  proof attempt atomically.
- `product_backend/api_auth.py` keeps bounded admin sessions and device action
  grants in process memory. Restart invalidates them safely; password/TOTP
  attempt budgets are account-global plus client bounded, and trusted-proxy
  resolution feeds an optional admin CIDR allowlist.
- `product_backend/admin_mfa.py` encrypts TOTP secrets, hashes one-time recovery
  codes and persists replay/audit state. `admin_credentials.py` persists only
  salted PBKDF2 password hashes so authenticated rotation survives restart.
- `product_backend/admin_mfa_api.py` exposes enrollment QR, activation,
  TOTP/recovery step-up, password change, session list/revoke and MFA reset.
- `product_backend/private_storage.py` stores sanitized, pixel-bounded payment
  images privately without duplicate full-frame copies.
- `product_backend/payment_instructions.py` loads optional owner-only bilingual
  payment details; invalid/missing input is claims-free `not_configured`.
- `product_backend/api_artifact_storage.py` is a read-only, no-follow release
  object adapter with exact size and SHA-256 checks.
- `product_backend/api_app.py` exposes the customer/admin API and mounts the
  bilingual `/admin/` panel.
- `product_backend/admin_web/static/` provides the installable responsive Admin
  PWA, visible-only in-app payment alerts and background privacy cleanup;
  `api_queries.py` supplies bounded persistent admin directories.
- `product_backend/runtime.py` assembles a single-process SQLite deployment from
  explicit environment configuration and owner-only secret files.
- `product_backend/api_operational.py`, `observability.py`, `ops/`, and `deploy/`
  provide the fail-closed HTTPS/host/proxy policy, probes, bearer-gated metrics,
  redacted structured logs, POSIX backup/restore/migration tools, and reference
  deployment recipes. These are locally tested contracts, not a deployed
  production service.

## Fresh-purchase and update lifecycle

1. A fresh desktop generates an Ed25519 device key and proves possession against
   a published, signed initial-installer target. The server returns its own price,
   currency, EN/RU release notes, and owner-configured payment instructions.
2. A valid bounded PNG/JPEG/WebP submission atomically creates or reuses the
   pseudonymous purchase account, one-plan license and initial device binding,
   then records a pending payment. The opaque purchase identity is stored only as
   a digest. This step creates **no entitlement**.
3. An MFA-authenticated admin privately reviews the evidence and approves or
   rejects it. Only approval creates the exact-version entitlement and append-only
   payment-decision audit record; reject/resubmit and approval retries are bounded
   and idempotent.
4. The activation path issues/consumes a one-time credential, proves the bound
   device key, returns a signed exact-version certificate and caches it for
   indefinite offline use. Raw activation material is returned only once and is
   not stored by the backend.
5. A later paid semantic version uses the existing license/device proof path and
   repeats payment and approval. Declining it does not affect the installed older
   version. Device replacement is an explicit admin action that removes only the
   old device's future server authority, not its already installed offline copy.

## Honest current limitations

- The backend runtime is a single-process SQLite MVP, not a multi-region service.
- Production deployment is **not production-verified**: no operator-owned domain,
  trusted public TLS endpoint, deployed reverse proxy, monitored server, restore
  drill, or real key-rotation cutover has been exercised here. The deployment
  recipes and local TLS environment are **tested locally** only.
- Real temporary-filesystem macOS `.app` replacement and rollback are tested
  through an explicit development-only adapter. Production replacement remains
  `not_available` until a signed/notarized helper, safe shutdown protocol and
  clean-Mac verification exist.
- macOS, Windows Credential Manager and Linux Secret Service secure-store adapters
  are implemented and covered by contract/negative tests; a real host smoke for
  each native backend remains an **external blocker**. A missing native service
  returns `not_available`. Windows/Linux packaging and updater helpers remain
  `not_available`.
- Mobile Admin notifications are implemented as visible, online PWA polling and
  tested at the contract level, but delivery of a newly created payment into a
  real browser banner was not exercised by the Stage 9 harness. Background push
  and native iOS/Android clients are `not_available`.
- The Admin PWA exposes the append-only payment approval/rejection audit. MFA
  events and device replacement history persist in separate stores/projections;
  a single product-wide audit query and UI are an **internal gap**.
- A real deployment origin, release public key, entitlement private key,
  activation pepper, payment destination/instructions and admin credentials must
  be supplied externally.
- Commercial distribution remains blocked by the rights, PyQt6, branding and
  signing gates in `PRODUCT_RELEASE_CONTRACT.md`.

The canonical per-function readiness matrix is in
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). Packaging, clean-Mac, updater,
admin-MFA and mobile detail is maintained in [`PACKAGING.md`](PACKAGING.md),
[`RELEASE_PACKAGING.md`](RELEASE_PACKAGING.md),
[`CLEAN_MAC_TEST.md`](CLEAN_MAC_TEST.md),
[`UPDATE_ROLLBACK.md`](UPDATE_ROLLBACK.md), [`ADMIN_MFA.md`](ADMIN_MFA.md), and
[`MOBILE_ADMIN.md`](MOBILE_ADMIN.md).

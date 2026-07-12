# JARVIS Product System Architecture

## Implemented boundary

The repository contains a tested, platform-neutral product foundation for one
paid plan. It does not introduce subscriptions or lifetime updates. Entitlement
authority is the exact semantic version; a higher semantic version requires a
separate payment and approval, while a higher build of the same semantic version
uses the existing entitlement.

```text
Desktop app
  ├─ OS secure store: Gemini key, device private key, active license ID
  ├─ signed offline entitlement cache (exact version + device fingerprint)
  ├─ HTTPS product client + one-time Ed25519 device proof
  └─ private update staging + durable rollback journal contract
             │
             ▼
FastAPI product backend
  ├─ admin session + CSRF + bounded login/challenge attempts
  ├─ account/license/device provisioning + explicit replacement history
  ├─ release price, EN/RU features/fixes and artifact publishing
  ├─ private payment evidence + manual approval audit
  ├─ one-time activation credentials + entitlement signer
  └─ signed update metadata + single-use artifact download grants
```

## Desktop layers

- `core/credential_service.py` stores Gemini credentials in the platform secure
  store. The historical `config/api_keys.json` path is read-only compatibility;
  no new onboarding write goes there.
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
  install call and requires the future helper to make and re-hash an inaccessible
  private copy before mutation. It also defines persisted backup, atomic mutation,
  bounded health proof and durable rollback-journal semantics. Real OS mutation
  adapters remain unavailable until a signed helper exists.
- `core/product_runtime.py` is the desktop facade used by `main.py` and `ui.py`.
  Source development is not gated. Frozen builds require a locally verified
  signed entitlement for their exact version before Gemini runtime startup.

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
  grants in process memory. Restart invalidates them safely.
- `product_backend/private_storage.py` stores sanitized, pixel-bounded payment
  images privately without duplicate full-frame copies.
- `product_backend/payment_instructions.py` loads optional owner-only bilingual
  payment details; invalid/missing input is claims-free `not_configured`.
- `product_backend/api_artifact_storage.py` is a read-only, no-follow release
  object adapter with exact size and SHA-256 checks.
- `product_backend/api_app.py` exposes the customer/admin API and mounts the
  bilingual `/admin/` panel.
- `product_backend/runtime.py` assembles a single-process SQLite deployment from
  explicit environment configuration and owner-only secret files.

## Manual sales lifecycle

1. Admin creates an account and one-plan license.
2. The desktop generates a device public fingerprint; admin binds that initial
   target to the license. Moving to another computer requires an explicit admin
   replacement that deactivates the old binding and retains its history.
3. Customer submits bounded PNG/JPEG/WebP payment evidence using a one-time
   device proof grant.
4. Admin reviews and approves or rejects. Only approval creates the exact-version
   entitlement and append-only decision record.
5. Admin issues a one-time activation key. The raw key is returned once; only a
   keyed digest remains in the activation database.
6. The bound desktop proves possession of its device key, receives a signed
   certificate and caches it for indefinite offline use of that exact version.
7. A later semantic version repeats payment and approval. Declining it does not
   affect the installed older version.

## Honest current limitations

- The backend runtime is a single-process SQLite MVP, not a multi-region service.
- Real macOS application replacement is disabled. Verified downloads and durable
  rollback contracts exist, but no adapter can claim install success.
- Windows/Linux packaging, secure credential backends and installer helpers are
  `not_available` where not verified.
- A real deployment origin, release public key, entitlement private key,
  activation pepper, payment destination/instructions and admin credentials must
  be supplied externally.
- Commercial distribution remains blocked by the rights, PyQt6, branding and
  signing gates in `PRODUCT_RELEASE_CONTRACT.md`.

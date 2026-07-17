# JARVIS Product Backend Operations

> For the full production runbook (topology, health/readiness/metrics, HTTPS
> policy, backup/restore, migration, retention, key rotation, single-process
> constraint, multi-instance plan, local TLS dev env, and the `ops/` +
> `deploy/` tooling), see [`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md).

## Evidence status

The fail-closed runtime, operational middleware, schema checks, POSIX tooling and
deployment recipes are **implemented** and **tested locally**. HTTPS/host/proxy,
MFA, private-evidence and secret-file boundaries are mechanically **enforced**.
No component is **production-verified**: a real domain/TLS endpoint, production
server, edge controls, monitoring, restore drill and key cutover are
**external blockers**. PostgreSQL/shared state, automatic retention, unified
product audit, and authenticated backup manifests remain **internal gaps**.
Native Windows mutating ops return `not_available`.

See [`../SECURITY.md`](../SECURITY.md), [`../THREAT_MODEL.md`](../THREAT_MODEL.md),
and [`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md) for the security and
local-evidence boundary.

## Runtime model

Use the ASGI factory; importing the module does not create files or load keys:

```bash
uvicorn product_backend.runtime:create_app_from_environment \
  --factory --host 127.0.0.1 --port 8080
```

Put it behind an HTTPS reverse proxy with the configured host preserved. Do not
expose the plain HTTP listener publicly. The admin cookie is secure-only and the
app rejects hosts outside `JARVIS_API_ALLOWED_HOSTS`.

By default the admin-login CPU budget and network policy use the direct ASGI
peer and ignore `X-Forwarded-For`. Set `JARVIS_TRUSTED_PROXIES` only to the CIDR
networks of proxies that actually sanitize that header; requests arriving from
any other peer cannot spoof it. Password and MFA budgets include an
account-global bound in addition to the client-IP bound. Apply an edge rate
limit too; the in-process bounds are not a substitute for DDoS protection.

Set `JARVIS_ADMIN_ALLOWED_NETWORKS` to comma-separated client CIDRs when the
admin console must be reachable only through a VPN or operator network. Once
configured, every admin API call fails closed for an unknown, malformed or
out-of-range resolved client address. Leaving it unset means the network edge
is responsible for access restriction.

## Required configuration

The factory fails closed unless all runtime and admin authentication values are
present:

- `JARVIS_BACKEND_DATA_DIR`: absolute owner-only (`0700`) data directory.
- `JARVIS_RELEASE_ARTIFACT_ROOT`: existing owner-only artifact directory.
- `JARVIS_RELEASE_PUBLIC_KEYS_JSON`: JSON map of key ID to raw 32-byte Ed25519
  public key encoded as unpadded base64url.
- `JARVIS_ENTITLEMENT_KEY_ID`: active entitlement signing key ID.
- `JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE`: absolute, regular, owner-only (`0600`),
  raw 32-byte Ed25519 private key file.
- `JARVIS_ACTIVATION_PEPPER_FILE`: absolute, regular, owner-only (`0600`), 32–128
  random bytes.
- `JARVIS_ADMIN_SUBJECT`, `JARVIS_ADMIN_PASSWORD_SALT_B64URL`,
  `JARVIS_ADMIN_PASSWORD_HASH_B64URL`, `JARVIS_ADMIN_PBKDF2_ITERATIONS`,
  `JARVIS_ADMIN_SESSION_SECRET_B64URL`, `JARVIS_API_ALLOWED_HOSTS`: validated by
  `AdminAuthSettings.from_env()`.
- `JARVIS_ADMIN_MFA_KEY_FILE`: absolute, regular, owner-only (`0600`), 32–128
  random bytes used to derive TOTP encryption and recovery-code HMAC keys. The
  production factory will not start without it.
- `JARVIS_REQUIRE_HTTPS`: must be present and explicitly true. False, malformed,
  or missing values fail before the backend data directory is created.

Optional security policy:

- `JARVIS_TRUSTED_PROXIES`: comma-separated proxy CIDRs; absent means forwarded
  client headers are ignored.
- `JARVIS_ADMIN_ALLOWED_NETWORKS`: comma-separated operator/VPN CIDRs enforced
  after trusted-proxy resolution.
- `JARVIS_ADMIN_SESSION_TTL_SECONDS`, `JARVIS_ADMIN_SESSION_IDLE_SECONDS` and
  `JARVIS_ADMIN_REAUTH_WINDOW_SECONDS`: bounded absolute, idle and sensitive
  action re-authentication windows.
- `JARVIS_ADMIN_MFA_ISSUER`: authenticator display issuer.
- `JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY`: development-only bypass. Never set it
  in a customer-facing deployment.

The environment password hash is a one-time bootstrap for
`admin-credentials.sqlite3`. An authenticated password change requires recent
MFA and the current password, persists a fresh salted PBKDF2 hash, audits the
event and revokes all sessions. A later process restart keeps the rotated hash;
changing only the bootstrap environment hash does not silently replace it.

Optional manual-payment configuration:

- `JARVIS_PAYMENT_INSTRUCTIONS_FILE`: absolute owner-owned, single-link regular
  JSON file with mode `0400` or `0600`, strict schema
  `jarvis.payment-instructions.v1`, a bounded recipient field and bilingual
  `method` / `instructions` objects (`en` + `ru`). Invalid or absent input becomes
  explicit `not_configured`; the desktop then disables screenshot submission.
  The backend returns configured details only after a valid one-time device proof.

Never put raw passwords, private keys, peppers, session secrets or payment
credentials in the repository, product config, logs, command arguments or
project memory. In production, materialize owner-only files from a managed secret
store immediately before process start.

## Client configuration

The non-secret client `product.json` contains only the HTTPS API origin and
pinned entitlement/release public keys. Start from
`config/product.example.json`, replace every placeholder, validate it in a
controlled build environment and pass it to the macOS build script via
`--product-config`. The real `config/product.json` is gitignored.

## Artifact publishing

The API accepts artifact metadata only after its canonical Ed25519 signature is
verified. A separate offline release pipeline must place the exact immutable
bytes under `JARVIS_RELEASE_ARTIFACT_ROOT` with owner-controlled directories and
non-writable-by-group/other files. The storage key, size and SHA-256 must match
the signed manifest and admin artifact record.

Do not reuse entitlement signing keys for release signing. Rotate public keys by
shipping old and new trusted IDs during the overlap window; never silently
replace an existing key ID with different key material.

## Backup, monitoring and retention

- Back up every SQLite database (commerce, device challenges, activation, MFA
  and admin credentials) and private payment evidence together during a real
  stopped-service maintenance window. `ops.backup` requires explicit
  `--confirm-service-stopped`; the flag is an operator assertion, not a process
  lock.
- `ops.backup`/`ops.restore` use POSIX owner/no-follow primitives and are
  **tested locally** on the supported host path. Native Windows execution is
  `not_available`. Restore publishes only into a fresh nonexistent target;
  overlay/force restore is `not_available`.
- The manifest's size and SHA-256 values provide **integrity detection, not
  authenticity**. Keep backups owner-only and immutable or authenticate them
  with a separately managed signature/MAC before transport. Never restore an
  untrusted archive merely because its hashes are internally consistent.
- Monitor authentication capacity errors, repeated 401/409/429 responses,
  artifact integrity failures, payment evidence storage errors and SQLite disk
  space/locking.
- Keep the reverse proxy body/concurrency limit at least as strict as the app:
  small JSON routes are capped before parsing/PBKDF2, while only the payment
  multipart route accepts the bounded evidence allowance.
- Define payment evidence retention and deletion policy before production. The
  current code supports compensating deletion on failed DB persistence, but does
  not automatically delete approved/rejected evidence.
- Restore drills must confirm account/license/release/payment/entitlement rows,
  activation one-time state and private evidence consistency.

## Deployment constraints

The runtime is intentionally one process with SQLite and in-memory sessions/
grants. Multiple workers would not share sessions, rate limits or download
grants. A production multi-instance deployment requires PostgreSQL migrations,
a shared bounded session/grant/rate-limit store, object storage with private
streaming reads, centralized audit/metrics and operational key rotation.

The currently exposed Admin audit view covers payment approvals/rejections.
MFA events and device replacement history are separately persisted; a unified,
queryable product-wide audit surface is an **internal gap**, not a
production-verified capability.

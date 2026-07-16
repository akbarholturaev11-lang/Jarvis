# JARVIS Product Backend — Production Deployment

This is the operational runbook for deploying the product backend to a real
HTTPS server. It complements [`PRODUCT_BACKEND_OPERATIONS.md`](PRODUCT_BACKEND_OPERATIONS.md)
(security config reference) and [`PRODUCT_RELEASE_CONTRACT.md`](PRODUCT_RELEASE_CONTRACT.md)
(business/entitlement invariants). It documents a target deployment; the
commercial-distribution gates in the contract still apply.

## 1. Topology

```
                    ┌─────────────────────────────┐
 Internet ─HTTPS─▶  │ reverse proxy (nginx/Caddy) │ ─HTTP─▶ uvicorn (--factory)
                    │  TLS termination + HSTS      │        product_backend.runtime
                    │  trusted host (server_name)  │        :create_app_from_environment
                    │  edge rate limit             │              │
                    │  admin IP allowlist (opt)    │              ▼
                    └─────────────────────────────┘        SQLite databases +
                          loopback / internal net           payment evidence
                                                            (owner-only 0700 data dir)
```

- One backend process. SQLite databases and payment-evidence objects live under
  an owner-only data directory. TLS is terminated at the proxy; the backend
  listens on `127.0.0.1:8080` (systemd) or an internal compose network (Docker)
  and is **never** published directly to the internet.
- Deployment recipes: [`deploy/`](../deploy/README.md) (systemd unit, Docker
  image + compose, nginx/Caddy configs, env reference).

## 2. Configuration

The ASGI factory fails closed: importing the module has no side effects, and
`create_app_from_environment` raises `BackendConfigurationError` unless all
required variables and owner-only secret files are present and valid. The full
list is in [`deploy/env/backend.env.example`](../deploy/env/backend.env.example)
and [`PRODUCT_BACKEND_OPERATIONS.md`](PRODUCT_BACKEND_OPERATIONS.md).

Generate material and validate before every deploy:

```bash
python -m ops.gen_secrets --out-dir /etc/jarvis/secrets \
    --admin-subject admin:ops --allowed-hosts api.example.com \
    --env-file /etc/jarvis/backend.env
python -m ops.validate_config --env-file /etc/jarvis/backend.env   # exit != 0 blocks deploy
```

`ops.validate_config` runs structural checks (required vars, wildcard host, HTTPS
policy, secret-file permissions) and then assembles the real app, exercising
every runtime guard, and closes it. The systemd unit runs it as `ExecStartPre`,
so a misconfiguration prevents the service from starting.

Secrets live **outside the repository** in a managed secret store, materialized
as owner-only files immediately before start. `.gitignore` already excludes
`*.key`, `*.pem`, `*.sqlite3`, `.env`/`.env.*`, and the payment-instructions and
product config files.

## 3. HTTPS-only policy and forwarded headers

- `JARVIS_REQUIRE_HTTPS=true` makes the app reject non-HTTPS requests (`400`), or
  308-redirect safe GET/HEAD requests when `JARVIS_HTTPS_REDIRECT=true`. When the
  request is HTTPS it emits `Strict-Transport-Security` (`JARVIS_HSTS_MAX_AGE`).
- The forwarded scheme (`X-Forwarded-Proto`) and forwarded client
  (`X-Forwarded-For`) are trusted **only** when the direct socket peer is in
  `JARVIS_TRUSTED_PROXIES`. With no configured proxy the forwarded headers are
  ignored and the socket peer is authoritative, so a client cannot spoof HTTPS or
  its rate-limit identity. Set `JARVIS_TRUSTED_PROXIES` to your proxy's CIDR (or
  use uvicorn `--proxy-headers --forwarded-allow-ips` for the loopback proxy).
- Health/readiness probes are exempt from the HTTPS policy and the trusted-host
  check, so a plain-HTTP liveness probe on the instance IP still works.

## 4. Operational endpoints

| Path | Auth | Purpose |
| --- | --- | --- |
| `GET /healthz` | none | Liveness. Always `200` if the process is up; no DB access, host-agnostic. |
| `GET /readyz` | none | Readiness. `200` when the databases answer a read; `503` otherwise. |
| `GET /metrics` | Bearer | Prometheus counters. Disabled (`404`) unless `JARVIS_METRICS_TOKEN` (≥16 chars) is set. Do not expose publicly; scrape internally. |

Every response carries an `X-Request-ID` correlation header (echoing a safe
inbound `X-Request-ID` or a fresh one). Structured access logs are emitted as one
JSON object per request on `jarvis.backend.access`, with secret values redacted
by `product_backend.observability`. Point the systemd/container log pipeline at
your log aggregator; the JSON includes `request_id`, `method`, `path`, `status`,
`client_ip`, `scheme`, and `duration_ms`.

## 5. Rate limiting (edge + app)

The app enforces bounded in-process limits on admin login, MFA, activation, and
purchase flows. These are **not** a DDoS defense. Add an **edge** limit at the
proxy (`limit_req` in nginx, `rate_limit` in Caddy) — the shipped configs cap
auth-sensitive paths tighter than general traffic — plus your provider's network
protection.

## 6. Backup and restore

```bash
# Consistent online snapshot (safe while the service runs).
python -m ops.backup  --data-dir /var/lib/jarvis/data \
    --backup-dir /var/backups/jarvis/$(date -u +%Y%m%dT%H%M%SZ)
# Verified restore into a fresh data dir (refuses to overwrite without --force).
python -m ops.restore --backup-dir /var/backups/jarvis/<stamp> \
    --data-dir /var/lib/jarvis/data
```

- `ops.backup` copies every SQLite database with the online backup API
  (transaction-consistent) and every payment-evidence object, hashing each into
  `manifest.json`.
- `ops.restore` re-verifies each file's SHA-256 against the manifest before
  copying; a corrupted or tampered backup fails closed.
- **Restore drills:** after restore, confirm account/license/release/payment/
  entitlement rows and activation one-time state (see below) and that private
  evidence bytes match.

## 7. Database migration

Only the commerce database carries an explicit schema version (`PRAGMA
user_version`, currently `4`); the challenge/activation/MFA/credential stores
apply idempotent `CREATE TABLE IF NOT EXISTS` schemas on start.

```bash
python -m ops.migrate report --data-dir /var/lib/jarvis/data   # versions + integrity
python -m ops.migrate apply  --data-dir /var/lib/jarvis/data   # forward migrations
python -m ops.migrate verify --data-dir /var/lib/jarvis/data   # fail closed if not current
```

`apply` runs the real repository migration code path (never a duplicated
schema); a database stamped newer than the runtime understands fails closed.

## 8. Retention

- **Payment evidence** — private evidence objects are kept for dispute/audit
  purposes. Define a retention window (recommended: keep for the accounting/
  chargeback period, e.g. 180 days after final approve/reject, then delete the
  object and null its stored key). The code compensates by deleting evidence on
  failed DB persistence but does **not** auto-delete approved/rejected evidence;
  run a scheduled retention job that reads decided payments past the window and
  removes their evidence via the private object store. Log every deletion.
- **Admin decision audit** — the append-only `admin_decision_audits` table is
  retained indefinitely by default (it is the record of who approved/rejected
  what). If a retention limit is required, archive rows older than the policy
  window to cold storage before pruning; never delete audit rows for a payment
  still inside its dispute window.

## 9. Key rotation

Use `ops.rotate <key-type>` to generate new material and print the exact cutover
steps and honest side effects. It never silently replaces live material.

| Key | Command | Side effect |
| --- | --- | --- |
| Session secret | `ops.rotate session-secret` | All admin sessions/grants invalidated on restart; operators re-login. |
| MFA master key | `ops.rotate mfa-key` | Every admin must re-enrol their second factor. |
| Activation pepper | `ops.rotate activation-pepper` | Pending/unused activation codes must be re-issued; activated devices keep working (their cached certificate is entitlement-key signed). |
| Entitlement signing key | `ops.rotate entitlement-key --key-id …` | **Overlap required:** ship the new public key + id to clients alongside the old, then switch signing, then retire the old. Cached certificates stay valid while the old id is still pinned. |
| Release signing key | `ops.rotate release-key --key-id …` | **Overlap required:** add the new public key + id to `JARVIS_RELEASE_PUBLIC_KEYS_JSON` (up to 16), sign new artifacts with it, retire the old id only when nothing served depends on it. |

Never reuse an entitlement/release key id for different key material; never
remotely disable a purchased older version during any rotation.

## 10. SQLite single-process mode (current model)

The runtime is intentionally **one process** with SQLite plus in-memory admin
sessions, device-action grants, and rate limiters. Consequences:

- Run exactly **one** worker/replica. Multiple uvicorn workers or replicas would
  each hold private SQLite handles and unshared session/grant/rate state, so
  logins, CSRF, download grants, and rate limits would not be coherent across
  workers.
- Vertical scaling only. `PRAGMA busy_timeout` and foreign keys are enabled; keep
  the data directory on fast local disk, not a network filesystem.

## 11. Multi-instance plan (future)

Horizontal scale requires replacing single-process assumptions:

1. **Database** — migrate SQLite → PostgreSQL. The repository/read-store are
   already behind `CommerceRepository`/`ProductReadStore` ports; add a Postgres
   implementation and a real migration tool (Alembic or equivalent).
2. **Shared session / grant / rate-limit store** — replace the in-memory
   `AdminSessionManager`, `DeviceActionGrantManager`, and `BoundedAttemptLimiter`
   with a shared bounded store (Redis or Postgres-backed) so any instance can
   validate a session, consume a single-use grant, and count attempts globally.
3. **Object storage** — move payment evidence and release artifacts to private
   object storage with authenticated streaming reads, behind the existing
   `PrivatePaymentEvidenceStore` / `ReleaseArtifactStore` ports.
4. **Centralized observability** — ship the JSON access logs and `/metrics`
   counters to a central aggregator; keep the correlation ID across instances.

Until those are in place, keep the single-process constraint above.

## 12. Local production-like TLS dev environment

```bash
python -m ops.dev_tls --out-dir ./devcerts --serve   # self-signed cert + uvicorn TLS
# set JARVIS_REQUIRE_HTTPS=true and add 'localhost' to JARVIS_API_ALLOWED_HOSTS
```

uvicorn terminates TLS directly, so the app sees an `https` scheme locally and
exercises the HTTPS-only policy + HSTS exactly as in production, without a
separate proxy. The generated key is owner-only and gitignored. This is a dev
convenience, not a production certificate.

## 13. Cross-platform hosting

The backend runtime enforces POSIX owner-only permissions on secret files and
the data directory, so the hardened service must run on **Linux or macOS** (or a
Linux container). The `ops/*` tooling runs on macOS, Windows, and Linux; on
Windows it returns an honest `manual` status with NTFS ACL guidance instead of
faking `0600`. To host on Windows, run the Docker image (Linux container).

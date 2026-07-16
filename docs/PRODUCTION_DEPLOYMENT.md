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
                    │  nginx edge rate limit;       │              │
                    │  Caddy needs external limit   │              │
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
as owner-only files immediately before start. `.gitignore` explicitly excludes
`deploy/env/backend.env` in addition to `*.key`, `*.pem`, `*.sqlite3`,
`.env`/`.env.*`, and the payment-instructions and product config files. The root
`.dockerignore` is a deny-by-default allowlist, so the Docker daemon receives
only `product_backend/`, `core/`, `ops/`, the Dockerfile and the backend
dependency manifest — never the rest of the repository.

### Docker Compose path and secret boundary

The Compose recipe requires five `JARVIS_COMPOSE_*` variables containing
**absolute host paths**: the owner-only backend env file, entitlement key,
activation pepper, MFA key and TLS certificate directory. It overrides the
corresponding runtime values with fixed in-container paths. Only the entitlement
key, activation pepper and MFA key are mounted read-only into the backend. The
release-signing private key is an offline signing asset and must never be mounted
or copied into this online service.

The backend dependency manifest pins the six directly tested packages. The
transitive dependency graph and container base images are not hash/digest locked
yet, so rebuilds are not claimed bit-for-bit reproducible; add a reviewed hash
lock and immutable base-image digests before a production supply-chain sign-off.

On the Linux production host, the three backend secret files must be mode `0600`
and owned by container UID `10001`. First render and inspect the fully substituted
configuration, then validate the assembled runtime **inside** the one-shot
container where `/var/lib/jarvis` and `/run/jarvis-secrets` exist:

```bash
docker compose -f deploy/docker/docker-compose.yml config --quiet
docker compose -f deploy/docker/docker-compose.yml build backend
docker compose -f deploy/docker/docker-compose.yml run --rm --no-deps backend \
    python -m ops.validate_config
docker compose -f deploy/docker/docker-compose.yml up -d
```

Host-side validation of that Compose env is invalid because the fixed container
paths do not exist on the host. For systemd, by contrast, continue to run the
host-side `ExecStartPre` validation against `/etc/jarvis/backend.env`.

## 3. HTTPS-only policy and forwarded headers

- `JARVIS_REQUIRE_HTTPS=true` makes the app reject non-HTTPS requests (`400`), or
  308-redirect safe GET/HEAD requests when `JARVIS_HTTPS_REDIRECT=true`. When the
  request is HTTPS it emits `Strict-Transport-Security` (`JARVIS_HSTS_MAX_AGE`).
- The forwarded scheme (`X-Forwarded-Proto`) and forwarded client
  (`X-Forwarded-For`) are trusted **only** when the direct socket peer is in
  `JARVIS_TRUSTED_PROXIES`. With no configured proxy the forwarded headers are
  ignored and the socket peer is authoritative, so a client cannot spoof HTTPS or
  its rate-limit identity. The recipes pin this to exactly `172.30.250.2/32`
  (Compose nginx) or `127.0.0.1/32` (systemd proxy). Do not enable uvicorn's
  proxy-header rewriting: the app must receive the raw socket peer so its own
  explicit trust policy can decide whether forwarded headers are authoritative.
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

Uvicorn's raw access log is disabled in both recipes to avoid a second,
unredacted request-target log. Artifact download grants are carried in the
`X-Artifact-Grant` request header; proxies must forward that header but must not
log it. A grant is never placed in a query string or public artifact URL.

## 5. Rate limiting (edge + app)

The app enforces bounded in-process limits on admin login, MFA, activation, and
purchase flows. These are **not** a DDoS defense. The shipped nginx recipe adds
`limit_req` limits and caps the payment multipart body at 11 MiB (10 MiB image
plus bounded framing). Stock Caddy has no built-in request-rate limiter: the
shipped Caddyfile therefore requires a provider/WAF limit or a separately
reviewed rate-limit module before public use. Do not claim the stock Caddy recipe
provides an edge limiter.

## 6. Backup and restore

```bash
# A cross-database backup requires a real maintenance window.
sudo systemctl stop jarvis-backend
python -m ops.backup --data-dir /var/lib/jarvis/data \
    --backup-dir /var/backups/jarvis/20260717T120000Z \
    --confirm-service-stopped
sudo systemctl start jarvis-backend

# Restore only into a fresh sibling while the service remains stopped.
sudo systemctl stop jarvis-backend
python -m ops.restore --backup-dir /var/backups/jarvis/<stamp> \
    --data-dir /var/lib/jarvis/restored-<stamp>
```

- `ops.backup` refuses to run without explicit stopped-service confirmation. It
  requires all five SQLite stores and the private `payment-evidence/` root,
  validates each database's application schema, copies evidence through
  no-follow bounded reads, and hashes every file into `manifest.json`.
- The source must remain the backend-created, owner-controlled `0700` data
  directory throughout the maintenance window. Never point backup at a
  world-writable or attacker-controlled tree; the confirmation flag cannot
  prove that the service really stopped or establish an external process lock.
- `ops.restore` requires a fresh nonexistent target. It verifies the complete
  evidence tree, strict manifest, hashes, SQLite integrity and schema, stages the
  whole tree on the target filesystem, then publishes it with one directory
  rename. Overlay/`--force` restore is intentionally `not_available`.
- Manifest SHA-256 values detect corruption but are **not an authenticity
  signature**. Backup storage is therefore a trusted boundary: keep each backup
  owner-only and immutable or add an independently managed signature/MAC before
  transporting it. Do not restore an operator-supplied untrusted archive.
- **Restore drills:** after restore, confirm account/license/release/payment/
  entitlement rows, activation one-time state and private evidence bytes; run
  `ops.migrate verify` against the restored directory. Only then switch the
  service data path and restart. Keep the old tree read-only until the drill and
  health check pass.
- Secure backup/restore mutation is implemented only with POSIX no-follow
  primitives on Linux/macOS. Native Windows execution returns honest
  `not_available`; use the reviewed Linux container/host path instead.

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
| Session secret | `ops.rotate session-secret --out-dir /secure/rotation` | All admin sessions/grants invalidated on restart; operators re-login. |
| MFA master key | `ops.rotate mfa-key --out-dir /secure/rotation` | **`not_available`:** stored TOTP secrets and recovery authenticators need an authenticated transactional re-encryption workflow; generating and switching a fresh key would lock out every admin. |
| Activation pepper | `ops.rotate activation-pepper --out-dir /secure/rotation` | Pending/unused activation codes must be re-issued; activated devices keep working (their cached certificate is entitlement-key signed). |
| Entitlement signing key | `ops.rotate entitlement-key --out-dir /secure/rotation --key-id …` | **Overlap required:** ship the new public key + id to clients alongside the old, then switch signing, then retire the old. Cached certificates stay valid while the old id is still pinned. |
| Release signing key | `ops.rotate release-key --out-dir /secure/rotation --key-id …` | **Overlap required:** add the new public key + id to `JARVIS_RELEASE_PUBLIC_KEYS_JSON` (up to 16), sign new artifacts with it, retire the old id only when nothing served depends on it. |

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
the data directory. The shipped Compose recipe is production-targeted at Linux,
where the three bind-mounted backend secrets can be owned by UID `10001`.
Docker Desktop bind-mount ownership behavior has not been production-verified
on macOS or Windows and is not claimed ready. Secure mutating `ops/*` commands
use POSIX owner/no-follow primitives on Linux/macOS; native Windows returns
honest `not_available` instead of writing first or faking `0600`.

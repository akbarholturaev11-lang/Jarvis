# JARVIS backend deployment recipes

Two supported topologies; both terminate TLS at a reverse proxy and forward to
the single-process backend on loopback / an internal network. See
[`docs/PRODUCTION_DEPLOYMENT.md`](../docs/PRODUCTION_DEPLOYMENT.md) for the full
runbook (config reference, backup/restore, migration, key rotation, retention).

```
Internet ──HTTPS──▶ reverse proxy (nginx/Caddy) ──HTTP──▶ uvicorn ──▶ product_backend
                    TLS, HSTS, host check,               (127.0.0.1:8080     SQLite +
                    admin allowlist; nginx limit          /8080 internal)     evidence
```

## Contents

| Path | Purpose |
| --- | --- |
| `env/backend.env.example` | Complete environment reference (copy, fill, keep owner-only, never commit). |
| `systemd/jarvis-backend.service` | Native Linux service unit with sandboxing + fail-closed `ExecStartPre` validation. |
| `docker/Dockerfile` | Slim, non-root backend image (backend + core + ops only). |
| `docker/docker-compose.yml` | Backend + nginx TLS proxy; backend port is not published. |
| `docker/requirements-backend.txt` | Minimal server dependencies (no desktop GUI/audio/vision). |
| `reverse-proxy/nginx.conf` | TLS, HSTS, trusted host, edge rate limit, admin allowlist, health passthrough. |
| `reverse-proxy/Caddyfile` | Automatic TLS alternative; stock Caddy needs an external/provider rate limiter. |

## Quick start (Docker)

```bash
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0700 \
    /srv/jarvis /srv/jarvis/secrets /srv/jarvis/tls
python -m ops.gen_secrets --out-dir /srv/jarvis/secrets \
    --admin-subject admin:ops --allowed-hosts api.example.com \
    --env-file /srv/jarvis/backend.env

# Move release-signing.key to the offline signer. Only these three runtime
# files are exposed read-only to the backend. Runtime UID 10001 must own them.
test ! -e /srv/jarvis/secrets/release-signing.key
sudo chown 10001:10001 /srv/jarvis/secrets/entitlement.key \
    /srv/jarvis/secrets/activation.pepper \
    /srv/jarvis/secrets/admin-mfa.key
sudo chmod 0600 /srv/jarvis/secrets/entitlement.key \
    /srv/jarvis/secrets/activation.pepper \
    /srv/jarvis/secrets/admin-mfa.key

# Put fullchain.pem + privkey.pem in /srv/jarvis/tls, then export ABSOLUTE
# host paths. Keep these exports in an owner-only operator file outside Git.
export JARVIS_COMPOSE_ENV_FILE=/srv/jarvis/backend.env
export JARVIS_COMPOSE_ENTITLEMENT_KEY_FILE=/srv/jarvis/secrets/entitlement.key
export JARVIS_COMPOSE_ACTIVATION_PEPPER_FILE=/srv/jarvis/secrets/activation.pepper
export JARVIS_COMPOSE_ADMIN_MFA_KEY_FILE=/srv/jarvis/secrets/admin-mfa.key
export JARVIS_COMPOSE_TLS_CERT_DIR=/srv/jarvis/tls

docker compose -f deploy/docker/docker-compose.yml config --quiet
docker compose -f deploy/docker/docker-compose.yml build backend
docker compose -f deploy/docker/docker-compose.yml run --rm --no-deps backend \
    python -m ops.validate_config
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Do not run the Compose env through host-side `ops.validate_config`: its data and
secret paths are intentionally fixed container paths under `/var/lib/jarvis`
and `/run/jarvis-secrets`. The one-shot container check above validates the
actual mounts and ownership before startup. The release-signing private key
stays offline and is never a Compose bind mount.

## Quick start (systemd)

```bash
sudo install -o jarvis -g jarvis -m 0600 deploy/env/backend.env /etc/jarvis/backend.env
sudo cp deploy/systemd/jarvis-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jarvis-backend
```

## Build the client config

`ops.gen_secrets` also writes `client-trust.json` (non-secret entitlement +
release public keys). Turn it into the pinned client `config/product.json` with
the real API origin, then bundle that file into the desktop build:

```bash
python -m ops.build_client_config \
    --trust-file /srv/jarvis/secrets/client-trust.json \
    --api-base-url https://api.example.com \
    --out config/product.json
```

The origin must be HTTPS and `allow_insecure_localhost` stays `false` for any
customer build. See [`docs/PRODUCT_BACKEND_OPERATIONS.md`](../docs/PRODUCT_BACKEND_OPERATIONS.md)
for key rotation and the development-only `--allow-insecure-localhost` flag.

## Hosting note (cross-platform)

The backend runtime enforces **POSIX** owner-only permissions on its secret
files and data directory. The shipped Compose recipe is production-targeted at
a Linux host where bind-mounted files can be owned by container UID `10001`;
Docker Desktop ownership semantics are not production-verified. Secure mutating
`ops/*` commands use POSIX owner/no-follow primitives on Linux/macOS; native
Windows reports honest `not_available` instead of writing first or faking
`0600`.

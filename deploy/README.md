# JARVIS backend deployment recipes

Two supported topologies; both terminate TLS at a reverse proxy and forward to
the single-process backend on loopback / an internal network. See
[`docs/PRODUCTION_DEPLOYMENT.md`](../docs/PRODUCTION_DEPLOYMENT.md) for the full
runbook (config reference, backup/restore, migration, key rotation, retention).

```
Internet ──HTTPS──▶ reverse proxy (nginx/Caddy) ──HTTP──▶ uvicorn ──▶ product_backend
                    TLS, HSTS, host check,               (127.0.0.1:8080     SQLite +
                    edge rate limit, admin allowlist      /8080 internal)     evidence
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
| `reverse-proxy/Caddyfile` | Same, using Caddy automatic TLS. |

## Quick start (Docker)

```bash
python -m ops.gen_secrets --out-dir ./secrets \
    --admin-subject admin:ops --allowed-hosts api.example.com \
    --env-file deploy/env/backend.env
# put real TLS certs in deploy/docker/certs/{fullchain,privkey}.pem
python -m ops.validate_config --env-file deploy/env/backend.env
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

## Quick start (systemd)

```bash
sudo install -o jarvis -g jarvis -m 0600 deploy/env/backend.env /etc/jarvis/backend.env
sudo cp deploy/systemd/jarvis-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jarvis-backend
```

## Hosting note (cross-platform)

The backend runtime enforces **POSIX** owner-only permissions on its secret
files and data directory, so the hardened service must run on **Linux or
macOS** (or a Linux container). The `ops/*` tooling itself runs on macOS,
Windows, and Linux; on Windows it reports an honest `manual` status with NTFS
ACL guidance instead of faking `0600`. To host on Windows, run the container.

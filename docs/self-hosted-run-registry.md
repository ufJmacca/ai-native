# Self-Hosted Run Registry

This document describes how to operate the standalone Run Registry service package at `services/run_registry`.

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `RUN_REGISTRY_DB_HOST` | Yes | PostgreSQL hostname or service name. |
| `RUN_REGISTRY_DB_PORT` | Yes | PostgreSQL port (default `5432`). |
| `RUN_REGISTRY_DB_NAME` | Yes | Database name used by the service. |
| `RUN_REGISTRY_DB_USER` | Yes | Database user name. |
| `RUN_REGISTRY_DB_PASSWORD` | Yes | Database password for `RUN_REGISTRY_DB_USER`. |
| `RUN_REGISTRY_AUTH_TOKEN` | Yes | Static bearer token expected in `Authorization: Bearer <token>`. |
| `RUN_REGISTRY_HOST` | No | Bind host for the web server (default `0.0.0.0`). |
| `RUN_REGISTRY_PORT` | No | Bind port for the web server (default `8080`). |
| `RUN_REGISTRY_CORS_ORIGINS` | No | Comma-separated allowed origins (default `*`). |
| `RUN_REGISTRY_RETENTION_DAYS` | No | Days before records expire and are purged (default `30`). |
| `RUN_REGISTRY_LIVENESS_TTL_SECONDS` | No | Age in seconds before a non-terminal run becomes `stale` (default `60`). |
| `RUN_REGISTRY_LIVENESS_GRACE_PERIOD_SECONDS` | No | Extra age in seconds before a `stale` run becomes `stopped` (default `120`). |

## Deployment artifacts

- Python project/dependencies (uv-managed): `services/run_registry/pyproject.toml`
- Container image definition: `services/run_registry/Dockerfile`
- Local stack: `services/run_registry/docker-compose.yml` (service + PostgreSQL)
- Kubernetes sample manifests: `services/run_registry/k8s/run-registry.yaml`
- Operator dashboard SPA: `services/run_registry_ui`

## What the service exposes

The registry backend stores both summary data for fast lists and a rich snapshot for detailed inspection.

- `GET /v1/runs` returns run summaries for dashboard tables.
- `GET /v1/runs/{run_id}` returns the full published snapshot, including `run_projection`, `stage_status`, and `slice_states`.
- `PUT /v1/runs/{run_id}` is the idempotent producer write endpoint used by `ai_native`.
- Liveness is computed server-side from `last_heartbeat_at`, terminal status, and the configured TTL/grace values.

## Local development flow

1. Start the full local stack:

```bash
docker compose -f services/run_registry/docker-compose.yml up --build
```

2. Open `http://localhost:3000`, enter `http://localhost:8080` as the API base URL, and use the configured bearer token.

Notes:
- The first UI release is read-only.
- The sample backend Compose file already sets `RUN_REGISTRY_CORS_ORIGINS=http://localhost:3000`.
- The UI is deployed separately as static assets; it is not bundled into the FastAPI container.

If you prefer to run the UI outside Docker during development:

```bash
cd services/run_registry_ui
npm install
npm run dev
```

## Upgrade and migration flow

1. Update dependencies (if needed) via `uv add` / `uv remove` in `services/run_registry`, then rebuild image artifacts.
2. Build and push a new image tag.
3. Update deployment manifests (`docker-compose.yml` image reference or Kubernetes deployment image).
4. Roll out the application.
5. On service startup, SQL migrations in `services/run_registry/migrations/*.sql` are applied automatically using the `schema_migrations` table.
6. Verify with `GET /v1/health` and a smoke request against `GET /v1/runs`.

### Rollback

- Roll back to the previous application image.
- If a migration introduced a backward-incompatible schema change, restore from a pre-upgrade backup before restarting old binaries.

## Backup and restore notes

### PostgreSQL backup

Use logical backups for portability:

```bash
pg_dump -h <db-host> -U <db-user> -d <db-name> -Fc -f run-registry.dump
```

### PostgreSQL restore

```bash
pg_restore -h <db-host> -U <db-user> -d <db-name> --clean --if-exists run-registry.dump
```

Notes:
- Back up before every schema upgrade.
- Validate restore in a staging environment before production use.
- Include persistent volume snapshots when using Kubernetes StatefulSets.

## TLS and reverse-proxy guidance

The service itself serves HTTP. Terminate TLS at an ingress or reverse proxy (for example NGINX, Envoy, ALB, or API Gateway).

Recommended proxy settings:
- Force HTTPS redirects.
- Set `X-Forwarded-Proto` and `X-Forwarded-For` headers.
- Apply request size and rate limits.
- Restrict CORS via `RUN_REGISTRY_CORS_ORIGINS` to trusted origins.
- Rotate `RUN_REGISTRY_AUTH_TOKEN` regularly and distribute through a secret manager.
- Publish a separate UI origin and add that origin explicitly to `RUN_REGISTRY_CORS_ORIGINS`.

Example NGINX location forwarding:

```nginx
location / {
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://run-registry:8080;
}
```

## API compatibility

All routes are versioned under `/v1/...` to support independent client and server upgrade cycles.

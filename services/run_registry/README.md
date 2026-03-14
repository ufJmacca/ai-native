# Run Registry Service

Standalone registry backend for `ai-native` run tracking, plus a separate React operator dashboard in `services/run_registry_ui`.

## API surface

All endpoints are versioned under `/v1`:

- `GET /v1/health`
- `POST /v1/runs`
- `PUT /v1/runs/{run_id}`
- `GET /v1/runs`
- `GET /v1/runs/{run_id}`
- `DELETE /v1/runs/{run_id}`
- `POST /v1/maintenance/purge-expired`

The backend stores indexed run summary fields at the top level and richer execution data in JSONB columns so list views stay fast while detail views can include `run_projection`, `stage_status`, and `slice_states`.

## Local development

### Backend API

```bash
cd services/run_registry
uv sync --group dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Required environment variables:

- `RUN_REGISTRY_DB_HOST`
- `RUN_REGISTRY_DB_NAME`
- `RUN_REGISTRY_DB_USER`
- `RUN_REGISTRY_DB_PASSWORD`
- `RUN_REGISTRY_AUTH_TOKEN`

Optional environment variables:

- `RUN_REGISTRY_DB_PORT`
- `RUN_REGISTRY_CORS_ORIGINS`
- `RUN_REGISTRY_RETENTION_DAYS`
- `RUN_REGISTRY_LIVENESS_TTL_SECONDS`
- `RUN_REGISTRY_LIVENESS_GRACE_PERIOD_SECONDS`

### Operator dashboard

```bash
cd services/run_registry_ui
npm install
npm run dev
```

The UI runs on `http://localhost:3000`, prompts for the API base URL and bearer token, stores that token in `sessionStorage`, and polls the backend every 15 seconds while the tab is visible.

## Local deployment with Docker Compose

```bash
docker compose -f services/run_registry/docker-compose.yml up --build
```

This stack now starts:

- PostgreSQL on `localhost:5432`
- the registry backend on `http://localhost:8080`
- the operator UI on `http://localhost:3000`

The sample Compose file already allows CORS from `http://localhost:3000` for local UI development.

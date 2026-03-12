# Run Registry Service

Standalone registry API for AI run metadata, designed for customer-managed hosting.

## API versioning
All endpoints are namespaced under `/v1`:

- `GET /v1/health`
- `POST /v1/runs`
- `GET /v1/runs`
- `GET /v1/runs/{run_id}`
- `DELETE /v1/runs/{run_id}`
- `POST /v1/maintenance/purge-expired`

## Local development with uv

```bash
cd services/run_registry
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Local deployment with Docker Compose

```bash
docker compose -f services/run_registry/docker-compose.yml up --build
```

The service is available at `http://localhost:8080`.

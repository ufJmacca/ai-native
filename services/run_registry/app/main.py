from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import load_settings
from .db import Database


class RunCreateRequest(BaseModel):
    run_id: UUID
    workflow: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    run_id: UUID
    workflow: str
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class HealthResponse(BaseModel):
    status: str


settings = load_settings()
database = Database(settings)

app = FastAPI(title="Run Registry", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    database.migrate()
    database.purge_expired()



def require_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {settings.auth_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


@app.get("/v1/health", response_model=HealthResponse)
def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/v1/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_auth)],
)
def create_run(request: RunCreateRequest) -> RunResponse:
    expires_at = datetime.now(UTC) + timedelta(days=settings.retention_days)
    with database.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, workflow, status, metadata, expires_at)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET workflow = EXCLUDED.workflow,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING run_id, workflow, status, metadata, created_at, updated_at, expires_at
                """,
                (
                    str(request.run_id),
                    request.workflow,
                    request.status,
                    json.dumps(request.metadata),
                    expires_at,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return RunResponse(**row)


@app.get(
    "/v1/runs/{run_id}",
    response_model=RunResponse,
    dependencies=[Depends(require_auth)],
)
def get_run(run_id: UUID) -> RunResponse:
    with database.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, workflow, status, metadata, created_at, updated_at, expires_at
                FROM runs
                WHERE run_id = %s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunResponse(**row)


@app.get(
    "/v1/runs",
    response_model=list[RunResponse],
    dependencies=[Depends(require_auth)],
)
def list_runs(limit: int = Query(default=50, ge=1)) -> list[RunResponse]:
    with database.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, workflow, status, metadata, created_at, updated_at, expires_at
                FROM runs
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [RunResponse(**row) for row in rows]


@app.delete(
    "/v1/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_auth)],
)
def delete_run(run_id: UUID) -> None:
    with database.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM runs WHERE run_id = %s", (str(run_id),))
        conn.commit()


@app.post(
    "/v1/maintenance/purge-expired",
    dependencies=[Depends(require_auth)],
)
def purge_expired() -> dict[str, int]:
    deleted = database.purge_expired()
    return {"deleted": deleted}

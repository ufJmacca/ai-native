from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .db import Database


class RunCreateRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=255)
    workflow: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunSnapshotRequest(BaseModel):
    workflow: str = Field(min_length=1, max_length=128)
    feature_slug: str | None = Field(default=None, max_length=256)
    spec_path: str | None = None
    workspace_root: str | None = None
    run_dir: str | None = None
    status: str = Field(min_length=1, max_length=64)
    current_stage: str | None = Field(default=None, max_length=64)
    scheduler_status: str | None = Field(default="idle", max_length=64)
    active_slice: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_projection: dict[str, Any] | None = None
    stage_status: dict[str, Any] = Field(default_factory=dict)
    slice_states: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    last_heartbeat_at: datetime | None = None


class RunSummaryResponse(BaseModel):
    run_id: str
    workflow: str
    feature_slug: str | None = None
    spec_path: str | None = None
    workspace_root: str | None = None
    status: str
    current_stage: str | None = None
    scheduler_status: str | None = None
    active_slice: str | None = None
    created_at: datetime
    updated_at: datetime
    last_heartbeat_at: datetime | None = None
    expires_at: datetime
    liveness: str


class RunDetailResponse(RunSummaryResponse):
    run_dir: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_projection: dict[str, Any] | None = None
    stage_status: dict[str, Any] = Field(default_factory=dict)
    slice_states: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str


def _parse_timestamp(timestamp: datetime | None) -> datetime | None:
    return timestamp.astimezone(UTC) if timestamp is not None else None


def _classify_liveness(row: dict[str, Any], settings: Settings, now: datetime | None = None) -> str:
    if row["status"] in {"completed", "failed"}:
        return "stopped"

    observed = _parse_timestamp(row.get("last_heartbeat_at")) or _parse_timestamp(row.get("updated_at"))
    if observed is None:
        return "stopped"

    age_seconds = ((now or datetime.now(UTC)) - observed).total_seconds()
    if age_seconds <= settings.liveness_ttl_seconds:
        return "active"
    if age_seconds <= settings.liveness_ttl_seconds + settings.liveness_grace_period_seconds:
        return "stale"
    return "stopped"


def _summary_response(row: dict[str, Any], settings: Settings) -> RunSummaryResponse:
    return RunSummaryResponse(
        run_id=row["run_id"],
        workflow=row["workflow"],
        feature_slug=row.get("feature_slug"),
        spec_path=row.get("spec_path"),
        workspace_root=row.get("workspace_root"),
        status=row["status"],
        current_stage=row.get("current_stage"),
        scheduler_status=row.get("scheduler_status"),
        active_slice=row.get("active_slice"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_heartbeat_at=row.get("last_heartbeat_at"),
        expires_at=row["expires_at"],
        liveness=_classify_liveness(row, settings),
    )


def _detail_response(row: dict[str, Any], settings: Settings) -> RunDetailResponse:
    summary = _summary_response(row, settings)
    return RunDetailResponse(
        **summary.model_dump(),
        run_dir=row.get("run_dir"),
        metadata=row.get("metadata") or {},
        run_projection=row.get("run_projection"),
        stage_status=row.get("stage_status") or {},
        slice_states=row.get("slice_states") or {},
    )


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    app_settings = settings or load_settings()
    app_database = database or Database(app_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app_database.migrate()
        app_database.purge_expired()
        yield

    app = FastAPI(title="Run Registry", version="1.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {app_settings.auth_token}"
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
        response_model=RunDetailResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_auth)],
    )
    def create_run(request: RunCreateRequest) -> RunDetailResponse:
        now = datetime.now(UTC)
        row = app_database.upsert_run(
            request.run_id,
            workflow=request.workflow,
            feature_slug=None,
            spec_path=None,
            workspace_root=None,
            run_dir=None,
            status=request.status,
            current_stage=None,
            scheduler_status="idle",
            active_slice=None,
            metadata=request.metadata,
            run_projection=None,
            stage_status={},
            slice_states={},
            created_at=now,
            updated_at=now,
            last_heartbeat_at=now,
            expires_at=now + timedelta(days=app_settings.retention_days),
        )
        return _detail_response(row, app_settings)

    @app.put(
        "/v1/runs/{run_id}",
        response_model=RunDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    def upsert_run(run_id: str, request: RunSnapshotRequest) -> RunDetailResponse:
        row = app_database.upsert_run(
            run_id,
            workflow=request.workflow,
            feature_slug=request.feature_slug,
            spec_path=request.spec_path,
            workspace_root=request.workspace_root,
            run_dir=request.run_dir,
            status=request.status,
            current_stage=request.current_stage,
            scheduler_status=request.scheduler_status,
            active_slice=request.active_slice,
            metadata=request.metadata,
            run_projection=request.run_projection,
            stage_status=request.stage_status,
            slice_states=request.slice_states,
            created_at=request.created_at,
            updated_at=request.updated_at,
            last_heartbeat_at=request.last_heartbeat_at,
            expires_at=datetime.now(UTC) + timedelta(days=app_settings.retention_days),
        )
        return _detail_response(row, app_settings)

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    def get_run(run_id: str) -> RunDetailResponse:
        row = app_database.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return _detail_response(row, app_settings)

    @app.get(
        "/v1/runs",
        response_model=list[RunSummaryResponse],
        dependencies=[Depends(require_auth)],
    )
    def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> list[RunSummaryResponse]:
        rows = app_database.list_runs(limit=limit)
        return [_summary_response(row, app_settings) for row in rows]

    @app.delete(
        "/v1/runs/{run_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        dependencies=[Depends(require_auth)],
    )
    def delete_run(run_id: str) -> Response:
        app_database.delete_run(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/maintenance/purge-expired",
        dependencies=[Depends(require_auth)],
    )
    def purge_expired() -> dict[str, int]:
        deleted = app_database.purge_expired()
        return {"deleted": deleted}

    return app


app = create_app()

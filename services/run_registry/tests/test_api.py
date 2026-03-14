from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RUN_REGISTRY_DB_HOST", "db")
os.environ.setdefault("RUN_REGISTRY_DB_NAME", "run_registry")
os.environ.setdefault("RUN_REGISTRY_DB_USER", "user")
os.environ.setdefault("RUN_REGISTRY_DB_PASSWORD", "password")
os.environ.setdefault("RUN_REGISTRY_AUTH_TOKEN", "secret-token")

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.migrated = False
        self.purged = 0

    def migrate(self) -> None:
        self.migrated = True

    def purge_expired(self) -> int:
        self.purged += 1
        return 0

    def upsert_run(self, run_id: UUID, **payload) -> dict:
        row = {"run_id": run_id, **payload}
        self.rows[str(run_id)] = row
        return row

    def get_run(self, run_id: UUID) -> dict | None:
        return self.rows.get(str(run_id))

    def list_runs(self, limit: int) -> list[dict]:
        rows = list(self.rows.values())
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return rows[:limit]

    def delete_run(self, run_id: UUID) -> None:
        self.rows.pop(str(run_id), None)


def _settings() -> Settings:
    return Settings(
        app_host="0.0.0.0",
        app_port=8080,
        database_host="db",
        database_port=5432,
        database_name="run_registry",
        database_user="user",
        database_password="password",
        auth_token="secret-token",
        cors_origins=["http://localhost:3000"],
        retention_days=30,
        liveness_ttl_seconds=60,
        liveness_grace_period_seconds=120,
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


def test_auth_is_required_for_registry_routes() -> None:
    client = TestClient(create_app(settings=_settings(), database=FakeDatabase()))

    response = client.get("/v1/runs")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_put_run_snapshot_returns_rich_detail_and_list_summary() -> None:
    database = FakeDatabase()
    client = TestClient(create_app(settings=_settings(), database=database))
    run_id = uuid4()
    created_at = datetime.now(UTC) - timedelta(minutes=3)
    updated_at = datetime.now(UTC)
    last_heartbeat_at = updated_at - timedelta(seconds=10)

    put_response = client.put(
        f"/v1/runs/{run_id}",
        headers=_auth_headers(),
        json={
            "workflow": "ai-native",
            "feature_slug": "task-management",
            "spec_path": "/workspace/specs/task.md",
            "workspace_root": "/workspace/app",
            "run_dir": "/workspace/.ai-native/runs/run-1",
            "status": "in_progress",
            "current_stage": "verify",
            "scheduler_status": "running",
            "active_slice": "S002",
            "metadata": {"heartbeat": {"updated_at": last_heartbeat_at.isoformat()}},
            "run_projection": {"schema_version": 1, "completed_steps": ["intake"]},
            "stage_status": {"verify": {"stage": "verify", "status": "completed", "artifacts": [], "notes": []}},
            "slice_states": {"S002": {"slice_id": "S002", "status": "running", "current_stage": "verify"}},
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "last_heartbeat_at": last_heartbeat_at.isoformat(),
        },
    )

    assert put_response.status_code == 200
    detail = put_response.json()
    assert detail["run_id"] == str(run_id)
    assert detail["feature_slug"] == "task-management"
    assert detail["current_stage"] == "verify"
    assert detail["scheduler_status"] == "running"
    assert detail["active_slice"] == "S002"
    assert detail["liveness"] == "active"
    assert detail["run_projection"]["schema_version"] == 1
    assert detail["slice_states"]["S002"]["status"] == "running"

    list_response = client.get("/v1/runs?limit=10", headers=_auth_headers())

    assert list_response.status_code == 200
    summary = list_response.json()[0]
    assert summary["run_id"] == str(run_id)
    assert summary["feature_slug"] == "task-management"
    assert summary["liveness"] == "active"
    assert "run_projection" not in summary


def test_get_run_returns_stopped_liveness_for_terminal_status() -> None:
    database = FakeDatabase()
    client = TestClient(create_app(settings=_settings(), database=database))
    run_id = uuid4()
    now = datetime.now(UTC)
    database.upsert_run(
        run_id,
        workflow="ai-native",
        feature_slug="cleanup",
        spec_path="/workspace/specs/cleanup.md",
        workspace_root="/workspace/app",
        run_dir="/workspace/.ai-native/runs/run-2",
        status="completed",
        current_stage="pr",
        scheduler_status="completed",
        active_slice=None,
        metadata={},
        run_projection={"schema_version": 1},
        stage_status={},
        slice_states={},
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=1),
        last_heartbeat_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=30),
    )

    response = client.get(f"/v1/runs/{run_id}", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["liveness"] == "stopped"


def test_get_run_returns_stale_liveness_for_old_heartbeat() -> None:
    database = FakeDatabase()
    client = TestClient(create_app(settings=_settings(), database=database))
    run_id = uuid4()
    now = datetime.now(UTC)
    database.upsert_run(
        run_id,
        workflow="ai-native",
        feature_slug="stale-run",
        spec_path="/workspace/specs/stale.md",
        workspace_root="/workspace/app",
        run_dir="/workspace/.ai-native/runs/run-3",
        status="in_progress",
        current_stage="loop",
        scheduler_status="running",
        active_slice="S101",
        metadata={},
        run_projection={"schema_version": 1},
        stage_status={},
        slice_states={},
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(seconds=70),
        last_heartbeat_at=now - timedelta(seconds=70),
        expires_at=now + timedelta(days=30),
    )

    response = client.get(f"/v1/runs/{run_id}", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["liveness"] == "stale"


def test_legacy_post_run_create_remains_supported() -> None:
    database = FakeDatabase()
    client = TestClient(create_app(settings=_settings(), database=database))
    run_id = uuid4()

    response = client.post(
        "/v1/runs",
        headers=_auth_headers(),
        json={
            "run_id": str(run_id),
            "workflow": "legacy-client",
            "status": "in_progress",
            "metadata": {"source": "test"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["run_id"] == str(run_id)
    assert payload["workflow"] == "legacy-client"
    assert payload["metadata"] == {"source": "test"}


def test_delete_run_removes_registry_entry() -> None:
    database = FakeDatabase()
    client = TestClient(create_app(settings=_settings(), database=database))
    run_id = uuid4()
    now = datetime.now(UTC)
    database.upsert_run(
        run_id,
        workflow="ai-native",
        feature_slug="delete-me",
        spec_path=None,
        workspace_root=None,
        run_dir=None,
        status="failed",
        current_stage="verify",
        scheduler_status="failed",
        active_slice=None,
        metadata={},
        run_projection=None,
        stage_status={},
        slice_states={},
        created_at=now,
        updated_at=now,
        last_heartbeat_at=now,
        expires_at=now + timedelta(days=30),
    )

    delete_response = client.delete(f"/v1/runs/{run_id}", headers=_auth_headers())
    get_response = client.get(f"/v1/runs/{run_id}", headers=_auth_headers())

    assert delete_response.status_code == 204
    assert get_response.status_code == 404

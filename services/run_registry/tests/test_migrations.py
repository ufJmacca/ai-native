from __future__ import annotations

from pathlib import Path


def test_snapshot_migration_adds_dashboard_columns() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "002_run_snapshots.sql"
    ).read_text(encoding="utf-8")
    run_id_migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "003_run_id_text.sql"
    ).read_text(encoding="utf-8")

    assert "feature_slug TEXT" in migration
    assert "spec_path TEXT" in migration
    assert "workspace_root TEXT" in migration
    assert "run_dir TEXT" in migration
    assert "current_stage TEXT" in migration
    assert "scheduler_status TEXT" in migration
    assert "active_slice TEXT" in migration
    assert "last_heartbeat_at TIMESTAMPTZ" in migration
    assert "run_projection JSONB" in migration
    assert "stage_status JSONB" in migration
    assert "slice_states JSONB" in migration
    assert "ALTER COLUMN run_id TYPE TEXT" in run_id_migration

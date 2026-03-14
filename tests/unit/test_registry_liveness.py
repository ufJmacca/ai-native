from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_native.config import RegistryConfig
from ai_native.models import RunHeartbeat
from ai_native.state import StateStore


def test_liveness_transitions_and_terminal_status_is_authoritative(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    registry = RegistryConfig(liveness_ttl_seconds=30, liveness_grace_period_seconds=60)
    now = datetime.now(UTC)

    state.updated_at = (now - timedelta(seconds=10)).isoformat()
    assert store.classify_liveness(state, registry, now=now) == "active"

    state.updated_at = (now - timedelta(seconds=45)).isoformat()
    assert store.classify_liveness(state, registry, now=now) == "stale"

    state.updated_at = (now - timedelta(seconds=200)).isoformat()
    assert store.classify_liveness(state, registry, now=now) == "stopped"

    state.status = "completed"
    state.updated_at = now.isoformat()
    assert store.classify_liveness(state, registry, now=now) == "stopped"


def test_record_heartbeat_and_run_read_models_include_status_and_liveness(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    heartbeat = RunHeartbeat(
        run_id=state.run_id,
        updated_at=datetime.now(UTC).isoformat(),
        status=state.status,
        metadata={"agent": "builder", "session": "sess-1"},
    )
    store.record_heartbeat(Path(state.run_dir), heartbeat)

    views = store.list_runs(RegistryConfig())
    assert views[0].status == "in_progress"
    assert views[0].liveness == "active"

    detail = store.get_run_detail(Path(state.run_dir), RegistryConfig())
    assert detail.status == "in_progress"
    assert detail.liveness == "active"
    assert detail.metadata["heartbeat"]["metadata"]["session"] == "sess-1"

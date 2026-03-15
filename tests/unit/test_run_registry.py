from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError

import pytest

from ai_native.config import RegistryConfig
from ai_native.run_registry import build_run_registry_snapshot, publish_run_snapshot
from ai_native.state import StateStore


def test_build_run_registry_snapshot_includes_projection_and_heartbeat(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)
    state.metadata["heartbeat"] = {
        "run_id": state.run_id,
        "updated_at": "2026-03-15T00:00:10+00:00",
        "status": state.status,
        "metadata": {"agent": "ainative"},
    }
    state.slice_states["S001"] = {
        "slice_id": "S001",
        "status": "running",
        "current_stage": "verify",
    }
    state.stage_status["verify"] = {
        "stage": "verify",
        "status": "completed",
        "artifacts": [],
        "notes": [],
    }

    snapshot = build_run_registry_snapshot(state)

    assert snapshot.feature_slug == state.feature_slug
    assert snapshot.last_heartbeat_at == "2026-03-15T00:00:10+00:00"
    assert snapshot.run_projection is not None
    assert snapshot.stage_status["verify"].status == "completed"
    assert snapshot.slice_states["S001"].status == "running"


def test_publish_run_snapshot_surfaces_http_errors(monkeypatch, tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    class _Body:
        def read(self, _size: int = -1) -> bytes:
            return b'{"detail":"unauthorized"}'

        def close(self) -> None:
            return None

    def fail_request(*_args, **_kwargs):
        raise HTTPError(
            url="https://registry.example.com/v1/runs/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=_Body(),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail_request)

    with pytest.raises(RuntimeError, match="Run registry request failed: 401 Unauthorized"):
        publish_run_snapshot(
            RegistryConfig(remote_url="https://registry.example.com", auth_token="secret-token"),
            state,
        )

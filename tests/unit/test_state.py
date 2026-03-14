from __future__ import annotations

from pathlib import Path

from ai_native.config import RegistryConfig
from ai_native.state import StateStore


def test_state_store_creates_and_updates_runs(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    assert Path(state.run_dir).exists()
    assert (Path(state.run_dir) / "spec.md").exists()
    assert Path(state.workspace_root) == workspace_root.resolve()

    store.update_stage(state, stage="intake", status="completed")
    reloaded = store.load(Path(state.run_dir))

    assert reloaded.stage_status["intake"].status == "completed"
    assert reloaded.status == "in_progress"


def test_state_store_emits_warning_when_run_registry_publish_fails(monkeypatch, tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    warnings: list[str] = []

    def fail_publish(*_args, **_kwargs) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("ai_native.state.publish_run_snapshot", fail_publish)
    store = StateStore(
        tmp_path / "artifacts",
        registry=RegistryConfig(remote_url="https://registry.example.com", auth_token="secret-token"),
        emit_warning=warnings.append,
    )

    state = store.create_run(spec, workspace_root)

    assert state.run_id
    assert warnings == [
        f"[ainative] registry: warning - failed to publish run snapshot for {state.run_id}: registry unavailable"
    ]

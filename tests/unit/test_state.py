from __future__ import annotations

from pathlib import Path

from ai_native.config import RegistryConfig
from ai_native.models import SliceExecutionState
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


def test_state_store_failed_stage_update_preserves_existing_artifacts(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)
    artifact = Path(state.run_dir) / "pr" / "S001-review.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# Review\n", encoding="utf-8")

    store.update_stage(state, stage="pr", status="pending", artifacts=[artifact])
    store.update_stage(state, stage="pr", status="failed", notes=["blocked"])

    reloaded = store.load(Path(state.run_dir))
    assert reloaded.stage_status["pr"].status == "failed"
    assert reloaded.stage_status["pr"].artifacts == [str(artifact)]


def test_state_store_successful_rerun_clears_stale_failed_run_status(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    store.update_stage(state, stage="pr", status="failed", notes=["old failure"])
    store.update_stage(state, stage="pr", status="completed")

    reloaded = store.load(Path(state.run_dir))
    assert reloaded.stage_status["pr"].status == "completed"
    assert reloaded.status == "in_progress"


def test_state_store_successful_rerun_keeps_run_failed_when_slice_failed(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)
    state.slice_states["S001"] = SliceExecutionState(
        slice_id="S001", status="failed", current_stage="pr"
    )
    store.save(state)

    store.update_stage(state, stage="pr", status="failed", notes=["old failure"])
    store.update_stage(state, stage="pr", status="completed")

    reloaded = store.load(Path(state.run_dir))
    assert reloaded.stage_status["pr"].status == "completed"
    assert reloaded.status == "failed"


def test_state_store_persists_normalized_reference_manifest_and_spec_body(tmp_path: Path) -> None:
    reference_path = tmp_path / "reference.png"
    reference_path.write_text("png", encoding="utf-8")
    spec = tmp_path / "feature.md"
    spec.write_text(
        """
---
ainative:
  workflow_profile: reference_driven_web
  references:
    - id: hero
      label: Hero reference
      kind: image
      path: reference.png
      route: /
      viewport:
        width: 1440
        height: 900
        label: desktop
  preview:
    url: http://127.0.0.1:3000
---
# Feature

Build the page.
""".lstrip(),
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)
    run_dir = Path(state.run_dir)

    assert (run_dir / "spec.md").read_text(encoding="utf-8") == "# Feature\n\nBuild the page.\n"
    manifest = (run_dir / "reference-manifest.json").read_text(encoding="utf-8")
    assert "reference_driven_web" in manifest
    assert str(reference_path.resolve()) in manifest


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

from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState, SliceDefinition, SliceExecutionState, SlicePlan, StageSnapshot
from ai_native.run_projection import PROJECTION_SCHEMA_VERSION, build_run_projection
from ai_native.state import StateStore
from ai_native.utils import utc_now, write_json


def _base_state(tmp_path: Path) -> RunState:
    now = utc_now()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return RunState(
        run_id="run-1",
        feature_slug="feature",
        spec_path=str(tmp_path / "spec.md"),
        workspace_root=str(tmp_path),
        spec_hash="abc",
        run_dir=str(run_dir),
        created_at=now,
        updated_at=now,
    )


def test_build_run_projection_classifies_ready_and_blocked_slices(tmp_path: Path) -> None:
    state = _base_state(tmp_path)
    state.stage_status["intake"] = StageSnapshot(stage="intake", status="completed")
    state.stage_status["recon"] = StageSnapshot(stage="recon", status="completed")
    state.stage_status["plan"] = StageSnapshot(stage="plan", status="completed")
    state.stage_status["architecture"] = StageSnapshot(stage="architecture", status="completed")
    state.stage_status["prd"] = StageSnapshot(stage="prd", status="completed")
    state.stage_status["slice"] = StageSnapshot(stage="slice", status="completed")

    state.slice_states = {
        "a": SliceExecutionState(slice_id="a", status="running", current_stage="verify"),
        "b": SliceExecutionState(slice_id="b", status="pending"),
    }
    plan = SlicePlan(
        title="t",
        summary="s",
        slices=[
            SliceDefinition(id="a", name="A", goal="A"),
            SliceDefinition(id="b", name="B", goal="B", dependencies=["a"]),
        ],
    )

    projection = build_run_projection(state, plan)

    assert projection.schema_version == PROJECTION_SCHEMA_VERSION
    assert "a:loop" in projection.completed_steps
    assert "a:verify" in projection.in_progress_steps
    assert any(step.step == "b:loop" and "dependency a" in step.reason for step in projection.blocked_steps)


def test_state_store_persists_projection_in_state_json(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec, workspace_root)

    slice_dir = Path(state.run_dir) / "slice"
    slice_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        slice_dir / "slices.json",
        {
            "title": "x",
            "summary": "y",
            "slices": [{"id": "s1", "name": "S1", "goal": "goal", "dependencies": []}],
        },
    )

    store.update_stage(state, stage="intake", status="completed")
    reloaded = store.load(Path(state.run_dir))

    assert reloaded.run_projection is not None
    assert reloaded.run_projection.schema_version == PROJECTION_SCHEMA_VERSION
    assert "recon" in reloaded.run_projection.next_executable_steps


def test_build_run_projection_retries_failed_slice_from_failed_stage(tmp_path: Path) -> None:
    state = _base_state(tmp_path)
    state.stage_status["intake"] = StageSnapshot(stage="intake", status="completed")
    state.stage_status["recon"] = StageSnapshot(stage="recon", status="completed")
    state.stage_status["plan"] = StageSnapshot(stage="plan", status="completed")
    state.stage_status["architecture"] = StageSnapshot(stage="architecture", status="completed")
    state.stage_status["prd"] = StageSnapshot(stage="prd", status="completed")
    state.stage_status["slice"] = StageSnapshot(stage="slice", status="completed")

    plan = SlicePlan(
        title="t",
        summary="s",
        slices=[SliceDefinition(id="a", name="A", goal="A")],
    )
    state.slice_states = {
        "a": SliceExecutionState(
            slice_id="a",
            status="failed",
            current_stage="commit",
            block_reason="commit command failed",
        )
    }

    projection = build_run_projection(state, plan)

    assert "a:loop" in projection.completed_steps
    assert "a:verify" in projection.completed_steps
    assert "a:commit" in projection.next_executable_steps
    assert "a:loop" not in projection.next_executable_steps

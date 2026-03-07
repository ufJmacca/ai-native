from __future__ import annotations

from pathlib import Path

from ai_native.state import StateStore


def test_state_store_creates_and_updates_runs(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")

    store = StateStore(tmp_path / "artifacts")
    state = store.create_run(spec)

    assert Path(state.run_dir).exists()
    assert (Path(state.run_dir) / "spec.md").exists()

    store.update_stage(state, stage="intake", status="completed")
    reloaded = store.load(Path(state.run_dir))

    assert reloaded.stage_status["intake"].status == "completed"
    assert reloaded.status == "in_progress"


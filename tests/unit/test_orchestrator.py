from __future__ import annotations

from pathlib import Path

from ai_native.orchestrator import WorkflowOrchestrator


def test_prepare_state_reuses_latest_matching_run(app_config, tmp_spec: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    first = orchestrator.prepare_state(tmp_spec)
    second = orchestrator.prepare_state(tmp_spec)

    assert first.run_dir == second.run_dir
    assert Path(first.workspace_root) == app_config.repo_root


def test_prepare_state_creates_new_run_when_spec_changes(app_config, tmp_spec: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    first = orchestrator.prepare_state(tmp_spec)
    tmp_spec.write_text("# Sample Spec\n\nChanged.\n", encoding="utf-8")
    second = orchestrator.prepare_state(tmp_spec)

    assert first.run_dir != second.run_dir


def test_prepare_state_uses_workspace_root_to_partition_runs(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    first_workspace = tmp_path / "workspace-a"
    second_workspace = tmp_path / "workspace-b"
    first_workspace.mkdir()
    second_workspace.mkdir()

    first = orchestrator.prepare_state(tmp_spec, workspace_root=first_workspace)
    second = orchestrator.prepare_state(tmp_spec, workspace_root=second_workspace)

    assert first.run_dir != second.run_dir
    assert Path(first.workspace_root) == first_workspace.resolve()
    assert Path(second.workspace_root) == second_workspace.resolve()


def test_run_until_emits_stage_progress(app_config, tmp_spec: Path) -> None:
    events: list[str] = []
    orchestrator = WorkflowOrchestrator(app_config, progress=events.append)

    def fake_stage(context, state):  # type: ignore[no-untyped-def]
        return []

    orchestrator.stage_handlers["intake"] = fake_stage
    orchestrator.stage_handlers["recon"] = fake_stage
    orchestrator.stage_handlers["plan"] = fake_stage

    orchestrator.run_until(tmp_spec, "plan")

    assert any(event.startswith("[ainative] run-dir: ") for event in events)
    assert any(event.startswith("[ainative] workspace-dir: ") for event in events)
    assert "[ainative] intake: started" in events
    assert "[ainative] recon: completed" in events
    assert "[ainative] plan: completed" in events

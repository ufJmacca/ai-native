from __future__ import annotations

from pathlib import Path

from ai_native.orchestrator import WorkflowOrchestrator


def test_prepare_state_reuses_latest_matching_run(app_config, tmp_spec: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    first = orchestrator.prepare_state(tmp_spec)
    second = orchestrator.prepare_state(tmp_spec)

    assert first.run_dir == second.run_dir


def test_prepare_state_creates_new_run_when_spec_changes(app_config, tmp_spec: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    first = orchestrator.prepare_state(tmp_spec)
    tmp_spec.write_text("# Sample Spec\n\nChanged.\n", encoding="utf-8")
    second = orchestrator.prepare_state(tmp_spec)

    assert first.run_dir != second.run_dir


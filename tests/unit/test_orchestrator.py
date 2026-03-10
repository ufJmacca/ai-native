from __future__ import annotations

from pathlib import Path

from ai_native.models import SlicePlan
from ai_native.utils import write_json

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


def test_prepare_state_defaults_runs_to_target_repo_and_bootstraps_git(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.artifacts_dir = Path(".ai-native/runs")
    orchestrator = WorkflowOrchestrator(app_config)
    workspace_root = tmp_path / "target-repo"

    state = orchestrator.prepare_state(tmp_spec, workspace_root=workspace_root)

    assert Path(state.run_dir).parent == (workspace_root / ".ai-native" / "runs").resolve()
    assert (workspace_root / ".git").exists()


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


def test_run_all_processes_slices_sequentially(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    orchestrator = WorkflowOrchestrator(app_config)
    events: list[tuple[str, str | None]] = []
    orchestrator.progress = lambda _message: None

    def fake_stage(context, state):  # type: ignore[no-untyped-def]
        return []

    def fake_slice(context, state):  # type: ignore[no-untyped-def]
        stage_dir = context.state_store.stage_dir(state, "slice")
        write_json(
            stage_dir / "slices.json",
            SlicePlan(
                title="Slices",
                summary="Summary",
                slices=[
                    {
                        "id": "S001",
                        "name": "First slice",
                        "goal": "Ship slice one.",
                        "acceptance_criteria": ["One"],
                        "file_impact": ["a.ts"],
                        "test_plan": ["test one"],
                        "dependencies": [],
                    },
                    {
                        "id": "S002",
                        "name": "Second slice",
                        "goal": "Ship slice two.",
                        "acceptance_criteria": ["Two"],
                        "file_impact": ["b.ts"],
                        "test_plan": ["test two"],
                        "dependencies": ["S001"],
                    },
                ],
            ).model_dump(mode="json"),
        )
        return [stage_dir / "slices.json"]

    def fake_loop(context, state):  # type: ignore[no-untyped-def]
        events.append(("loop", state.active_slice))
        return [Path(state.run_dir) / "slices" / str(state.active_slice) / "builder-summary.md"]

    def fake_verify(context, state):  # type: ignore[no-untyped-def]
        events.append(("verify", state.active_slice))
        return [Path(state.run_dir) / "verify" / f"{state.active_slice}.md"]

    def fake_commit(context, state):  # type: ignore[no-untyped-def]
        events.append(("commit", state.active_slice))
        commit_path = context.state_store.stage_dir(state, "commit") / f"{state.active_slice}.txt"
        commit_path.write_text("sha\n", encoding="utf-8")
        return [commit_path]

    def fake_pr(context, state, dry_run=False):  # type: ignore[no-untyped-def]
        events.append(("pr", state.active_slice))
        return []

    orchestrator.stage_handlers.update(
        {
            "intake": fake_stage,
            "recon": fake_stage,
            "plan": fake_stage,
            "architecture": fake_stage,
            "prd": fake_stage,
            "slice": fake_slice,
            "loop": fake_loop,
            "verify": fake_verify,
            "commit": fake_commit,
            "pr": fake_pr,
        }
    )

    state = orchestrator.run_all(tmp_spec, workspace_root=workspace_root, dry_run_pr=True)

    assert events == [
        ("loop", "S001"),
        ("verify", "S001"),
        ("commit", "S001"),
        ("loop", "S002"),
        ("verify", "S002"),
        ("commit", "S002"),
        ("pr", "S002"),
    ]
    assert state.active_slice == "S002"

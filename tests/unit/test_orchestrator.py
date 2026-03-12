from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.config import TelemetryDestination
from ai_native.models import SlicePlan
from ai_native.utils import write_json

from ai_native.orchestrator import WorkflowOrchestrator
from ai_native.stages.common import StageError


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


def test_prepare_state_rejects_nested_workspace_inside_existing_repo(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator(app_config)
    parent_repo = tmp_path / "parent-repo"
    nested_workspace = parent_repo / "app"
    nested_workspace.mkdir(parents=True)

    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=parent_repo, check=True, capture_output=True, text=True)

    with pytest.raises(StageError, match="nested inside existing git repository"):
        orchestrator.prepare_state(tmp_spec, workspace_root=nested_workspace)


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


def test_run_all_continues_with_committed_dependencies_when_policy_assumes_merge(app_config, tmp_spec: Path, tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    orchestrator = WorkflowOrchestrator(app_config)
    events: list[tuple[str, str | None]] = []
    merged_dependencies: list[tuple[str, str]] = []
    orchestrator.progress = lambda _message: None

    def fake_ensure_worktree(_repo_root, _branch_name, worktree_path, _base_ref):  # type: ignore[no-untyped-def]
        worktree_path.mkdir(parents=True, exist_ok=True)
        return worktree_path.resolve()

    def fake_merge_commit(repo_root, commit_sha):  # type: ignore[no-untyped-def]
        merged_dependencies.append((Path(repo_root).name, commit_sha))

    monkeypatch.setattr("ai_native.orchestrator.ensure_worktree", fake_ensure_worktree)
    monkeypatch.setattr("ai_native.orchestrator.merge_commit", fake_merge_commit)

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
        commit_path.write_text(f"sha-{state.active_slice}\n", encoding="utf-8")
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
        ("pr", "S001"),
        ("loop", "S002"),
        ("verify", "S002"),
        ("commit", "S002"),
        ("pr", "S002"),
    ]
    assert merged_dependencies == [("S002", "sha-S001")]
    assert state.slice_states["S001"].status == "pr_opened"
    assert state.slice_states["S002"].status == "pr_opened"
    assert state.status == "completed"


def test_run_all_blocks_dependent_slices_until_prerequisites_merge_to_base_when_configured(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.dependency_policy = "wait_for_base_merge"
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
        ("pr", "S001"),
    ]
    assert state.slice_states["S001"].status == "pr_opened"
    assert state.slice_states["S002"].status == "blocked"
    assert state.status == "in_progress"


def test_context_resolves_active_telemetry_profile(app_config, tmp_spec: Path) -> None:
    app_config.telemetry.enabled = True
    app_config.telemetry.profile = "default"
    app_config.telemetry.destinations = {
        "default": TelemetryDestination(url="https://telemetry.example.com/events")
    }
    orchestrator = WorkflowOrchestrator(app_config)
    state = orchestrator.prepare_state(tmp_spec)

    orchestrator._context(tmp_spec.resolve(), state)

    assert orchestrator.telemetry_destination is not None
    profile_name, destination = orchestrator.telemetry_destination
    assert profile_name == "default"
    assert destination.url == "https://telemetry.example.com/events"


def test_context_raises_when_active_telemetry_profile_is_missing(app_config, tmp_spec: Path) -> None:
    app_config.telemetry.enabled = True
    app_config.telemetry.profile = "missing"
    app_config.telemetry.destinations = {}
    orchestrator = WorkflowOrchestrator(app_config)
    state = orchestrator.prepare_state(tmp_spec)

    with pytest.raises(StageError, match="Telemetry profile 'missing' is not configured"):
        orchestrator._context(tmp_spec.resolve(), state)

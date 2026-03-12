from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages.common import ExecutionContext
from ai_native.stages.git_pr import commit_run, create_prs
from ai_native.utils import utc_now, write_json
from tests.helpers import FakeWorkflowAdapter


def test_create_prs_dry_run_writes_body_and_review(app_config, tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "run-1"
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (run_dir / "prd").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "Summary",
            "slices": [
                {
                    "id": "S001",
                    "name": "Create todos",
                    "goal": "Ship the first slice.",
                    "acceptance_criteria": ["Todo can be created"],
                    "file_impact": ["app.py"],
                    "test_plan": ["Test create endpoint"],
                    "dependencies": [],
                }
            ],
        },
    )
    write_json(
        run_dir / "prd" / "prd.json",
        {
            "title": "PRD",
            "user_value": "Users can create todos",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": [],
            "out_of_scope": [],
        },
    )
    state = RunState(
        run_id="run-1",
        feature_slug="todo",
        spec_path=str(tmp_path / "spec.md"),
        workspace_root=str(Path(__file__).resolve().parents[2]),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S001",
    )
    prompt_library = PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts")
    adapter = FakeWorkflowAdapter()
    context = ExecutionContext(
        config=app_config,
        prompt_library=prompt_library,
        state_store=StateStore(tmp_path / "artifacts"),
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_path / "spec.md",
        run_dir=run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
    )

    artifacts = create_prs(context, state, dry_run=True)

    assert any(path.name == "S001-body.md" for path in artifacts)
    assert any(path.name == "S001-review.md" for path in artifacts)


def test_commit_run_uses_slice_specific_commit_message(monkeypatch, app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    run_dir = tmp_path / "artifacts" / "run-1"
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (slice_dir / "builder-summary.md").write_text("# Builder Summary\nImplemented the first slice.\n", encoding="utf-8")
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "Summary",
            "slices": [
                {
                    "id": "S001",
                    "name": "Create todos",
                    "goal": "Ship the first slice.",
                    "acceptance_criteria": ["Todo can be created"],
                    "file_impact": ["app.py"],
                    "test_plan": ["Test create endpoint"],
                    "dependencies": [],
                }
            ],
        },
    )
    state = RunState(
        run_id="run-1",
        feature_slug="todo",
        spec_path=str(tmp_path / "spec.md"),
        workspace_root=str(workspace_root),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S001",
    )
    prompt_library = PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts")
    adapter = FakeWorkflowAdapter()
    context = ExecutionContext(
        config=app_config,
        prompt_library=prompt_library,
        state_store=StateStore(tmp_path / "artifacts"),
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=workspace_root,
        spec_path=tmp_path / "spec.md",
        run_dir=run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr("ai_native.stages.git_pr.has_changes", lambda cwd: True)
    monkeypatch.setattr("ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: captured.setdefault("branch", branch))

    def fake_commit_all(cwd, subject, body):  # type: ignore[no-untyped-def]
        captured["subject"] = subject
        captured["body"] = body
        return "abc123"

    monkeypatch.setattr("ai_native.stages.git_pr.commit_all", fake_commit_all)

    artifacts = commit_run(context, state)

    commit_record = (run_dir / "commit" / "S001.txt").read_text(encoding="utf-8")
    assert any(path.name == "S001.txt" for path in artifacts)
    assert captured["branch"] == "codex/todo-S001"
    assert captured["subject"] == "feat: Create todos [S001]"
    assert "Goal: Ship the first slice." in str(captured["body"])
    assert "Acceptance Criteria:" in str(captured["body"])
    assert "Implemented the first slice." in commit_record


def test_create_prs_targets_deepest_dependency_branch(monkeypatch, app_config, tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "run-1"
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (run_dir / "prd").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "Summary",
            "slices": [
                {
                    "id": "S001",
                    "name": "First slice",
                    "goal": "Ship slice one.",
                    "acceptance_criteria": ["One"],
                    "file_impact": ["a.py"],
                    "test_plan": ["test one"],
                    "dependencies": [],
                },
                {
                    "id": "S002",
                    "name": "Second slice",
                    "goal": "Ship slice two.",
                    "acceptance_criteria": ["Two"],
                    "file_impact": ["b.py"],
                    "test_plan": ["test two"],
                    "dependencies": ["S001"],
                },
                {
                    "id": "S003",
                    "name": "Third slice",
                    "goal": "Ship slice three.",
                    "acceptance_criteria": ["Three"],
                    "file_impact": ["c.py"],
                    "test_plan": ["test three"],
                    "dependencies": ["S001", "S002"],
                },
            ],
        },
    )
    write_json(
        run_dir / "prd" / "prd.json",
        {
            "title": "PRD",
            "user_value": "Users can create todos",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": [],
            "out_of_scope": [],
        },
    )
    state = RunState(
        run_id="run-1",
        feature_slug="todo",
        spec_path=str(tmp_path / "spec.md"),
        workspace_root=str(Path(__file__).resolve().parents[2]),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S003",
        slice_states={
            "S001": {"slice_id": "S001", "branch_name": "codex/todo-S001"},
            "S002": {"slice_id": "S002", "branch_name": "codex/todo-S002"},
        },
    )
    prompt_library = PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts")
    adapter = FakeWorkflowAdapter()
    context = ExecutionContext(
        config=app_config,
        prompt_library=prompt_library,
        state_store=StateStore(tmp_path / "artifacts"),
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_path / "spec.md",
        run_dir=run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr("ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None)
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        captured["title"] = title
        captured["base_branch"] = base_branch
        return "https://example.invalid/pr/1"

    monkeypatch.setattr("ai_native.stages.git_pr.create_pull_request", fake_create_pull_request)

    artifacts = create_prs(context, state, dry_run=False)

    assert any(path.name == "S003-url.txt" for path in artifacts)
    assert captured["title"] == "S003: Third slice"
    assert captured["base_branch"] == "codex/todo-S002"


def test_create_prs_falls_back_to_base_branch_for_incomparable_dependencies(monkeypatch, app_config, tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "run-1"
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (run_dir / "prd").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "Summary",
            "slices": [
                {
                    "id": "S001",
                    "name": "First slice",
                    "goal": "Ship slice one.",
                    "acceptance_criteria": ["One"],
                    "file_impact": ["a.py"],
                    "test_plan": ["test one"],
                    "dependencies": [],
                },
                {
                    "id": "S002",
                    "name": "Second slice",
                    "goal": "Ship slice two.",
                    "acceptance_criteria": ["Two"],
                    "file_impact": ["b.py"],
                    "test_plan": ["test two"],
                    "dependencies": ["S001"],
                },
                {
                    "id": "S003",
                    "name": "Third slice",
                    "goal": "Ship slice three.",
                    "acceptance_criteria": ["Three"],
                    "file_impact": ["c.py"],
                    "test_plan": ["test three"],
                    "dependencies": ["S001"],
                },
                {
                    "id": "S004",
                    "name": "Fourth slice",
                    "goal": "Ship slice four.",
                    "acceptance_criteria": ["Four"],
                    "file_impact": ["d.py"],
                    "test_plan": ["test four"],
                    "dependencies": ["S002", "S003"],
                },
            ],
        },
    )
    write_json(
        run_dir / "prd" / "prd.json",
        {
            "title": "PRD",
            "user_value": "Users can create todos",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": [],
            "out_of_scope": [],
        },
    )
    state = RunState(
        run_id="run-1",
        feature_slug="todo",
        spec_path=str(tmp_path / "spec.md"),
        workspace_root=str(Path(__file__).resolve().parents[2]),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S004",
        slice_states={
            "S002": {"slice_id": "S002", "branch_name": "codex/todo-S002"},
            "S003": {"slice_id": "S003", "branch_name": "codex/todo-S003"},
        },
    )
    prompt_library = PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts")
    adapter = FakeWorkflowAdapter()
    context = ExecutionContext(
        config=app_config,
        prompt_library=prompt_library,
        state_store=StateStore(tmp_path / "artifacts"),
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_path / "spec.md",
        run_dir=run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr("ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None)
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        captured["base_branch"] = base_branch
        return "https://example.invalid/pr/2"

    monkeypatch.setattr("ai_native.stages.git_pr.create_pull_request", fake_create_pull_request)

    artifacts = create_prs(context, state, dry_run=False)

    assert any(path.name == "S004-url.txt" for path in artifacts)
    assert captured["base_branch"] == app_config.workspace.base_branch

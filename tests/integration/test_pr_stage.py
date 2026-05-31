from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.adapters.base import AgentResult
from ai_native.models import RunState
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.stages.git_pr import commit_run, create_prs
from ai_native.utils import read_json, utc_now, write_json
from tests.helpers import FakeWorkflowAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]


class SequencedReviewAdapter:
    def __init__(self, reviews: list[str]) -> None:
        self.reviews = list(reviews)
        self.calls: list[dict[str, object]] = []

    def review(
        self, cwd: Path, prompt: str, base_branch: str | None = None
    ) -> AgentResult:
        self.calls.append({"cwd": cwd, "prompt": prompt, "base_branch": base_branch})
        if not self.reviews:
            raise AssertionError("Unexpected PR review call")
        return AgentResult(text=self.reviews.pop(0))


class SequencedCriticAdapter:
    def __init__(self, reports: list[dict[str, object]]) -> None:
        self.reports = list(reports)
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "schema_path": schema_path,
                "image_paths": image_paths or [],
            }
        )
        if not self.reports:
            raise AssertionError("Unexpected critic call")
        payload = self.reports.pop(0)
        return AgentResult(text="", json_data=payload)

    def supports_image_inputs(self) -> bool:
        return False


class RecordingBuilderAdapter:
    def __init__(self, text: str = "# PR Repair Summary\nFixed the blocker.") -> None:
        self.text = text
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "schema_path": schema_path,
                "image_paths": image_paths or [],
            }
        )
        return AgentResult(text=self.text)

    def supports_image_inputs(self) -> bool:
        return False


class FailingAdapter:
    def run(self, *_args, **_kwargs) -> AgentResult:  # type: ignore[no-untyped-def]
        raise AssertionError("Adapter should not be called")

    def supports_image_inputs(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def clean_pr_worktree(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "ai_native.stages.git_pr.worktree_is_clean", lambda cwd: True
    )
    monkeypatch.setattr("ai_native.stages.git_pr.status_porcelain", lambda cwd: "")


def _approved_report(summary: str = "No blocking PR findings.") -> dict[str, object]:
    return {
        "verdict": "approved",
        "summary": summary,
        "findings": [],
        "required_changes": [],
    }


def _changes_required_report(
    summary: str = "One blocking PR finding.",
) -> dict[str, object]:
    return {
        "verdict": "changes_required",
        "summary": summary,
        "findings": ["The PR review found an unhandled edge case."],
        "required_changes": ["Handle the missing edge case and cover it with a test."],
    }


def _create_single_slice_pr_context(
    app_config,
    tmp_path: Path,
    *,
    builder,
    critic,
    verifier,
    pr_reviewer,
    repo_root: Path | None = None,
) -> tuple[ExecutionContext, RunState, Path]:
    workspace_root = repo_root or tmp_path / "target-repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "artifacts" / "run-1"
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (run_dir / "prd").mkdir(parents=True, exist_ok=True)
    (run_dir / "slices" / "S001").mkdir(parents=True, exist_ok=True)
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Spec\n", encoding="utf-8")
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
        spec_path=str(spec_path),
        workspace_root=str(workspace_root),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S001",
        slice_states={
            "S001": {
                "slice_id": "S001",
                "branch_name": "codex/todo-S001",
                "commit_sha": "oldsha",
            }
        },
    )
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(REPO_ROOT / "ai_native" / "prompts"),
        state_store=StateStore(tmp_path / "artifacts"),
        template_root=REPO_ROOT / "ai_native",
        repo_root=workspace_root,
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=verifier,
        pr_reviewer=pr_reviewer,
    )
    return context, state, run_dir


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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
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


def test_create_prs_approved_review_opens_pr_and_writes_structured_artifacts(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    reviewer = SequencedReviewAdapter(["# Review\nNo blocking issues found."])
    critic = SequencedCriticAdapter([_approved_report()])
    builder = RecordingBuilderAdapter()
    context, state, run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=reviewer,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch",
        lambda cwd, branch: captured.setdefault("branch", branch),
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.push_branch",
        lambda cwd, branch: captured.setdefault("pushed", branch),
    )

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        captured["title"] = title
        captured["body_file"] = body_file
        captured["base_branch"] = base_branch
        return "https://example.invalid/pr/approved"

    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request", fake_create_pull_request
    )

    artifacts = create_prs(context, state, dry_run=False)

    artifact_names = {path.name for path in artifacts}
    assert "S001-review.md" in artifact_names
    assert "S001-review-attempt-1.md" in artifact_names
    assert "S001-review-report.md" in artifact_names
    assert "S001-review-report.json" in artifact_names
    assert "S001-review-report-attempt-1.json" in artifact_names
    assert "S001-url.txt" in artifact_names
    assert (
        read_json(run_dir / "pr" / "S001-review-report.json")["verdict"] == "approved"
    )
    assert (run_dir / "pr" / "S001-url.txt").read_text(
        encoding="utf-8"
    ).strip() == "https://example.invalid/pr/approved"
    assert captured["pushed"] == "codex/todo-S001"
    assert captured["base_branch"] == app_config.workspace.base_branch
    assert len(reviewer.calls) == 1
    assert len(critic.calls) == 1
    assert builder.calls == []


def test_create_prs_repairs_actionable_review_and_amends_slice_commit(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    reviewer = SequencedReviewAdapter(
        [
            "# Review\nThe edge case is unhandled.",
            "# Review\nThe repaired diff handles the edge case.",
        ]
    )
    critic = SequencedCriticAdapter(
        [_changes_required_report(), _approved_report("Repair looks good.")]
    )
    builder = RecordingBuilderAdapter(
        "# PR Repair Summary\nHandled the missing edge case."
    )
    context, state, run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=reviewer,
    )
    commit_dir = run_dir / "commit"
    commit_dir.mkdir(parents=True, exist_ok=True)
    (commit_dir / "S001.txt").write_text(
        "feat: Create todos [S001]\n\nBody\n\noldsha\n", encoding="utf-8"
    )
    previous_verify_dir = run_dir / "verify"
    previous_verify_dir.mkdir(parents=True, exist_ok=True)
    passed_verification = {
        "verdict": "passed",
        "summary": "Previously passed before PR repair.",
        "acceptance_checks": ["Old check"],
        "evidence": ["old.log"],
        "gaps": [],
    }
    write_json(previous_verify_dir / "S001.json", passed_verification)
    (previous_verify_dir / "S001.md").write_text(
        "# Verification\nold pass\n", encoding="utf-8"
    )
    write_json(previous_verify_dir / "S001-attempt-1.json", passed_verification)
    (previous_verify_dir / "S001-attempt-1.md").write_text(
        "# Verification\nold pass\n", encoding="utf-8"
    )
    visual_attempt_dir = previous_verify_dir / "visual" / "S001" / "attempt-1"
    visual_attempt_dir.mkdir(parents=True, exist_ok=True)
    (visual_attempt_dir / "capture.png").write_text(
        "old visual capture\n", encoding="utf-8"
    )
    verify_calls: list[str] = []
    branch_calls: list[str] = []
    pushed: list[str] = []
    prs: list[str] = []
    monkeypatch.setattr("ai_native.stages.git_pr.has_changes", lambda cwd: True)
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch",
        lambda cwd, branch: branch_calls.append(branch),
    )
    monkeypatch.setattr("ai_native.stages.git_pr.amend_all", lambda cwd: "newsha")
    monkeypatch.setattr(
        "ai_native.stages.git_pr.push_branch",
        lambda cwd, branch: pushed.append(branch),
    )

    def fake_run_verify(context_arg, state_arg):  # type: ignore[no-untyped-def]
        verify_calls.append(state_arg.active_slice)
        assert not (previous_verify_dir / "S001.json").exists()
        assert not (previous_verify_dir / "S001-attempt-1.json").exists()
        assert not (previous_verify_dir / "visual" / "S001").exists()
        verify_path = run_dir / "verify" / "S001.md"
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text("# Verification\npassed\n", encoding="utf-8")
        return [verify_path]

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        prs.append(title)
        return "https://example.invalid/pr/repaired"

    monkeypatch.setattr("ai_native.stages.git_pr.run_verify", fake_run_verify)
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request", fake_create_pull_request
    )

    artifacts = create_prs(context, state, dry_run=False)

    artifact_names = {path.name for path in artifacts}
    assert "S001-review-report-attempt-1.json" in artifact_names
    assert "S001-review-report-attempt-2.json" in artifact_names
    assert "S001-repair-summary-attempt-1.md" in artifact_names
    assert "S001.md" in artifact_names
    assert (
        read_json(run_dir / "pr" / "S001-review-report-attempt-1.json")["verdict"]
        == "changes_required"
    )
    assert (
        read_json(run_dir / "pr" / "S001-review-report-attempt-2.json")["verdict"]
        == "approved"
    )
    assert (
        (commit_dir / "S001.txt").read_text(encoding="utf-8").strip().endswith("newsha")
    )
    archive_dir = run_dir / "pr" / "S001-verification-before-pr-repair-attempt-1"
    assert (archive_dir / "S001.json").exists()
    assert (archive_dir / "S001-attempt-1.json").exists()
    assert (archive_dir / "S001" / "attempt-1" / "capture.png").exists()
    assert state.slice_states["S001"].commit_sha == "newsha"
    assert len(builder.calls) == 1
    assert verify_calls == ["S001"]
    assert len(reviewer.calls) == 2
    assert len(critic.calls) == 2
    assert branch_calls == ["codex/todo-S001", "codex/todo-S001"]
    assert pushed == ["codex/todo-S001"]
    assert prs == ["S001: Create todos"]


def test_create_prs_fails_when_repair_amend_leaves_dirty_worktree(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    reviewer = SequencedReviewAdapter(["# Review\nThe edge case is unhandled."])
    critic = SequencedCriticAdapter([_changes_required_report()])
    builder = RecordingBuilderAdapter(
        "# PR Repair Summary\nAttempted the missing edge case fix."
    )
    context, state, _run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=reviewer,
    )
    clean_checks = iter([True, False])
    pushed: list[str] = []
    opened: list[str] = []

    monkeypatch.setattr(
        "ai_native.stages.git_pr.worktree_is_clean",
        lambda cwd: next(clean_checks),
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.status_porcelain",
        lambda cwd: " M tests/test_repo_hygiene.py",
    )
    monkeypatch.setattr("ai_native.stages.git_pr.has_changes", lambda cwd: True)
    monkeypatch.setattr("ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None)
    monkeypatch.setattr("ai_native.stages.git_pr.amend_all", lambda cwd: "newsha")
    monkeypatch.setattr("ai_native.stages.git_pr.run_verify", lambda context_arg, state_arg: [])
    monkeypatch.setattr(
        "ai_native.stages.git_pr.push_branch",
        lambda cwd, branch: pushed.append(branch),
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request",
        lambda cwd, title, body_file, draft, base_branch=None: opened.append(title)
        or "https://example.invalid/pr",
    )

    with pytest.raises(StageError, match="after PR repair amend"):
        create_prs(context, state, dry_run=False)

    assert len(builder.calls) == 1
    assert len(reviewer.calls) == 1
    assert pushed == []
    assert opened == []


def test_create_prs_exhausted_review_attempts_do_not_push_or_open(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    app_config.workspace.pr_review_max_attempts = 1
    reviewer = SequencedReviewAdapter(["# Review\nThe edge case is unhandled."])
    critic = SequencedCriticAdapter([_changes_required_report()])
    builder = RecordingBuilderAdapter()
    context, state, run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=reviewer,
    )
    pushed: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(
        "ai_native.stages.git_pr.push_branch",
        lambda cwd, branch: pushed.append(branch),
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request",
        lambda cwd, title, body_file, draft, base_branch=None: opened.append(title)
        or "https://example.invalid/pr",
    )

    with pytest.raises(
        StageError, match="PR review failed for slice S001 after 1 attempts"
    ):
        create_prs(context, state, dry_run=False)

    assert (
        read_json(run_dir / "pr" / "S001-review-report-attempt-1.json")["verdict"]
        == "changes_required"
    )
    assert builder.calls == []
    assert pushed == []
    assert opened == []


def test_create_prs_uses_latest_report_for_pr_blocker_ledger(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    reviewer = SequencedReviewAdapter(["# Review\nNo blocking issues found."])
    critic = SequencedCriticAdapter([_approved_report()])
    builder = RecordingBuilderAdapter()
    context, state, run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=reviewer,
    )
    pr_dir = run_dir / "pr"
    pr_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        pr_dir / "S001-review-report-attempt-1.json",
        _changes_required_report("Old blocker."),
    )
    write_json(
        pr_dir / "S001-review-report-attempt-2.json",
        _approved_report("Previously approved."),
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None
    )
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request",
        lambda cwd,
        title,
        body_file,
        draft,
        base_branch=None: "https://example.invalid/pr",
    )

    create_prs(context, state, dry_run=False)

    assert len(critic.calls) == 1
    prompt = str(critic.calls[0]["prompt"])
    assert "Old blocker." in prompt
    assert "No active PR review blockers exist in the latest triage report." in prompt
    assert "S001-review-report-attempt-3.json" in {
        path.name for path in (run_dir / "pr").glob("*.json")
    }


def test_create_prs_can_keep_pr_review_advisory(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    app_config.quality_gates.require_pr_review_approval = False
    reviewer = SequencedReviewAdapter(
        ["# Review\nThis would otherwise be advisory only."]
    )
    context, state, run_dir = _create_single_slice_pr_context(
        app_config,
        tmp_path,
        builder=FailingAdapter(),
        critic=FailingAdapter(),
        verifier=FailingAdapter(),
        pr_reviewer=reviewer,
    )
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None
    )
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request",
        lambda cwd,
        title,
        body_file,
        draft,
        base_branch=None: "https://example.invalid/pr/advisory",
    )

    artifacts = create_prs(context, state, dry_run=False)

    artifact_names = {path.name for path in artifacts}
    assert "S001-review.md" in artifact_names
    assert "S001-url.txt" in artifact_names
    assert not (run_dir / "pr" / "S001-review-report.json").exists()
    assert (run_dir / "pr" / "S001-url.txt").read_text(
        encoding="utf-8"
    ).strip() == "https://example.invalid/pr/advisory"
    assert len(reviewer.calls) == 1


def test_commit_run_uses_slice_specific_commit_message(
    monkeypatch, app_config, tmp_path: Path
) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    run_dir = tmp_path / "artifacts" / "run-1"
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (slice_dir / "builder-summary.md").write_text(
        "# Builder Summary\nImplemented the first slice.\n", encoding="utf-8"
    )
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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
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
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch",
        lambda cwd, branch: captured.setdefault("branch", branch),
    )

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


def test_create_prs_targets_deepest_dependency_branch(
    monkeypatch, app_config, tmp_path: Path
) -> None:
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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
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
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None
    )
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        captured["title"] = title
        captured["base_branch"] = base_branch
        return "https://example.invalid/pr/1"

    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request", fake_create_pull_request
    )

    artifacts = create_prs(context, state, dry_run=False)

    assert any(path.name == "S003-url.txt" for path in artifacts)
    assert captured["title"] == "S003: Third slice"
    assert captured["base_branch"] == "codex/todo-S002"


def test_create_prs_falls_back_to_base_branch_for_incomparable_dependencies(
    monkeypatch, app_config, tmp_path: Path
) -> None:
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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
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
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None
    )
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)

    def fake_create_pull_request(cwd, title, body_file, draft, base_branch=None):  # type: ignore[no-untyped-def]
        captured["base_branch"] = base_branch
        return "https://example.invalid/pr/2"

    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request", fake_create_pull_request
    )

    artifacts = create_prs(context, state, dry_run=False)

    assert any(path.name == "S004-url.txt" for path in artifacts)
    assert captured["base_branch"] == app_config.workspace.base_branch


def test_create_prs_uses_review_adapter_base_branch(
    monkeypatch, app_config, tmp_path: Path
) -> None:
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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
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
    monkeypatch.setattr(
        "ai_native.stages.git_pr.ensure_branch", lambda cwd, branch: None
    )
    monkeypatch.setattr("ai_native.stages.git_pr.push_branch", lambda cwd, branch: None)
    monkeypatch.setattr(
        "ai_native.stages.git_pr.create_pull_request",
        lambda cwd,
        title,
        body_file,
        draft,
        base_branch=None: "https://example.invalid/pr/3",
    )

    create_prs(context, state, dry_run=False)

    review_calls = [call for call in adapter.calls if call["mode"] == "review"]
    assert len(review_calls) == 1
    assert review_calls[0]["cwd"] == Path(__file__).resolve().parents[2]
    assert review_calls[0]["base_branch"] == "codex/todo-S002"


def test_create_prs_falls_back_to_run_with_base_branch_context(
    app_config, tmp_path: Path
) -> None:
    app_config.quality_gates.require_pr_review_approval = False

    class RunOnlyAdapter:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def run(
            self, prompt: str, cwd: Path, schema_path: Path | None = None
        ) -> AgentResult:
            self.prompts.append(prompt)
            return AgentResult(text="# Review\nLooks good.")

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
    prompt_library = PromptLibrary(
        Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
    )
    adapter = RunOnlyAdapter()
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

    create_prs(context, state, dry_run=True)

    assert len(adapter.prompts) == 1
    assert "git base branch `main`" in adapter.prompts[0]

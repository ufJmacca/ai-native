from __future__ import annotations

import json
import re
from pathlib import Path

from ai_native.adapters.base import AgentResult
from ai_native.models import ReviewReport
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext
from ai_native.stages.loop import run as run_loop
from ai_native.state import StateStore
from ai_native.utils import write_json
from tests.helpers import FakeWorkflowAdapter

SLICE_DIR_RE = re.compile(r"Slice artifact directory:\n(?P<path>.+)")


def _extract_slice_dir(prompt: str) -> Path:
    match = SLICE_DIR_RE.search(prompt)
    if not match:
        raise AssertionError("Slice artifact directory not found in prompt.")
    return Path(match.group("path").strip())


class RevisingLoopBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        slice_dir = _extract_slice_dir(prompt)
        slice_dir.mkdir(parents=True, exist_ok=True)
        (slice_dir / "red.log").write_text(f"failing test output attempt {self.calls}\n", encoding="utf-8")
        (slice_dir / "green.log").write_text(f"passing test output attempt {self.calls}\n", encoding="utf-8")
        (slice_dir / "refactor-notes.md").write_text(f"# Refactor Notes\n- attempt {self.calls}\n", encoding="utf-8")
        return AgentResult(text=f"# Builder Summary\nAttempt {self.calls}\n")


class OneRejectingLoopCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = ReviewReport(
                verdict="changes_required",
                summary="Tests need explicit behavioral assertions.",
                findings=["Assertions are too weak."],
                required_changes=["Add assertions that prove behavior"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="Tests are explicit and high signal.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ApprovingLoopCritic:
    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        payload = ReviewReport(
            verdict="approved",
            summary="Tests are explicit and high signal.",
            findings=[],
            required_changes=[],
        ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ExhaustionLoopCritic:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        if self.calls < 3:
            payload = ReviewReport(
                verdict="changes_required",
                summary="Still missing explicit assertions.",
                findings=["Assertions remain weak."],
                required_changes=["Add assertions that prove behavior"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="Tests are explicit and high signal.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def _seed_slice_plan(run_dir: Path) -> None:
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "One slice",
            "slices": [
                {
                    "id": "S001",
                    "name": "Create and list tasks",
                    "goal": "Implement the first slice.",
                    "acceptance_criteria": ["Can create a task", "Can list tasks"],
                    "file_impact": ["app/tasks.py", "tests/test_tasks.py"],
                    "test_plan": ["Write failing tests first"],
                    "dependencies": [],
                }
            ],
        },
    )


def test_loop_stage_revises_after_test_critique(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.loop_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan(run_dir)
    builder = RevisingLoopBuilder()
    critic = OneRejectingLoopCritic()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    artifacts = run_loop(context, state)
    slice_dir = run_dir / "slices" / "S001"

    assert builder.calls == 2
    assert critic.calls == 2
    assert any("critique requested changes" in event for event in progress)
    assert any("Add assertions that prove behavior" in prompt for prompt in builder.prompts)
    assert any("Blocker ledger:" in prompt for prompt in critic.prompts)
    assert (slice_dir / "builder-summary-attempt-2.md").exists()
    assert (slice_dir / "red-attempt-2.log").exists()
    assert (slice_dir / "test-review-attempt-2.json").exists()
    assert (slice_dir / "critique-history.md").exists()
    assert (slice_dir / "blocker-ledger.md").exists()
    assert any(path.name == "test-review-attempt-2.md" for path in artifacts)


def test_loop_stage_resumes_from_latest_attempt(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.loop_max_attempts = 4
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan(run_dir)
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (slice_dir / "builder-summary-attempt-2.md").write_text("# Builder Summary\nPrior attempt\n", encoding="utf-8")
    write_json(
        slice_dir / "test-review-attempt-2.json",
        {
            "verdict": "changes_required",
            "summary": "Need stronger assertions.",
            "findings": ["Assertions are too weak."],
            "required_changes": ["Add assertions that prove behavior"],
        },
    )
    (slice_dir / "test-review-attempt-2.md").write_text("# Prior Review\n", encoding="utf-8")
    builder = RevisingLoopBuilder()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=ApprovingLoopCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_loop(context, state)

    assert builder.calls == 1
    assert any("resuming from previous critique at attempt 3" in event for event in progress)
    assert any("Add assertions that prove behavior" in prompt for prompt in builder.prompts)
    assert (slice_dir / "builder-summary-attempt-3.md").exists()
    assert (slice_dir / "test-review-attempt-3.json").exists()


def test_loop_stage_can_continue_after_attempt_budget_is_exhausted(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.loop_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan(run_dir)
    builder = RevisingLoopBuilder()
    critic = ExhaustionLoopCritic()
    progress: list[str] = []
    asked_questions: list[list[str]] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
        ask_questions=lambda stage, questions: asked_questions.append(questions) or ["yes", "2"],
    )

    run_loop(context, state)

    assert critic.calls == 3
    assert builder.calls == 3
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more loop attempts?" in asked_questions[0][0]


def test_loop_stage_resumes_legacy_failed_run(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.loop_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan(run_dir)
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (slice_dir / "builder-summary.md").write_text("# Builder Summary\nLegacy attempt\n", encoding="utf-8")
    (slice_dir / "red.log").write_text("legacy red\n", encoding="utf-8")
    (slice_dir / "green.log").write_text("legacy green\n", encoding="utf-8")
    (slice_dir / "refactor-notes.md").write_text("# Refactor Notes\n- legacy\n", encoding="utf-8")
    write_json(
        slice_dir / "test-review.json",
        {
            "verdict": "changes_required",
            "summary": "Need stronger assertions.",
            "findings": ["Assertions are too weak."],
            "required_changes": ["Add assertions that prove behavior"],
        },
    )
    (slice_dir / "test-review.md").write_text("# Legacy Review\n", encoding="utf-8")
    builder = RevisingLoopBuilder()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=ApprovingLoopCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_loop(context, state)

    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert (slice_dir / "builder-summary-attempt-1.md").exists()
    assert (slice_dir / "test-review-attempt-1.json").exists()
    assert (slice_dir / "builder-summary-attempt-2.md").exists()

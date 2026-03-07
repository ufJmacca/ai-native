from __future__ import annotations

import json
import re
from pathlib import Path

from ai_native.adapters.base import AgentResult
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext
from ai_native.stages.verify import run as run_verify
from ai_native.state import StateStore
from ai_native.utils import write_json
from tests.helpers import FakeWorkflowAdapter

SLICE_DIR_RE = re.compile(r"Slice artifact directory:\n(?P<path>.+)")


def _extract_slice_dir(prompt: str) -> Path:
    match = SLICE_DIR_RE.search(prompt)
    if not match:
        raise AssertionError("Slice artifact directory not found in prompt.")
    return Path(match.group("path").strip())


class VerificationRevisionBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        slice_dir = _extract_slice_dir(prompt)
        slice_dir.mkdir(parents=True, exist_ok=True)
        (slice_dir / "green.log").write_text(f"updated green output {self.calls}\n", encoding="utf-8")
        (slice_dir / "refactor-notes.md").write_text(f"# Refactor Notes\n- verify attempt {self.calls}\n", encoding="utf-8")
        return AgentResult(text=f"# Verification Revision Summary\nAttempt {self.calls}\n")


class OneFailingVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = {
                "verdict": "failed",
                "summary": "Acceptance checks are incomplete.",
                "acceptance_checks": ["Can create a task"],
                "evidence": ["red.log", "green.log"],
                "gaps": ["Add explicit verification for list behavior"],
            }
        else:
            payload = {
                "verdict": "passed",
                "summary": "Acceptance checks are satisfied.",
                "acceptance_checks": ["Can create a task", "Can list tasks"],
                "evidence": ["red.log", "green.log", "refactor-notes.md"],
                "gaps": [],
            }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class PassingVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        payload = {
            "verdict": "passed",
            "summary": "Acceptance checks are satisfied.",
            "acceptance_checks": ["Can create a task", "Can list tasks"],
            "evidence": ["red.log", "green.log", "refactor-notes.md"],
            "gaps": [],
        }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ExhaustionVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        if self.calls < 3:
            payload = {
                "verdict": "failed",
                "summary": "Still missing list verification.",
                "acceptance_checks": ["Can create a task"],
                "evidence": ["red.log", "green.log"],
                "gaps": ["Add explicit verification for list behavior"],
            }
        else:
            payload = {
                "verdict": "passed",
                "summary": "Acceptance checks are satisfied.",
                "acceptance_checks": ["Can create a task", "Can list tasks"],
                "evidence": ["red.log", "green.log", "refactor-notes.md"],
                "gaps": [],
            }
        return AgentResult(text=json.dumps(payload), json_data=payload)


def _seed_slice_plan_and_artifacts(run_dir: Path) -> None:
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
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (slice_dir / "red.log").write_text("red output\n", encoding="utf-8")
    (slice_dir / "green.log").write_text("green output\n", encoding="utf-8")
    (slice_dir / "refactor-notes.md").write_text("# Refactor Notes\n- initial\n", encoding="utf-8")


def test_verify_stage_revises_after_failed_verification(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    builder = VerificationRevisionBuilder()
    verifier = OneFailingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2],
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    artifacts = run_verify(context, state)
    verify_dir = run_dir / "verify"

    assert verifier.calls == 2
    assert builder.calls == 1
    assert any("verification requested changes" in event for event in progress)
    assert any("Add explicit verification for list behavior" in prompt for prompt in builder.prompts)
    assert any("Blocker ledger:" in prompt for prompt in verifier.prompts)
    assert (verify_dir / "S001-attempt-2.json").exists()
    assert (verify_dir / "S001-revision-summary-attempt-2.md").exists()
    assert (verify_dir / "S001-critique-history.md").exists()
    assert (verify_dir / "S001-blocker-ledger.md").exists()
    assert any(path.name == "S001-attempt-2.md" for path in artifacts)


def test_verify_stage_resumes_from_latest_attempt(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 4
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001-attempt-2.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001-attempt-2.md").write_text("# Prior Verification\n", encoding="utf-8")
    builder = VerificationRevisionBuilder()
    verifier = PassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2],
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_verify(context, state)

    assert builder.calls == 1
    assert verifier.calls == 1
    assert any("resuming from previous critique at attempt 3" in event for event in progress)
    assert any("Add explicit verification for list behavior" in prompt for prompt in builder.prompts)
    assert (verify_dir / "S001-attempt-3.json").exists()


def test_verify_stage_can_continue_after_attempt_budget_is_exhausted(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    builder = VerificationRevisionBuilder()
    verifier = ExhaustionVerifier()
    progress: list[str] = []
    asked_questions: list[list[str]] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2],
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
        ask_questions=lambda stage, questions: asked_questions.append(questions) or ["yes", "2"],
    )

    run_verify(context, state)

    assert verifier.calls == 3
    assert builder.calls == 2
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more verification attempts?" in asked_questions[0][0]


def test_verify_stage_resumes_legacy_failed_run(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001.md").write_text("# Legacy Verification\n", encoding="utf-8")
    builder = VerificationRevisionBuilder()
    verifier = PassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2],
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_verify(context, state)

    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert (verify_dir / "S001-attempt-1.json").exists()
    assert (verify_dir / "S001-attempt-2.json").exists()

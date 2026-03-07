from __future__ import annotations

import json
from pathlib import Path

from ai_native.adapters.base import AgentResult
from ai_native.models import ReviewReport
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext
from ai_native.stages.prd import run as run_prd
from ai_native.state import StateStore
from ai_native.utils import write_json
from tests.helpers import FakeWorkflowAdapter


class RevisingPrdBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.attempts = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "prd-artifact.json":
            self.attempts += 1
            if self.attempts == 1:
                payload = {
                    "title": "Task Management PRD",
                    "user_value": "Track work items.",
                    "scope": ["Basic CRUD"],
                    "constraints": ["Keep implementation small"],
                    "acceptance_criteria": ["Users can create tickets"],
                    "out_of_scope": [],
                }
            else:
                payload = {
                    "title": "Task Management PRD",
                    "user_value": "Individuals and managers can track project work through UI and API flows.",
                    "scope": [
                        "Project, epic, story, and task management",
                        "Kanban and sprint/backlog UI flows",
                        "Completed-vs-outstanding dashboard metrics",
                    ],
                    "constraints": ["Keep the first implementation intentionally small", "Use tests before implementation changes"],
                    "acceptance_criteria": [
                        "Users can create and modify tickets in the web UI",
                        "Users can manage tickets in kanban and sprint/backlog views",
                        "Managers can view completed-vs-outstanding dashboard status",
                        "Agents can integrate through the API backend",
                    ],
                    "out_of_scope": ["Advanced multi-tenant administration", "Enterprise deployment hardening beyond v1 assumptions"],
                }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        raise AssertionError("Unexpected non-PRD builder call.")


class OneRejectingPrdCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = ReviewReport(
                verdict="changes_required",
                summary="The PRD must define dashboard scope and make the intentionally-small v1 boundaries explicit.",
                findings=["Dashboard scope is underspecified."],
                required_changes=["Define dashboard scope", "Make v1 scope boundaries explicit"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised PRD is explicit and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ResumePrdBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.attempts = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "prd-artifact.json":
            self.attempts += 1
            payload = {
                "title": "Task Management PRD",
                "user_value": "Individuals and managers can track project work through UI and API flows.",
                "scope": ["Project hierarchy management", "Kanban and sprint/backlog views", "Dashboard metrics"],
                "constraints": ["Keep the first implementation intentionally small", "Use tests before implementation changes"],
                "acceptance_criteria": ["Users can create and modify tickets", "Managers can view dashboard status"],
                "out_of_scope": ["Enterprise administration"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        raise AssertionError("Resume should not call any non-PRD builder prompt.")


class ExhaustionPrdCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls < 3:
            payload = ReviewReport(
                verdict="changes_required",
                summary="Still missing explicit dashboard scope.",
                findings=["Dashboard requirements remain vague."],
                required_changes=["Define dashboard scope"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised PRD is explicit and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ApprovingPrdCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        payload = ReviewReport(
            verdict="approved",
            summary="The revised PRD is explicit and implementable.",
            findings=[],
            required_changes=[],
        ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def _seed_context_plan_architecture(run_dir: Path) -> None:
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["Scope boundaries must be explicit."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    (run_dir / "plan").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "plan" / "plan.json",
        {
            "title": "Task Management Plan",
            "summary": "Implement task management with explicit UI and API contracts.",
            "implementation_steps": ["Define contracts", "Implement features", "Add tests"],
            "interfaces": ["Web UI", "REST API"],
            "data_flow": ["Browser to service to storage"],
            "edge_cases": ["Invalid transitions"],
            "test_strategy": ["Unit and integration tests"],
            "rollout_notes": ["Keep v1 intentionally small."],
        },
    )
    (run_dir / "architecture").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "architecture" / "architecture.json",
        {
            "title": "Task Management Architecture",
            "diagram": "flowchart TD\n  Browser-->WebApp\n  WebApp-->Service\n  Service-->SQLite",
            "legend": ["Browser interacts with the web app.", "Service owns data access."],
            "assumptions": ["Single-service v1."],
        },
    )


def test_prd_stage_revises_after_critique(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.prd_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_context_plan_architecture(run_dir)
    builder = RevisingPrdBuilder()
    critic = OneRejectingPrdCritic()
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
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    artifacts = run_prd(context, state)

    assert builder.attempts == 2
    assert critic.calls == 2
    assert any("critique requested changes" in event for event in progress)
    assert any("Define dashboard scope" in prompt for prompt in builder.prompts)
    assert any("Context report:" in prompt for prompt in critic.prompts)
    assert any("Architecture:" in prompt for prompt in critic.prompts)
    assert any("Blocker ledger:" in prompt for prompt in critic.prompts)
    assert (run_dir / "prd" / "prd-attempt-2.json").exists()
    assert (run_dir / "prd" / "prd-review-attempt-2.json").exists()
    assert (run_dir / "prd" / "critique-history.md").exists()
    assert (run_dir / "prd" / "blocker-ledger.md").exists()
    assert any(path.name == "prd-review-attempt-2.md" for path in artifacts)


def test_prd_stage_resumes_from_latest_attempt(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.prd_max_attempts = 4
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_context_plan_architecture(run_dir)
    stage_dir = run_dir / "prd"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        stage_dir / "prd-attempt-2.json",
        {
            "title": "Task Management PRD",
            "user_value": "Track work items.",
            "scope": ["Basic CRUD"],
            "constraints": ["Keep implementation small"],
            "acceptance_criteria": ["Users can create tickets"],
            "out_of_scope": [],
        },
    )
    (stage_dir / "prd-attempt-2.md").write_text("# Prior PRD\n", encoding="utf-8")
    write_json(
        stage_dir / "prd-review-attempt-2.json",
        {
            "verdict": "changes_required",
            "summary": "Define dashboard scope and make v1 scope boundaries explicit.",
            "findings": ["Scope remains vague."],
            "required_changes": ["Define dashboard scope", "Make v1 scope boundaries explicit"],
        },
    )
    (stage_dir / "prd-review-attempt-2.md").write_text("# Prior Review\n", encoding="utf-8")
    builder = ResumePrdBuilder()
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
        critic=ApprovingPrdCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_prd(context, state)

    assert builder.attempts == 1
    assert any("resuming from previous critique at attempt 3" in event for event in progress)
    assert any("Define dashboard scope" in prompt for prompt in builder.prompts)
    assert (stage_dir / "prd-attempt-3.json").exists()
    assert (stage_dir / "prd-review-attempt-3.json").exists()


def test_prd_stage_can_continue_after_attempt_budget_is_exhausted(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.prd_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_context_plan_architecture(run_dir)
    builder = RevisingPrdBuilder()
    critic = ExhaustionPrdCritic()
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
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
        ask_questions=lambda stage, questions: asked_questions.append(questions) or ["yes", "2"],
    )

    run_prd(context, state)

    assert critic.calls == 3
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more PRD attempts?" in asked_questions[0][0]


def test_prd_stage_resumes_legacy_failed_run(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.prd_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_context_plan_architecture(run_dir)
    stage_dir = run_dir / "prd"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        stage_dir / "prd.json",
        {
            "title": "Task Management PRD",
            "user_value": "Track work items.",
            "scope": ["Basic CRUD"],
            "constraints": ["Keep implementation small"],
            "acceptance_criteria": ["Users can create tickets"],
            "out_of_scope": [],
        },
    )
    (stage_dir / "prd.md").write_text("# Legacy PRD\n", encoding="utf-8")
    write_json(
        stage_dir / "prd-review.json",
        {
            "verdict": "changes_required",
            "summary": "Define dashboard scope and make v1 scope boundaries explicit.",
            "findings": ["Scope remains vague."],
            "required_changes": ["Define dashboard scope", "Make v1 scope boundaries explicit"],
        },
    )
    (stage_dir / "prd-review.md").write_text("# Legacy PRD Review\n", encoding="utf-8")
    builder = ResumePrdBuilder()
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
        critic=ApprovingPrdCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_prd(context, state)

    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert (stage_dir / "prd-attempt-1.json").exists()
    assert (stage_dir / "prd-review-attempt-1.json").exists()
    assert (stage_dir / "prd-attempt-2.json").exists()

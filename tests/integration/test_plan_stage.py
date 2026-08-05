from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_native.adapters.base import AgentResult
from ai_native.factory_runner.author import FactoryClarificationRequired
from ai_native.models import ReviewReport
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext
from ai_native.stages.planning import run as run_plan
from ai_native.state import StateStore
from ai_native.utils import read_json, write_json
from tests.helpers import FakeWorkflowAdapter


class RecordingPlanningAdapter:
    def __init__(
        self,
        role: str,
        calls: list[str],
        *,
        needs_clarification: bool = False,
    ) -> None:
        self.role = role
        self.calls = calls
        self.needs_clarification = needs_clarification
        self.prompts: list[str] = []
        self.estimated_tokens = 0

    @staticmethod
    def _token_estimate(text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 3) // 4)

    def _result(
        self,
        text: str,
        json_data: dict[str, object] | None = None,
    ) -> AgentResult:
        self.estimated_tokens += self._token_estimate(text)
        return AgentResult(text=text, json_data=json_data)

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
    ) -> AgentResult:
        del cwd
        self.prompts.append(prompt)
        self.estimated_tokens += self._token_estimate(prompt)
        schema_name = schema_path.name if schema_path is not None else None
        if schema_name is None:
            if "Phase 1:" in prompt:
                call = "grounding"
            elif "Phase 2:" in prompt:
                call = "intent"
            elif "Phase 3:" in prompt:
                call = "implementation"
            elif "stable approval rubric" in prompt:
                call = "checklist"
            else:
                call = "freeform"
        else:
            call = schema_name
        self.calls.append(f"{self.role}:{call}")

        if schema_name == "question-batch.json":
            payload = {
                "needs_user_input": self.needs_clarification,
                "summary": (
                    "An acceptance-critical contract is missing."
                    if self.needs_clarification
                    else "The immutable factory context is sufficient."
                ),
                "questions": (
                    ["Which observable contract should the implementation provide?"]
                    if self.needs_clarification
                    else []
                ),
            }
            return self._result(json.dumps(payload), payload)
        if schema_name == "plan-artifact.json":
            payload = {
                "title": "Factory plan",
                "summary": "Implement the admitted behavior with focused tests.",
                "implementation_steps": [
                    "Add the failing test",
                    "Implement the behavior",
                ],
                "interfaces": ["The admitted public interface"],
                "data_flow": ["Input -> implementation -> observable output"],
                "edge_cases": ["Reject inputs outside the admitted contract"],
                "test_strategy": ["Run the declared focused and regression checks"],
                "rollout_notes": ["Return the change set without publication"],
            }
            return self._result(json.dumps(payload), payload)
        if schema_name == "review-report.json":
            payload = ReviewReport(
                verdict="approved",
                summary="The plan is acceptance-complete and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
            return self._result(json.dumps(payload), payload)
        return self._result("# Notes\n\nModel-generated planning notes.\n")


def _prepare_planning_run(
    app_config,
    tmp_spec: Path,
    tmp_path: Path,
    *,
    factory_mode: bool,
):
    app_config.workspace.plan_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    state.metadata["factory_mode"] = factory_mode
    run_dir = Path(state.run_dir)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "existing",
            "languages": ["python"],
            "manifests": ["pyproject.toml"],
            "test_frameworks": ["pytest"],
            "architecture_summary": "A small existing Python service.",
            "risks": [],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    return state_store, state, run_dir


def _recording_planning_context(
    app_config,
    tmp_spec: Path,
    state_store: StateStore,
    run_dir: Path,
    builder: RecordingPlanningAdapter,
    critic: RecordingPlanningAdapter,
    *,
    ask_questions=None,
) -> ExecutionContext:
    return ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        **({"ask_questions": ask_questions} if ask_questions is not None else {}),
    )


def test_factory_plan_uses_three_calls_and_materializes_compatibility_artifacts(
    app_config,
    tmp_spec: Path,
    tmp_path: Path,
) -> None:
    state_store, state, run_dir = _prepare_planning_run(
        app_config,
        tmp_spec,
        tmp_path,
        factory_mode=True,
    )
    calls: list[str] = []
    builder = RecordingPlanningAdapter("builder", calls)
    critic = RecordingPlanningAdapter("critic", calls)
    context = _recording_planning_context(
        app_config,
        tmp_spec,
        state_store,
        run_dir,
        builder,
        critic,
    )

    artifacts = run_plan(context, state)

    assert calls == [
        "builder:question-batch.json",
        "builder:plan-artifact.json",
        "critic:review-report.json",
    ]
    artifact_names = {path.name for path in artifacts}
    assert {
        "grounding.md",
        "intent.md",
        "implementation.md",
        "approval-checklist.md",
        "questions.json",
        "answers.json",
        "plan.json",
        "plan-review.json",
    } <= artifact_names
    plan_dir = run_dir / "plan"
    assert (
        "immutable factory"
        in (plan_dir / "grounding.md").read_text(encoding="utf-8").lower()
    )
    assert (
        "every acceptance criterion"
        in (plan_dir / "approval-checklist.md").read_text(encoding="utf-8").lower()
    )
    assert "Plan-Mode-style subworkflow" not in builder.prompts[-1]


def test_factory_plan_clarification_remains_fail_closed(
    app_config,
    tmp_spec: Path,
    tmp_path: Path,
) -> None:
    state_store, state, run_dir = _prepare_planning_run(
        app_config,
        tmp_spec,
        tmp_path,
        factory_mode=True,
    )
    calls: list[str] = []
    builder = RecordingPlanningAdapter(
        "builder",
        calls,
        needs_clarification=True,
    )
    critic = RecordingPlanningAdapter("critic", calls)

    def reject_questions(_stage: str, _questions: list[str]) -> list[str]:
        raise FactoryClarificationRequired("immutable context is incomplete")

    context = _recording_planning_context(
        app_config,
        tmp_spec,
        state_store,
        run_dir,
        builder,
        critic,
        ask_questions=reject_questions,
    )

    with pytest.raises(FactoryClarificationRequired):
        run_plan(context, state)

    assert calls == ["builder:question-batch.json"]
    assert not (run_dir / "plan" / "plan.json").exists()


def test_non_factory_plan_keeps_seven_call_workflow(
    app_config,
    tmp_spec: Path,
    tmp_path: Path,
) -> None:
    state_store, state, run_dir = _prepare_planning_run(
        app_config,
        tmp_spec,
        tmp_path,
        factory_mode=False,
    )
    calls: list[str] = []
    builder = RecordingPlanningAdapter("builder", calls)
    critic = RecordingPlanningAdapter("critic", calls)
    context = _recording_planning_context(
        app_config,
        tmp_spec,
        state_store,
        run_dir,
        builder,
        critic,
    )

    run_plan(context, state)

    assert calls == [
        "builder:grounding",
        "builder:question-batch.json",
        "builder:intent",
        "builder:implementation",
        "builder:checklist",
        "builder:plan-artifact.json",
        "critic:review-report.json",
    ]


def test_factory_plan_saves_enough_estimated_tokens_for_the_ten_thousand_token_budget(
    app_config,
    tmp_spec: Path,
    tmp_path: Path,
) -> None:
    tmp_spec.write_text(
        """# Deterministic fixture health endpoint

## Acceptance criteria
- Add a focused failing standard-library test before implementation.
- GET /health returns status 200 OK.
- The response content type is application/json.
- The response body is exactly {\"status\":\"ok\"}.
- The existing greeting test remains green.
- python -m unittest discover -v passes.

## Non-goals
- Do not publish or merge directly; return only the protocol change set and evidence.

## Constraints
- Allowed paths: app.py and test_app.py.
- Network access: none.
- Do not add dependencies, credentials, deployment behavior, or unrelated endpoints.
- Use only the Python standard library.
""",
        encoding="utf-8",
    )
    factory_store, factory_state, factory_run_dir = _prepare_planning_run(
        app_config,
        tmp_spec,
        tmp_path / "factory",
        factory_mode=True,
    )
    factory_calls: list[str] = []
    factory_builder = RecordingPlanningAdapter("builder", factory_calls)
    factory_critic = RecordingPlanningAdapter("critic", factory_calls)
    run_plan(
        _recording_planning_context(
            app_config,
            tmp_spec,
            factory_store,
            factory_run_dir,
            factory_builder,
            factory_critic,
        ),
        factory_state,
    )

    legacy_store, legacy_state, legacy_run_dir = _prepare_planning_run(
        app_config,
        tmp_spec,
        tmp_path / "legacy",
        factory_mode=False,
    )
    legacy_calls: list[str] = []
    legacy_builder = RecordingPlanningAdapter("builder", legacy_calls)
    legacy_critic = RecordingPlanningAdapter("critic", legacy_calls)
    run_plan(
        _recording_planning_context(
            app_config,
            tmp_spec,
            legacy_store,
            legacy_run_dir,
            legacy_builder,
            legacy_critic,
        ),
        legacy_state,
    )

    factory_tokens = factory_builder.estimated_tokens + factory_critic.estimated_tokens
    legacy_tokens = legacy_builder.estimated_tokens + legacy_critic.estimated_tokens
    assert len(factory_calls) == 3
    assert len(legacy_calls) == 7
    assert legacy_tokens - factory_tokens >= 1_500


class RevisingPlanBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.plan_attempts = 0

    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "plan-artifact.json":
            self.plan_attempts += 1
            if self.plan_attempts == 1:
                payload = {
                    "title": "Task Management Plan",
                    "summary": "Build the feature.",
                    "implementation_steps": ["Add workflow support"],
                    "interfaces": ["Task list view"],
                    "data_flow": ["Request updates data"],
                    "edge_cases": ["Validation errors"],
                    "test_strategy": ["Add tests"],
                    "rollout_notes": ["Ship carefully"],
                }
            else:
                payload = {
                    "title": "Task Management Plan",
                    "summary": "Implement task management with explicit contracts, user assignment rules, and dashboard rollups.",
                    "implementation_steps": [
                        "Add task and assignment domain models",
                        "Implement task CRUD endpoints and service layer contracts",
                        "Add dashboard rollup queries and serializers",
                    ],
                    "interfaces": [
                        "POST /tasks creates a task with title, description, status, assignee_id, and due_at",
                        "PATCH /tasks/{id} updates status and assignment with validation for invalid transitions",
                        "GET /dashboard returns task rollups grouped by assignee and status",
                    ],
                    "data_flow": [
                        "HTTP handlers validate payloads and map them into service commands",
                        "Services persist task changes and assignment records before recalculating dashboard rollups",
                        "Dashboard queries aggregate task state into a view model for the client",
                    ],
                    "edge_cases": [
                        "Reject assignment to unknown users",
                        "Prevent invalid status transitions",
                        "Return zero-count dashboard groups when no tasks match a bucket",
                    ],
                    "test_strategy": [
                        "Unit test service transition and assignment rules with Arrange-Act-Assert structure",
                        "Integration test task endpoints and dashboard responses against acceptance criteria",
                    ],
                    "rollout_notes": [
                        "Release behind a feature branch and verify dashboard aggregates against seeded fixtures"
                    ],
                }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if schema_path and schema_path.name == "question-batch.json":
            payload = {
                "needs_user_input": False,
                "summary": "The spec is detailed enough.",
                "questions": [],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if "stable approval rubric" in prompt:
            return AgentResult(
                text=(
                    "# Approval Checklist\n\n"
                    "## Approval Gates\n"
                    "- Make workflow and API contracts explicit.\n\n"
                    "## Minimum Explicit Contracts\n"
                    "- Task read and write interfaces.\n\n"
                    "## Allowed Defaults\n"
                    "- A small opinionated v1 is acceptable if stated.\n\n"
                    "## Ask The User If\n"
                    "- Workflow semantics remain ambiguous."
                )
            )
        return AgentResult(text="# Notes\nGrounded planning notes.")


class OneRejectingPlanCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = ReviewReport(
                verdict="changes_required",
                summary="The plan needs explicit workflow semantics, interfaces, assignment rules, and dashboard rollups.",
                findings=["Interfaces are underspecified."],
                required_changes=[
                    "Define API contracts",
                    "Specify assignment rules",
                    "Define dashboard rollup behavior",
                ],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised plan is concrete and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def test_plan_stage_revises_after_critique(
    app_config, tmp_spec: Path, tmp_path: Path
) -> None:
    app_config.workspace.plan_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["Initial contracts must be defined explicitly."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    builder = RevisingPlanBuilder()
    critic = OneRejectingPlanCritic()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
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

    artifacts = run_plan(context, state)

    assert builder.plan_attempts == 2
    assert critic.calls == 2
    assert any("critique requested changes" in event for event in progress)
    assert "Define API contracts" in builder.prompts[-1]
    assert any("Approval checklist:" in prompt for prompt in critic.prompts)
    assert any("Blocker ledger:" in prompt for prompt in critic.prompts)
    assert (run_dir / "plan" / "plan.md").exists()
    assert (run_dir / "plan" / "plan-review.md").exists()
    assert (run_dir / "plan" / "approval-checklist.md").exists()
    assert (run_dir / "plan" / "critique-history.md").exists()
    assert (run_dir / "plan" / "blocker-ledger.md").exists()
    assert any(path.name == "plan-review-attempt-2.md" for path in artifacts)
    assert read_json(run_dir / "plan" / "plan-review.json")["verdict"] == "approved"


class QuestionAskingPlanBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "question-batch.json":
            payload = {
                "needs_user_input": True,
                "summary": "The allowed task statuses are not specified.",
                "questions": ["Which task statuses should the first release support?"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if schema_path and schema_path.name == "plan-artifact.json":
            payload = {
                "title": "Task Management Plan",
                "summary": "Implement task management using the user-confirmed statuses.",
                "implementation_steps": [
                    "Add task model",
                    "Add status transitions",
                    "Add tests",
                ],
                "interfaces": ["POST /tasks", "PATCH /tasks/{id}/status"],
                "data_flow": ["Requests validate status values before persistence"],
                "edge_cases": ["Reject unknown statuses"],
                "test_strategy": ["Unit and integration tests cover allowed statuses"],
                "rollout_notes": ["Seed fixtures with the confirmed statuses"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        return AgentResult(text="# Notes\nPlan using the provided user answers.")


class ApprovingCritic:
    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        payload = ReviewReport(
            verdict="approved",
            summary="The plan is concrete and implementable.",
            findings=[],
            required_changes=[],
        ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ReferenceAwarePlanBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "question-batch.json":
            payload = {
                "needs_user_input": False,
                "summary": "No clarification needed.",
                "questions": [],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if schema_path and schema_path.name == "plan-artifact.json":
            payload = {
                "title": "Reference-driven Plan",
                "summary": "Implement the page with fidelity checks.",
                "implementation_steps": [
                    "Extract primitives",
                    "Implement UI",
                    "Run fidelity verify",
                ],
                "interfaces": ["Reference-driven spec frontmatter"],
                "data_flow": ["Spec manifest -> reference context -> plan"],
                "edge_cases": ["Missing preview command"],
                "test_strategy": ["Stage tests cover fidelity prompts"],
                "rollout_notes": ["Document reference workflow"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        return AgentResult(text="# Notes\nUse the reference context.")


def test_plan_stage_passes_user_answers_back_into_planning(
    app_config, tmp_spec: Path, tmp_path: Path
) -> None:
    app_config.workspace.plan_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["Statuses must be defined before tests can be written."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    builder = QuestionAskingPlanBuilder()
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=ApprovingCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        ask_questions=lambda stage, questions: ["todo, in_progress, done"],
    )

    run_plan(context, state)

    assert (run_dir / "plan" / "questions.md").exists()
    assert (run_dir / "plan" / "answers.md").exists()
    assert read_json(run_dir / "plan" / "answers.json") == [
        {
            "question": "Which task statuses should the first release support?",
            "answer": "todo, in_progress, done",
        }
    ]
    assert any("todo, in_progress, done" in prompt for prompt in builder.prompts)


def test_plan_stage_includes_reference_prompt_block_when_reference_context_exists(
    app_config, tmp_path: Path
) -> None:
    spec_path = tmp_path / "spec.md"
    reference_path = tmp_path / "landing.html"
    reference_path.write_text("<html></html>\n", encoding="utf-8")
    spec_path.write_text(
        """
---
ainative:
  workflow_profile: reference_driven_web
  references:
    - id: landing
      label: Landing export
      kind: html_export
      path: landing.html
      route: /
      viewport:
        width: 1440
        height: 1024
        label: desktop
  preview:
    url: http://127.0.0.1:3000
---
# Visual Spec

Build the page.
""".lstrip(),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "existing",
            "languages": ["javascript"],
            "manifests": ["package.json"],
            "test_frameworks": ["pytest"],
            "architecture_summary": "Existing frontend app.",
            "risks": [],
            "touched_areas": ["src"],
            "recommended_questions": [],
        },
    )
    write_json(
        run_dir / "recon" / "reference-context.json",
        {
            "workflow_profile": "reference_driven_web",
            "summary": "Faithful landing page recreation.",
            "design_intent": "Keep the supplied hierarchy and card rhythm.",
            "stable_patterns": ["Hero then card grid"],
            "typography": ["Display headline"],
            "colors": ["#112233"],
            "spacing": ["32px"],
            "layout_patterns": ["Wide hero"],
            "repeated_components": ["Buttons"],
            "responsive_behaviors": ["Collapse to one column"],
            "fidelity_constraints": ["Keep section order"],
        },
    )
    builder = ReferenceAwarePlanBuilder()
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=ApprovingCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
    )

    run_plan(context, state)

    assert any(
        "Reference-driven web fidelity profile is active" in prompt
        for prompt in builder.prompts
    )


class ResumeOnlyBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.plan_attempts = 0

    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "plan-artifact.json":
            self.plan_attempts += 1
            payload = {
                "title": "Task Management Plan",
                "summary": "Clarify read and edit interfaces.",
                "implementation_steps": ["Finalize read/edit flows", "Add tests"],
                "interfaces": ["GET /tasks", "PATCH /tasks/{id}"],
                "data_flow": ["Read and edit paths are explicit"],
                "edge_cases": ["Reject invalid edits"],
                "test_strategy": ["Add interface-level tests"],
                "rollout_notes": ["Ship after the critique is resolved"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if "stable approval rubric" in prompt:
            return AgentResult(
                text=(
                    "# Approval Checklist\n\n"
                    "## Approval Gates\n"
                    "- Resolve read and edit contracts.\n\n"
                    "## Minimum Explicit Contracts\n"
                    "- Read and edit request and response behavior.\n\n"
                    "## Allowed Defaults\n"
                    "- Keep the first release intentionally small.\n\n"
                    "## Ask The User If\n"
                    "- Edit semantics remain ambiguous."
                )
            )
        raise AssertionError(
            "Resume should not rerun grounding, questions, intent, or implementation beyond checklist recovery."
        )


def test_plan_stage_resumes_from_latest_attempt(
    app_config, tmp_spec: Path, tmp_path: Path
) -> None:
    app_config.workspace.plan_max_attempts = 4
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    plan_dir = run_dir / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["Read and edit contracts remain ambiguous."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    (plan_dir / "grounding.md").write_text("# Grounding\n", encoding="utf-8")
    (plan_dir / "intent.md").write_text("# Intent\n", encoding="utf-8")
    (plan_dir / "implementation.md").write_text("# Implementation\n", encoding="utf-8")
    prior_plan = {
        "title": "Task Management Plan",
        "summary": "Previous attempt",
        "implementation_steps": ["Draft plan"],
        "interfaces": ["Task list view"],
        "data_flow": ["Request updates data"],
        "edge_cases": ["Validation errors"],
        "test_strategy": ["Add tests"],
        "rollout_notes": ["Ship carefully"],
    }
    prior_review = {
        "verdict": "changes_required",
        "summary": "Need explicit read/edit interfaces and higher-risk behavior definitions.",
        "findings": ["Interfaces are underspecified."],
        "required_changes": ["Define read/edit contracts"],
    }
    older_review = {
        "verdict": "changes_required",
        "summary": "Earlier review also flagged workflow ambiguity.",
        "findings": ["Workflow behavior was vague."],
        "required_changes": ["Define workflow semantics"],
    }
    write_json(plan_dir / "plan-attempt-1.json", prior_plan)
    (plan_dir / "plan-attempt-1.md").write_text("# Older Plan\n", encoding="utf-8")
    write_json(plan_dir / "plan-review-attempt-1.json", older_review)
    (plan_dir / "plan-review-attempt-1.md").write_text(
        "# Older Review\n", encoding="utf-8"
    )
    write_json(plan_dir / "plan-attempt-3.json", prior_plan)
    (plan_dir / "plan-attempt-3.md").write_text("# Prior Plan\n", encoding="utf-8")
    write_json(plan_dir / "plan-review-attempt-3.json", prior_review)
    (plan_dir / "plan-review-attempt-3.md").write_text(
        "# Prior Review\n", encoding="utf-8"
    )
    builder = ResumeOnlyBuilder()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=ApprovingCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_plan(context, state)

    assert builder.plan_attempts == 1
    assert any(
        "resuming from previous critique at attempt 4" in event for event in progress
    )
    assert any(
        "Need explicit read/edit interfaces" in prompt for prompt in builder.prompts
    )
    assert any("Define workflow semantics" in prompt for prompt in builder.prompts)
    assert (plan_dir / "approval-checklist.md").exists()
    assert "Define read/edit contracts" in (plan_dir / "blocker-ledger.md").read_text(
        encoding="utf-8"
    )
    assert (plan_dir / "plan-attempt-3.json").exists()
    assert (plan_dir / "plan-attempt-4.json").exists()
    assert (plan_dir / "plan-review-attempt-4.json").exists()


class ExhaustionCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(
        self, prompt: str, cwd: Path, schema_path: Path | None = None
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls < 3:
            payload = ReviewReport(
                verdict="changes_required",
                summary="Still missing explicit edit semantics.",
                findings=["Edit semantics are vague."],
                required_changes=["Specify edit behavior"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The plan is now explicit and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def test_plan_stage_can_continue_after_attempt_budget_is_exhausted(
    app_config, tmp_spec: Path, tmp_path: Path
) -> None:
    app_config.workspace.plan_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["Edit semantics remain ambiguous."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    builder = RevisingPlanBuilder()
    critic = ExhaustionCritic()
    progress: list[str] = []
    asked_questions: list[list[str]] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(
            Path(__file__).resolve().parents[2] / "ai_native" / "prompts"
        ),
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
        ask_questions=lambda stage, questions: asked_questions.append(questions)
        or ["yes", "2"],
    )

    run_plan(context, state)

    assert critic.calls == 3
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more planning attempts?" in asked_questions[0][0]
    assert any("Critique history:" in prompt for prompt in critic.prompts)
    assert (run_dir / "plan" / "plan-review-attempt-3.json").exists()

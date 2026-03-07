from __future__ import annotations

import json
from pathlib import Path

from ai_native.adapters.base import AgentResult
from ai_native.models import ReviewReport
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext
from ai_native.stages.planning import run as run_plan
from ai_native.state import StateStore
from ai_native.utils import read_json, write_json
from tests.helpers import FakeWorkflowAdapter


class RevisingPlanBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.plan_attempts = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
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
                    "rollout_notes": ["Release behind a feature branch and verify dashboard aggregates against seeded fixtures"],
                }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        return AgentResult(text="# Notes\nGrounded planning notes.")


class OneRejectingPlanCritic:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            payload = ReviewReport(
                verdict="changes_required",
                summary="The plan needs explicit workflow semantics, interfaces, assignment rules, and dashboard rollups.",
                findings=["Interfaces are underspecified."],
                required_changes=["Define API contracts", "Specify assignment rules", "Define dashboard rollup behavior"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised plan is concrete and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def test_plan_stage_revises_after_critique(app_config, tmp_spec: Path, tmp_path: Path) -> None:
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

    artifacts = run_plan(context, state)

    assert builder.plan_attempts == 2
    assert critic.calls == 2
    assert any("critique requested changes" in event for event in progress)
    assert "Define API contracts" in builder.prompts[-1]
    assert (run_dir / "plan" / "plan.md").exists()
    assert (run_dir / "plan" / "plan-review.md").exists()
    assert any(path.name == "plan-review-attempt-2.md" for path in artifacts)
    assert read_json(run_dir / "plan" / "plan-review.json")["verdict"] == "approved"

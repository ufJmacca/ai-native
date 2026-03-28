from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from ai_native.adapters.base import AgentResult
from ai_native.models import ReviewReport
from ai_native.prompting import PromptLibrary
from ai_native.stages.architecture import run as run_architecture
from ai_native.stages.common import ExecutionContext
from ai_native.state import StateStore
from ai_native.utils import write_json
from tests.helpers import FakeWorkflowAdapter


class RevisingArchitectureBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.attempts = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "diagram-artifact.json":
            self.attempts += 1
            if self.attempts == 1:
                payload = {
                    "title": "Task Management Architecture",
                    "diagram": "flowchart TD\n  Browser-->Scripts\n  Scripts-->DB",
                    "legend": ["Scripts write directly to the database."],
                    "assumptions": ["Greenfield implementation with minimal boundaries."],
                }
            else:
                payload = {
                    "title": "Task Management Architecture",
                    "diagram": "flowchart TD\n  Browser-->WebApp\n  WebApp-->Service\n  Service-->SQLite",
                    "legend": ["Browser interacts through the web app boundary.", "Service owns data access."],
                    "assumptions": ["Single-service v1 with explicit service and storage boundaries."],
                }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        raise AssertionError("Unexpected non-diagram builder call.")


class OneRejectingArchitectureCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = ReviewReport(
                verdict="changes_required",
                summary="The diagram must show the browser/session boundary and route scripts through the service layer.",
                findings=["The current diagram writes to the database directly from scripts."],
                required_changes=["Show the browser/session boundary", "Route scripts through the service/API layer"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised diagram is explicit and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ResumeArchitectureBuilder:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.attempts = 0

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "diagram-artifact.json":
            self.attempts += 1
            payload = {
                "title": "Task Management Architecture",
                "diagram": "flowchart TD\n  Browser-->WebApp\n  WebApp-->Service\n  Service-->SQLite",
                "legend": ["Web app and service boundaries are explicit."],
                "assumptions": ["Single-service v1."],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        raise AssertionError("Resume should not call any non-diagram builder prompt.")


class ExhaustionArchitectureCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls < 3:
            payload = ReviewReport(
                verdict="changes_required",
                summary="Still missing the browser/session boundary.",
                findings=["The browser actor boundary is not explicit."],
                required_changes=["Show the browser/session boundary"],
            ).model_dump(mode="json")
        else:
            payload = ReviewReport(
                verdict="approved",
                summary="The revised diagram is explicit and implementable.",
                findings=[],
                required_changes=[],
            ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ApprovingArchitectureCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        payload = ReviewReport(
            verdict="approved",
            summary="The revised diagram is explicit and implementable.",
            findings=[],
            required_changes=[],
        ).model_dump(mode="json")
        return AgentResult(text=json.dumps(payload), json_data=payload)


def _seed_plan_and_context(run_dir: Path) -> None:
    (run_dir / "recon").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "recon" / "context.json",
        {
            "repo_state": "greenfield",
            "languages": [],
            "manifests": [],
            "test_frameworks": ["pytest"],
            "architecture_summary": "No product code exists yet.",
            "risks": ["System boundaries must be explicit."],
            "touched_areas": ["Application code", "Tests"],
            "recommended_questions": [],
        },
    )
    (run_dir / "plan").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "plan" / "plan.json",
        {
            "title": "Task Management Plan",
            "summary": "Implement task management with explicit service boundaries.",
            "implementation_steps": ["Add service layer", "Add browser UI", "Add tests"],
            "interfaces": ["Browser UI", "Service layer", "SQLite persistence"],
            "data_flow": ["Browser sends requests to web app", "Web app delegates to service", "Service persists to SQLite"],
            "edge_cases": ["Invalid transitions"],
            "test_strategy": ["Integration tests cover browser-to-service behavior"],
            "rollout_notes": ["Keep v1 intentionally small."],
        },
    )


def test_architecture_stage_revises_after_critique(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.architecture_max_attempts = 3
    app_config.workspace.mermaid_validate_command = []
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_plan_and_context(run_dir)
    builder = RevisingArchitectureBuilder()
    critic = OneRejectingArchitectureCritic()
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

    artifacts = run_architecture(context, state)

    assert builder.attempts == 2
    assert critic.calls == 2
    assert any("critique requested changes" in event for event in progress)
    assert any("Route scripts through the service/API layer" in prompt for prompt in builder.prompts)
    assert any("Context report:" in prompt for prompt in critic.prompts)
    assert any("Blocker ledger:" in prompt for prompt in critic.prompts)
    assert (run_dir / "architecture" / "architecture-attempt-2.mmd").exists()
    assert (run_dir / "architecture" / "architecture-review-attempt-2.json").exists()
    assert (run_dir / "architecture" / "critique-history.md").exists()
    assert (run_dir / "architecture" / "blocker-ledger.md").exists()
    assert any(path.name == "validation-attempt-2.json" for path in artifacts)


def test_architecture_stage_resumes_from_latest_attempt(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.architecture_max_attempts = 4
    app_config.workspace.mermaid_validate_command = []
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_plan_and_context(run_dir)
    stage_dir = run_dir / "architecture"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        stage_dir / "architecture-attempt-2.json",
        {
            "title": "Task Management Architecture",
            "diagram": "flowchart TD\n  Browser-->Scripts\n  Scripts-->DB",
            "legend": ["Scripts still write directly to the database."],
            "assumptions": ["Single-service v1."],
        },
    )
    write_json(
        stage_dir / "architecture-review-attempt-2.json",
        {
            "verdict": "changes_required",
            "summary": "Show the browser/session boundary and route scripts through the service layer.",
            "findings": ["The browser boundary is missing."],
            "required_changes": ["Show the browser/session boundary", "Route scripts through the service/API layer"],
        },
    )
    builder = ResumeArchitectureBuilder()
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
        critic=ApprovingArchitectureCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_architecture(context, state)

    assert builder.attempts == 1
    assert any("resuming from previous critique at attempt 3" in event for event in progress)
    assert any("Route scripts through the service/API layer" in prompt for prompt in builder.prompts)
    assert (stage_dir / "architecture-attempt-3.json").exists()
    assert (stage_dir / "validation-attempt-3.json").exists()


def test_architecture_stage_can_continue_after_attempt_budget_is_exhausted(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.architecture_max_attempts = 1
    app_config.workspace.mermaid_validate_command = []
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_plan_and_context(run_dir)
    builder = RevisingArchitectureBuilder()
    critic = ExhaustionArchitectureCritic()
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

    run_architecture(context, state)

    assert critic.calls == 3
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more architecture attempts?" in asked_questions[0][0]


def test_architecture_stage_resumes_legacy_failed_run(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.architecture_max_attempts = 3
    app_config.workspace.mermaid_validate_command = []
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_plan_and_context(run_dir)
    stage_dir = run_dir / "architecture"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        stage_dir / "architecture.json",
        {
            "title": "Task Management Architecture",
            "diagram": "flowchart TD\n  Browser-->Scripts\n  Scripts-->DB",
            "legend": ["Legacy failed architecture."],
            "assumptions": ["Single-service v1."],
        },
    )
    (stage_dir / "architecture.mmd").write_text("flowchart TD\n  Browser-->Scripts\n  Scripts-->DB\n", encoding="utf-8")
    (stage_dir / "architecture.md").write_text("# Legacy Architecture\n", encoding="utf-8")
    write_json(
        stage_dir / "architecture-review.json",
        {
            "verdict": "changes_required",
            "summary": "Show the browser/session boundary and route scripts through the service layer.",
            "findings": ["The browser boundary is missing."],
            "required_changes": ["Show the browser/session boundary", "Route scripts through the service/API layer"],
        },
    )
    (stage_dir / "architecture-review.md").write_text("# Legacy Review\n", encoding="utf-8")
    write_json(stage_dir / "validation.json", {"valid": True, "message": "Legacy validation passed."})
    builder = ResumeArchitectureBuilder()
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
        critic=ApprovingArchitectureCritic(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_architecture(context, state)

    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert (stage_dir / "architecture-attempt-1.json").exists()
    assert (stage_dir / "architecture-review-attempt-1.json").exists()
    assert (stage_dir / "validation-attempt-1.json").exists()
    assert (stage_dir / "architecture-attempt-2.json").exists()


def test_architecture_stage_skips_browser_launch_validation_failures(
    monkeypatch, app_config, tmp_spec: Path, tmp_path: Path
) -> None:
    app_config.workspace.architecture_max_attempts = 3
    app_config.workspace.mermaid_validate_command = ["mmdc"]
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_plan_and_context(run_dir)
    builder = ResumeArchitectureBuilder()
    critic = ApprovingArchitectureCritic()
    progress: list[str] = []

    monkeypatch.setattr("ai_native.stages.architecture.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "ai_native.stages.architecture.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Error: Failed to launch the browser process!\n"
                "[0328/092145.746999:ERROR:zygote_host_impl_linux.cc(101)] "
                "Running as root without --no-sandbox is not supported."
            ),
        ),
    )

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

    run_architecture(context, state)

    validation = json.loads((run_dir / "architecture" / "validation.json").read_text(encoding="utf-8"))
    assert validation["valid"] is True
    assert "browser launch unavailable; validation skipped" in validation["message"]
    assert builder.attempts == 1
    assert critic.calls == 1
    assert not any("validation failed, retrying" in event for event in progress)

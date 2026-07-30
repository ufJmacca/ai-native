"""Factory-only dispatcher around reusable AI Native authoring stages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import cast

from ai_native.config import (
    AppConfig,
    QualityGates,
    RegistryConfig,
    TelemetryConfig,
    WorkspaceConfig,
)
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessRunner,
)
from ai_native.factory_runner.process_policy import resolve_trusted_command
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.workflow_adapter import (
    FactoryGatewayAdapter,
    FactoryWorkflowError,
    FactoryWorkflowPolicyViolation,
    load_gateway_command,
)
from ai_native.adapters.base import AgentResult
from ai_native.models import RunState, StageName
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages.architecture import run as run_architecture
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.stages.intake import run as run_intake
from ai_native.stages.loop import run as run_loop
from ai_native.stages.planning import run as run_plan
from ai_native.stages.prd import run as run_prd
from ai_native.stages.recon import run as run_recon
from ai_native.stages.slicing import run as run_slice
from ai_native.stages.verify import run as run_authoring_verify
from ai_native.utils import utc_now, write_json, write_text
from ai_native.workflow_stages import LEGACY_ORDERED_STAGES


class FactoryClarificationRequired(RuntimeError):
    pass


class FactoryAuthorError(RuntimeError):
    pass


class FactoryAuthorCancelled(RuntimeError):
    pass


class FactoryAuthorTimedOut(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorOutcome:
    completed_stages: tuple[str, ...]


class _BudgetedGatewayAdapter:
    """Share deterministic attempt budgets across all legacy stage roles."""

    def __init__(
        self,
        adapter: FactoryGatewayAdapter,
        *,
        max_turns: int,
        max_tokens: int,
    ) -> None:
        self._adapter = adapter
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._turns = 0
        self._estimated_tokens = 0

    def supports_image_inputs(self) -> bool:
        return self._adapter.supports_image_inputs()

    @staticmethod
    def _token_estimate(text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 3) // 4)

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        if self._turns >= self._max_turns:
            raise FactoryWorkflowError("factory gateway turn budget exhausted")
        prompt_tokens = self._token_estimate(prompt)
        if self._estimated_tokens + prompt_tokens > self._max_tokens:
            raise FactoryWorkflowError("factory gateway token budget exhausted")
        self._turns += 1
        self._estimated_tokens += prompt_tokens
        result = self._adapter.run(
            prompt,
            cwd,
            schema_path=schema_path,
            image_paths=image_paths,
        )
        self._estimated_tokens += self._token_estimate(result.text)
        if self._estimated_tokens > self._max_tokens:
            raise FactoryWorkflowError("factory gateway token budget exhausted")
        return result

    def review(
        self,
        cwd: Path,
        prompt: str,
        base_branch: str | None = None,
    ) -> AgentResult:
        del base_branch
        return self.run(prompt, cwd)


_FACTORY_STAGE_HANDLERS: Mapping[str, Callable[..., list[Path]]] = {
    "intake": run_intake,
    "recon": run_recon,
    "plan": run_plan,
    "architecture": run_architecture,
    "prd": run_prd,
    "slice": run_slice,
    "loop": run_loop,
    "verify": run_authoring_verify,
}


def _factory_config(inputs: ValidatedInputs, private_root: Path) -> AppConfig:
    config = AppConfig(
        workspace=WorkspaceConfig(
            artifacts_dir=private_root,
            specs_dir=private_root,
            base_branch="HEAD",
            parallel_workers=1,
            question_budget_per_stage=1,
            question_budget_per_run=1,
            plan_max_attempts=1,
            architecture_max_attempts=1,
            prd_max_attempts=1,
            loop_max_attempts=1,
            verification_max_attempts=1,
            pr_review_max_attempts=1,
            mermaid_validate_command=[],
            mermaid_validate_args=[],
        ),
        agents={},
        quality_gates=QualityGates(),
        registry=RegistryConfig(
            remote_url=None,
            auth_token=None,
        ),
        telemetry=TelemetryConfig(enabled=False),
        config_path=private_root / "factory-config.disabled",
        repo_root=inputs.workspace,
        package_root=Path(__file__).resolve().parents[1],
    )
    return config


def _private_run_directory(inputs: ValidatedInputs, scratch_root: Path) -> Path:
    identity = inputs.run_spec.identity
    opaque = f"{identity.run_id}\0{identity.attempt_id}".encode()
    safe_name = hashlib.sha256(opaque).hexdigest()
    return scratch_root / "state" / safe_name


def _write_factory_spec(inputs: ValidatedInputs, path: Path) -> None:
    task = inputs.run_spec.task
    sections = [
        f"# {task.outcome}",
        "",
        "## Acceptance criteria",
        *[f"- {criterion}" for criterion in task.acceptance_criteria],
        "",
        "## Non-goals",
        *[f"- {non_goal}" for non_goal in task.non_goals],
        "",
        "## Constraints",
        *[f"- {constraint}" for constraint in task.constraints],
        "",
        "## Repository instructions",
        *[
            f"- {instruction}"
            for instruction in inputs.context_bundle.repository_instructions
        ],
        "",
        "## Trusted policy summary",
        *[
            f"- {observation}"
            for observation in inputs.context_bundle.trusted_policy_summary
        ],
        "",
        "## Approved repository memory",
        *[f"- {memory}" for memory in inputs.context_bundle.approved_repository_memory],
        "",
        "## Dependency outputs",
        *[f"- {dependency}" for dependency in inputs.context_bundle.dependency_outputs],
        "",
        "## Operator input",
        *[f"- {value}" for value in inputs.context_bundle.operator_input],
    ]
    write_text(path, "\n".join(sections).rstrip() + "\n")


def _initial_state(
    inputs: ValidatedInputs,
    *,
    run_dir: Path,
    spec_path: Path,
) -> RunState:
    timestamp = utc_now()
    state = RunState(
        run_id=inputs.run_spec.identity.run_id,
        feature_slug="factory-run",
        spec_path=str(spec_path),
        workspace_root=str(inputs.workspace),
        spec_hash=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        run_dir=str(run_dir),
        created_at=timestamp,
        updated_at=timestamp,
        metadata={
            "factory_mode": True,
            "attempt_id": inputs.run_spec.identity.attempt_id,
            "workspace_artifacts_root": str(run_dir / "agent-workspace"),
        },
    )
    return state


def _materialise_context_report(
    inputs: ValidatedInputs,
    run_dir: Path,
) -> None:
    recon_dir = run_dir / "recon"
    recon_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "repo_state": "existing",
        "languages": [],
        "manifests": [],
        "test_frameworks": [],
        "architecture_summary": (
            "Factory-supplied, digest-verified repository context is available."
        ),
        "risks": list(inputs.context_bundle.trusted_policy_summary),
        "touched_areas": list(inputs.run_spec.policy.allowed_paths),
        "recommended_questions": [],
    }
    write_json(recon_dir / "context.json", report)


def _reject_questions(_stage: str, questions: list[str]) -> list[str]:
    del questions
    raise FactoryClarificationRequired(
        "required information is missing from the immutable factory context"
    )


def execute_author(
    inputs: ValidatedInputs,
    *,
    process_runner: FactoryProcessRunner,
    cancellation_token: CancellationToken,
    deadline: Deadline,
    child_environment: Mapping[str, str],
    scratch_root: Path,
    boundary_check: Callable[[], None],
    restore_workspace: Callable[[], None],
    progress: Callable[[str], None],
) -> AuthorOutcome:
    """Run only explicitly admitted reusable stages in canonical order."""

    command = resolve_trusted_command(
        load_gateway_command(inputs.environment),
        environment=child_environment,
        prohibited_roots=(inputs.workspace, inputs.output_dir),
    )
    private_run_dir = _private_run_directory(inputs, scratch_root)
    gateway_temp_root = scratch_root / "gateway"
    private_run_dir.mkdir(parents=True, exist_ok=True)
    spec_path = private_run_dir / "spec.md"
    _write_factory_spec(inputs, spec_path)

    state_store = StateStore(private_run_dir.parent, registry=None)
    state = _initial_state(
        inputs,
        run_dir=private_run_dir,
        spec_path=spec_path,
    )
    state_store.save(state)
    _materialise_context_report(inputs, private_run_dir)
    config = _factory_config(inputs, private_run_dir.parent)
    adapter = _BudgetedGatewayAdapter(
        FactoryGatewayAdapter(
            command=command,
            process_runner=process_runner,
            environment=child_environment,
            model_profile=inputs.run_spec.policy.model_profile,
            temp_root=gateway_temp_root,
            timeout_seconds=inputs.run_spec.policy.max_wall_seconds,
            protected_roots=(inputs.output_dir, scratch_root),
            mutable_roots=(
                gateway_temp_root,
                private_run_dir / "agent-workspace",
                *(
                    Path(child_environment[key])
                    for key in (
                        "HOME",
                        "TMPDIR",
                        "XDG_CACHE_HOME",
                        "XDG_CONFIG_HOME",
                        "XDG_DATA_HOME",
                    )
                    if key in child_environment
                ),
            ),
            output_root=inputs.output_dir,
        ),
        max_turns=inputs.run_spec.policy.max_agent_turns,
        max_tokens=inputs.run_spec.policy.max_model_tokens,
    )
    context = ExecutionContext(
        config=config,
        prompt_library=PromptLibrary(config.package_root / "prompts"),
        state_store=state_store,
        template_root=config.package_root,
        repo_root=inputs.workspace,
        spec_path=spec_path,
        run_dir=private_run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
        emit_progress=progress,
        ask_questions=_reject_questions,
    )

    requested = set(inputs.run_spec.policy.allowed_stages)
    completed: list[str] = []
    for stage in LEGACY_ORDERED_STAGES:
        if stage not in requested:
            continue
        handler = _FACTORY_STAGE_HANDLERS.get(stage)
        if handler is None:
            raise FactoryAuthorError("publication stage is unavailable in factory mode")
        if cancellation_token.cancelled:
            break
        if deadline.expired:
            break
        boundary_check()
        progress(f"[factory] {stage}: started")
        try:
            try:
                artifacts = handler(context, state)
            finally:
                boundary_check()
        except FactoryClarificationRequired:
            restore_workspace()
            boundary_check()
            raise
        except FactoryWorkflowPolicyViolation as exc:
            raise FactoryPolicyViolation(
                "factory gateway modified protocol output"
            ) from exc
        except FactoryWorkflowError as exc:
            if cancellation_token.cancelled:
                raise FactoryAuthorCancelled from exc
            if deadline.expired:
                raise FactoryAuthorTimedOut from exc
            raise FactoryAuthorError(f"{stage} stage failed") from exc
        except (StageError, ValueError, OSError) as exc:
            raise FactoryAuthorError(f"{stage} stage failed") from exc
        state_store.update_stage(
            state,
            stage=cast(StageName, stage),
            status="completed",
            artifacts=artifacts,
        )
        completed.append(stage)
        progress(f"[factory] {stage}: completed")
    return AuthorOutcome(completed_stages=tuple(completed))


__all__ = [
    "AuthorOutcome",
    "FactoryAuthorCancelled",
    "FactoryAuthorError",
    "FactoryAuthorTimedOut",
    "FactoryClarificationRequired",
    "execute_author",
]

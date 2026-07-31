"""Coordinator for bounded non-interactive factory author and verify runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import shutil
import tempfile
from typing import Any, BinaryIO, Literal

from ai_native.factory_runner.admission import (
    FactoryAdmissionError,
    admit_inputs,
    validate_workspace,
)
from ai_native.factory_runner.author import (
    AuthorUsage,
    FactoryAuthorCancelled,
    FactoryAuthorError,
    FactoryAuthorTimedOut,
    FactoryClarificationRequired,
    execute_author,
)
from ai_native.factory_runner.changes import (
    ChangePolicyError,
    build_change_set,
    capture_repository_security_snapshot,
    capture_workspace_patch,
    restore_clean_author_workspace,
    validate_author_boundary,
)
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.events import EventSink
from ai_native.factory_runner.evidence import EvidenceSufficiencyError
from ai_native.factory_runner.git_runtime import (
    FactoryGitCancelled,
    FactoryGitError,
    FactoryGitRuntime,
    FactoryGitTimedOut,
)
from ai_native.factory_runner.outputs import (
    OutputWriter,
    sanitised_message,
    utc_timestamp,
    validate_output_root,
)
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessRunner,
)
from ai_native.factory_runner.process_policy import (
    FactoryPolicyViolation,
    build_child_environment,
    resolve_trusted_command,
)
from ai_native.factory_runner.protocol import validate_contract
from ai_native.factory_runner.verification import (
    PhaseCommandCompletion,
    PhaseCommandStart,
    PhaseExecutionOutcome,
    RedAlreadyGreen,
    execute_declared_phase,
    execute_verification,
    finalize_authoring_evidence,
)


Operation = Literal["author", "verify"]
_GATEWAY_ONLY_ENVIRONMENT_KEYS = ("ATTEMPT_GATEWAY_TOKEN_FILE",)
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024

_EXIT_BY_REASON = {
    "completed": 0,
    "invalid_json": 2,
    "invalid_input": 2,
    "unsupported_protocol": 2,
    "unsupported_schema": 2,
    "unsupported_schema_version": 2,
    "unsupported_capability": 2,
    "digest_mismatch": 2,
    "policy_denied": 3,
    "missing_requirements": 4,
    "checkpoint_incompatible": 5,
    "verification_failed": 6,
    "runner_failed": 7,
    "cancelled": 8,
    "timed_out": 9,
}


class _RunEvents:
    """Own one attempt's ordered event identity and terminal finalization."""

    def __init__(
        self,
        *,
        writer: OutputWriter,
        spec: RunSpec,
        stdout: BinaryIO | None,
    ) -> None:
        self._sink = EventSink(writer=writer, stdout=stdout)
        self._spec = spec
        self._sequence = 0
        self._terminal_emitted = False
        self._reference: ArtifactReference | None = None

    def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        artifact_refs: Sequence[ArtifactReference] = (),
    ) -> None:
        if self._terminal_emitted:
            raise RuntimeError("events cannot be emitted after a terminal event")
        sequence = self._sequence + 1
        event = RunnerEvent.model_validate(
            {
                "protocol": "factory-runner-protocol/v1",
                "schema": "runner-event/v1",
                "schema_version": 1,
                "run_id": self._spec.identity.run_id,
                "attempt_id": self._spec.identity.attempt_id,
                "sequence": sequence,
                "timestamp": utc_timestamp(),
                "event_type": event_type,
                "correlation_id": self._spec.identity.correlation_id,
                "causation_id": None,
                "sanitised_payload": dict(payload or {}),
                "artifact_refs": [
                    reference.model_dump(mode="json")
                    for reference in artifact_refs
                ],
            }
        )
        self._sink.append(event)
        self._sequence = sequence

    def finalize(
        self,
        *,
        outcome: str,
        reason_code: str,
    ) -> ArtifactReference:
        if self._reference is not None:
            return self._reference
        terminal_type = (
            "RunnerCompleted" if reason_code == "completed" else "RunnerFailed"
        )
        self.emit(
            terminal_type,
            payload={
                "operation": self._spec.operation,
                "outcome": outcome,
                "reason_code": reason_code,
            },
        )
        self._terminal_emitted = True
        self._reference = self._sink.finalize()
        return self._reference


def _stream_target(
    spec: RunSpec,
    event_stdout: BinaryIO | None,
) -> BinaryIO | None:
    if not spec.outputs.stream_events_to_stdout:
        return None
    capabilities = {
        *spec.capabilities.required,
        *spec.capabilities.optional,
    }
    return event_stdout if "structured-events" in capabilities else None


def _git_environment(
    source_environment: Mapping[str, str],
    *,
    sterile_home: Path,
    temp_dir: Path,
) -> dict[str, str]:
    source = dict(source_environment)
    source["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return build_child_environment(
        allowed_keys=("PATH",),
        source_env=source,
        sterile_home=sterile_home,
        temp_dir=temp_dir,
    )


def _candidate_run_spec(path: Path) -> RunSpec | None:
    try:
        if path.is_symlink():
            return None
        metadata = path.stat()
        if metadata.st_size > 16 * 1024 * 1024:
            return None
        candidate = validate_contract(
            path.read_bytes(),
            expected_schema="run-spec/v1",
        )
    except Exception:
        return None
    return candidate if isinstance(candidate, RunSpec) else None


def _path_is_below_git_marker(path: Path) -> bool:
    candidate = path if path.exists() else path.parent
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return True
    return any(
        (ancestor / ".git").exists() for ancestor in (candidate, *candidate.parents)
    )


def _safe_failure_output(
    *,
    output_dir: Path,
    candidate_spec: RunSpec | None,
) -> OutputWriter | None:
    try:
        resolved_output = output_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if candidate_spec is None:
        if _path_is_below_git_marker(resolved_output):
            return None
    else:
        try:
            declared_output = Path(candidate_spec.outputs.output_dir).resolve(
                strict=False
            )
            workspace = Path(candidate_spec.workspace.path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if (
            resolved_output != declared_output
            or resolved_output == workspace
            or resolved_output.is_relative_to(workspace)
            or workspace.is_relative_to(resolved_output)
        ):
            return None
    try:
        validated_output = validate_output_root(resolved_output)
        writer = OutputWriter(
            validated_output,
            max_artifact_bytes=_MAX_ARTIFACT_BYTES,
            max_total_bytes=_MAX_TOTAL_OUTPUT_BYTES,
        )
    except (OSError, ValueError):
        return None
    return writer


def _finish(
    *,
    writer: OutputWriter,
    operation: Operation,
    outcome: str,
    reason_code: str,
    message: str,
    started_at: str,
    spec: RunSpec | None,
    completed_stages: Sequence[str] = (),
    latest_checkpoint: ArtifactReference | None = None,
    change_set: Any | None = None,
    verification_evidence: Any | None = None,
    events: _RunEvents | None = None,
) -> int:
    try:
        finished_at = utc_timestamp()
        event_reference = (
            events.finalize(outcome=outcome, reason_code=reason_code)
            if events is not None
            else writer.write_events_placeholder()
        )
        protocol_manifest = writer.write_protocol_manifest(
            event_stream=event_reference,
        )
        result, reference = writer.write_run_result(
            operation=operation,
            outcome=outcome,  # type: ignore[arg-type]
            reason_code=reason_code,
            message=sanitised_message(message, "Factory runner stopped."),
            started_at=started_at,
            finished_at=finished_at,
            identity=spec.identity if spec is not None else None,
            repository=spec.repository if spec is not None else None,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            change_set=change_set,
            verification_evidence=verification_evidence,
            event_stream_digest=event_reference.digest,
            protocol_manifest=protocol_manifest,
        )
        writer.write_completion(
            result=result,
            result_reference=reference,
            protocol_manifest=protocol_manifest,
        )
        return _EXIT_BY_REASON[reason_code]
    except Exception:
        # A partially finalized output tree is intentionally left without a
        # completion marker and must never be rewritten recursively.
        return _EXIT_BY_REASON["runner_failed"]


def execute_factory(
    *,
    expected_operation: Operation,
    run_spec_path: Path,
    output_dir: Path,
    environment: Mapping[str, str],
    cancellation_token: CancellationToken,
    log: Callable[[str], None],
    event_stdout: BinaryIO | None = None,
) -> int:
    """Execute one complete factory invocation and return its stable exit code."""

    started_at = utc_timestamp()
    try:
        inputs = admit_inputs(
            expected_operation=expected_operation,
            run_spec_path=run_spec_path,
            output_dir=output_dir,
            environment=dict(environment),
        )
    except FactoryAdmissionError as exc:
        reason_code = exc.reason_code
        log(f"[factory] admission denied: {reason_code}")
        candidate_spec = _candidate_run_spec(run_spec_path)
        failure_output = _safe_failure_output(
            output_dir=output_dir,
            candidate_spec=candidate_spec,
        )
        if failure_output is None:
            return _EXIT_BY_REASON[reason_code]
        writer = failure_output
        events = (
            _RunEvents(
                writer=writer,
                spec=candidate_spec,
                stdout=_stream_target(candidate_spec, event_stdout),
            )
            if candidate_spec is not None
            else None
        )
        if events is not None:
            events.emit(
                "RunnerStarted",
                payload={"operation": expected_operation},
            )
        outcome = "policy_denied" if reason_code == "policy_denied" else "invalid_input"
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome=outcome,
            reason_code=reason_code,
            message="Factory input admission failed.",
            started_at=started_at,
            spec=candidate_spec,
            events=events,
        )
    except Exception:
        log("[factory] admission failed")
        candidate_spec = _candidate_run_spec(run_spec_path)
        failure_output = _safe_failure_output(
            output_dir=output_dir,
            candidate_spec=candidate_spec,
        )
        if failure_output is None:
            return _EXIT_BY_REASON["invalid_input"]
        writer = failure_output
        events = (
            _RunEvents(
                writer=writer,
                spec=candidate_spec,
                stdout=_stream_target(candidate_spec, event_stdout),
            )
            if candidate_spec is not None
            else None
        )
        if events is not None:
            events.emit(
                "RunnerStarted",
                payload={"operation": expected_operation},
            )
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="invalid_input",
            reason_code="invalid_input",
            message="Factory input admission failed.",
            started_at=started_at,
            spec=candidate_spec,
            events=events,
        )

    try:
        validated_output = validate_output_root(inputs.output_dir)
        writer = OutputWriter(
            validated_output,
            max_artifact_bytes=_MAX_ARTIFACT_BYTES,
            max_total_bytes=_MAX_TOTAL_OUTPUT_BYTES,
        )
        events = _RunEvents(
            writer=writer,
            spec=inputs.run_spec,
            stdout=_stream_target(inputs.run_spec, event_stdout),
        )
        events.emit(
            "RunnerStarted",
            payload={"operation": expected_operation},
        )
        events.emit(
            "InputValidated",
            payload={"operation": expected_operation},
        )
    except (OSError, ValueError):
        log("[factory] invalid output directory")
        return _EXIT_BY_REASON["invalid_input"]

    completed_stages: tuple[str, ...] = ()
    private_environment_root: Path | None = None
    try:
        deadline = Deadline.from_timeout(inputs.run_spec.policy.max_wall_seconds)
        private_environment_root = Path(
            tempfile.mkdtemp(prefix="ai-native-factory-runner-")
        )
        sterile_home = private_environment_root / "home"
        temp_dir = private_environment_root / "tmp"
        sterile_home.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        process_runner = FactoryProcessRunner(
            cancellation_token=cancellation_token,
            deadline=deadline,
        )
        git_environment = _git_environment(
            inputs.environment,
            sterile_home=sterile_home,
            temp_dir=temp_dir,
        )
        git_runtime = FactoryGitRuntime(
            workspace=inputs.workspace,
            output_dir=inputs.output_dir,
            environment=git_environment,
            process_runner=process_runner,
            deadline=deadline,
        )
        validate_workspace(inputs, git_runtime=git_runtime)

        if (
            expected_operation == "author"
            and not inputs.run_spec.task.acceptance_criteria
        ):
            log("[factory] blocked: acceptance criteria are required")
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="blocked",
                reason_code="missing_requirements",
                message="Acceptance criteria are required for unattended authoring.",
                started_at=started_at,
                spec=inputs.run_spec,
                events=events,
            )

        security_snapshot = capture_repository_security_snapshot(git_runtime)
        command_environment = build_child_environment(
            allowed_keys=inputs.run_spec.policy.allowed_environment_keys,
            source_env=inputs.environment,
            sterile_home=sterile_home,
            temp_dir=temp_dir,
        )
        for command in inputs.run_spec.policy.allowed_commands:
            resolve_trusted_command(
                command,
                environment=command_environment,
                prohibited_roots=(inputs.workspace, inputs.output_dir),
            )

        def boundary_check() -> None:
            validate_author_boundary(
                inputs,
                git_runtime=git_runtime,
                security_snapshot=security_snapshot,
            )

        def restore_workspace() -> None:
            restore_clean_author_workspace(
                inputs,
                git_runtime=git_runtime,
                security_snapshot=security_snapshot,
            )

        def emit_stage_event(status: str, stage: str) -> None:
            event_type = {
                "started": "StageStarted",
                "completed": "StageCompleted",
            }.get(status)
            if event_type is None:
                raise ValueError("author stage event status is invalid")
            events.emit(event_type, payload={"stage": stage})

        def emit_test_started(start: PhaseCommandStart) -> None:
            events.emit(
                "TestStarted",
                payload={
                    "phase": start.phase,
                    "command_index": start.index,
                },
            )

        def emit_test_completed(completion: PhaseCommandCompletion) -> None:
            events.emit(
                "TestCompleted",
                payload={
                    "phase": completion.phase,
                    "command_index": completion.index,
                    "actual_status": completion.actual_status,
                    "failure_classification": (
                        completion.failure_classification
                    ),
                },
            )

        author_usage = AuthorUsage(agent_turns=0, model_tokens=0)

        def record_author_usage(usage: AuthorUsage) -> None:
            nonlocal author_usage
            author_usage = usage

        if cancellation_token.cancelled:
            raise _Cancelled
        if deadline.expired:
            raise _TimedOut

        if expected_operation == "verify":
            verification = execute_verification(
                inputs,
                writer=writer,
                process_runner=process_runner,
                cancellation_token=cancellation_token,
                deadline=deadline,
                sterile_home=sterile_home,
                temp_dir=temp_dir,
                clean_verification=True,
                boundary_check=boundary_check,
                git_runtime=git_runtime,
                on_command_started=emit_test_started,
                on_command_completed=emit_test_completed,
            )
            events.emit(
                "VerificationEvidenceWritten",
                payload={
                    "environment_kind": verification.evidence.environment_kind,
                    "overall_status": verification.evidence.overall_status,
                },
                artifact_refs=(verification.reference,),
            )
            if verification.cancelled:
                raise _Cancelled
            if verification.timed_out:
                raise _TimedOut
            if not verification.passed:
                log("[factory] deterministic verification failed")
                return _finish(
                    writer=writer,
                    operation=expected_operation,
                    outcome="failed",
                    reason_code="verification_failed",
                    message="A declared deterministic verification command failed.",
                    started_at=started_at,
                    spec=inputs.run_spec,
                    completed_stages=(),
                    verification_evidence=verification.reference,
                    events=events,
                )
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="succeeded",
                reason_code="completed",
                message="Clean verification completed successfully.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=("verify",),
                verification_evidence=verification.reference,
                events=events,
            )

        child_environment = build_child_environment(
            allowed_keys=(
                *inputs.run_spec.policy.allowed_environment_keys,
                *_GATEWAY_ONLY_ENVIRONMENT_KEYS,
            ),
            source_env=inputs.environment,
            sterile_home=sterile_home,
            temp_dir=temp_dir,
        )
        phase_outcomes: list[PhaseExecutionOutcome] = []
        baseline_already_green = False
        try:
            red = execute_declared_phase(
                inputs,
                phase="red",
                writer=writer,
                process_runner=process_runner,
                cancellation_token=cancellation_token,
                deadline=deadline,
                sterile_home=sterile_home,
                temp_dir=temp_dir,
                boundary_check=boundary_check,
                git_runtime=git_runtime,
                on_command_started=emit_test_started,
                on_command_completed=emit_test_completed,
            )
            phase_outcomes.append(red)
            if red.cancelled:
                raise _Cancelled
            if red.timed_out:
                raise _TimedOut
        except RedAlreadyGreen:
            baseline_already_green = True

        author = execute_author(
            inputs,
            process_runner=process_runner,
            cancellation_token=cancellation_token,
            deadline=deadline,
            child_environment=child_environment,
            scratch_root=private_environment_root,
            boundary_check=boundary_check,
            restore_workspace=restore_workspace,
            progress=log,
            stage_event=emit_stage_event,
            usage_event=record_author_usage,
        )
        completed_stages = author.completed_stages
        author_usage = AuthorUsage(
            agent_turns=author.agent_turns,
            model_tokens=author.model_tokens,
        )
        if cancellation_token.cancelled:
            raise _Cancelled
        if deadline.expired:
            raise _TimedOut

        if baseline_already_green:
            if capture_workspace_patch(inputs, git_runtime=git_runtime) is not None:
                raise FactoryPolicyViolation(
                    "author changes require a genuine red baseline"
                )
            no_change_verification = execute_declared_phase(
                inputs,
                phase="verification",
                writer=writer,
                process_runner=process_runner,
                cancellation_token=cancellation_token,
                deadline=deadline,
                sterile_home=sterile_home,
                temp_dir=temp_dir,
                boundary_check=boundary_check,
                git_runtime=git_runtime,
                on_command_started=emit_test_started,
                on_command_completed=emit_test_completed,
            )
            if no_change_verification.cancelled:
                raise _Cancelled
            if no_change_verification.timed_out:
                raise _TimedOut
            if not no_change_verification.passed:
                return _finish(
                    writer=writer,
                    operation=expected_operation,
                    outcome="failed",
                    reason_code="verification_failed",
                    message="The already-satisfied behavior became invalid.",
                    started_at=started_at,
                    spec=inputs.run_spec,
                    completed_stages=completed_stages,
                    events=events,
                )
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="no_change",
                reason_code="completed",
                message="The declared behavior was already satisfied.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=completed_stages,
                events=events,
            )

        for phase in ("green", "refactor", "verification"):
            phase_outcome = execute_declared_phase(
                inputs,
                phase=phase,  # type: ignore[arg-type]
                writer=writer,
                process_runner=process_runner,
                cancellation_token=cancellation_token,
                deadline=deadline,
                sterile_home=sterile_home,
                temp_dir=temp_dir,
                boundary_check=boundary_check,
                git_runtime=git_runtime,
                on_command_started=emit_test_started,
                on_command_completed=emit_test_completed,
            )
            phase_outcomes.append(phase_outcome)
            if phase_outcome.cancelled:
                raise _Cancelled
            if phase_outcome.timed_out:
                raise _TimedOut
            if not phase_outcome.passed:
                return _finish(
                    writer=writer,
                    operation=expected_operation,
                    outcome="failed",
                    reason_code="verification_failed",
                    message=f"The declared {phase} phase failed.",
                    started_at=started_at,
                    spec=inputs.run_spec,
                    completed_stages=completed_stages,
                    events=events,
                )

        verification = finalize_authoring_evidence(
            inputs,
            writer=writer,
            phase_outcomes=tuple(phase_outcomes),
        )
        events.emit(
            "VerificationEvidenceWritten",
            payload={
                "environment_kind": verification.evidence.environment_kind,
                "overall_status": verification.evidence.overall_status,
            },
            artifact_refs=(verification.reference,),
        )
        if capture_workspace_patch(inputs, git_runtime=git_runtime) is None:
            raise FactoryPolicyViolation(
                "a genuine red-to-green transition requires repository changes"
            )
        change_set, change_reference = build_change_set(
            inputs,
            writer=writer,
            git_runtime=git_runtime,
            evidence=verification.evidence,
            evidence_reference=verification.reference,
        )
        if change_set is not None and change_reference is not None:
            events.emit(
                "ChangeSetWritten",
                artifact_refs=(change_set.patch, change_reference),
            )
        if change_set is None or change_reference is None:
            raise FactoryPolicyViolation("repository change set is incomplete")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="succeeded",
            reason_code="completed",
            message="Authoring completed with an uncommitted ChangeSet.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            change_set=change_reference,
            events=events,
        )
    except FactoryAdmissionError as exc:
        reason_code = exc.reason_code
        outcome = "policy_denied" if reason_code == "policy_denied" else "invalid_input"
        log(f"[factory] workspace denied: {reason_code}")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome=outcome,
            reason_code=reason_code,
            message="Factory workspace admission failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryClarificationRequired:
        log("[factory] blocked: immutable context is incomplete")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="blocked",
            reason_code="missing_requirements",
            message="The immutable context is insufficient; no prompt was attempted.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryAuthorCancelled:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed during an author stage.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryAuthorTimedOut:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner deadline expired during an author stage.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryGitCancelled:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed during repository inspection.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryGitTimedOut:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner deadline expired during repository inspection.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except _Cancelled:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed at a safe boundary.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except _TimedOut:
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner wall-clock deadline expired.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except (
        FactoryPolicyViolation,
        ChangePolicyError,
        EvidenceSufficiencyError,
        FactoryGitError,
    ):
        log("[factory] runtime policy denied the operation")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="policy_denied",
            reason_code="policy_denied",
            message="Runtime policy denied the attempted operation.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except FactoryAuthorError:
        log("[factory] author workflow failed")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="failed",
            reason_code="runner_failed",
            message="The factory author workflow failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    except Exception:
        log("[factory] runner failed")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="failed",
            reason_code="runner_failed",
            message="The factory runner failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            events=events,
        )
    finally:
        if private_environment_root is not None:
            shutil.rmtree(private_environment_root, ignore_errors=True)


class _Cancelled(RuntimeError):
    pass


class _TimedOut(RuntimeError):
    pass


__all__ = ["execute_factory"]

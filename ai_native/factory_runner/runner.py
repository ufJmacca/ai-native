"""Coordinator for bounded non-interactive factory author and verify runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import math
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, BinaryIO, Literal, cast

from ai_native.factory_runner.admission import (
    FactoryAdmissionError,
    admit_inputs,
    validate_workspace,
)
from ai_native.factory_runner.attempt_secrets import (
    admit_attempt_secrets,
    materialize_attempt_secret_files,
    remove_materialized_attempt_secret_files,
)
from ai_native.factory_runner.author import (
    AuthorUsage,
    FactoryAuthorCancelled,
    FactoryAuthorError,
    FactoryAuthorTimedOut,
    FactoryClarificationRequired,
    execute_author,
    private_run_directory,
    retarget_restored_author_state,
    validate_restored_author_state,
)
from ai_native.factory_runner.checkpoint_runtime import (
    CheckpointRuntimeError,
    CheckpointStateObject,
    build_checkpoint_bundle,
)
from ai_native.factory_runner.checkpoints import (
    CheckpointCancelled,
    CheckpointError,
    CheckpointManager,
    CheckpointTimedOut,
)
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.changes import (
    ChangePolicyError,
    build_change_set,
    capture_repository_security_snapshot,
    capture_workspace_patch,
    restore_clean_author_workspace,
    validate_author_boundary,
    validate_checkpoint_patch_paths,
)
from ai_native.factory_runner.contracts.checkpoint import ResourceBudget
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
    JSON_MEDIA_TYPE,
    OutputWriter,
    sanitised_message,
    utc_timestamp,
    validate_output_root,
)
from ai_native.factory_runner.phase_checkpoint import (
    ALREADY_GREEN_WORKFLOW_KEY,
    PHASE_EVIDENCE_WORKFLOW_KEY,
    restore_already_green_observation,
    restore_phase_evidence,
    snapshot_already_green_observation,
    snapshot_phase_evidence,
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
from ai_native.factory_runner.private_state import (
    PRIVATE_STATE_WORKFLOW_KEY,
    restore_private_run_directory,
    snapshot_private_run_directory,
)
from ai_native.factory_runner.protocol import validate_contract
from ai_native.factory_runner.redaction import SecretScanner
from ai_native.factory_runner.verification import (
    FalseRedEvidenceError,
    PhaseCommandCompletion,
    PhaseCommandStart,
    PhaseExecutionOutcome,
    RedAlreadyGreen,
    RedAlreadyGreenObservation,
    execute_declared_phase,
    execute_verification,
    finalize_authoring_evidence,
    finalize_verification_evidence,
)
from ai_native.workflow_stages import LEGACY_ORDERED_STAGES


Operation = Literal["author", "verify"]
_GATEWAY_ONLY_ENVIRONMENT_KEYS = ("ATTEMPT_GATEWAY_TOKEN_FILE",)
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024
_FINALIZATION_RESERVE_BYTES = 8 * 1024 * 1024
_AUTHOR_WORKFLOW_STATE_SCHEMA = "factory-author-workflow/v1"
_VERIFY_WORKFLOW_STATE_SCHEMA = "factory-verify-workflow/v1"
_EVIDENCE_PHASES = ("red", "green", "refactor", "verification")

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
                    reference.model_dump(mode="json") for reference in artifact_refs
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

    def abort(self) -> None:
        """Discard an event stream that could not be finalized."""

        if self._reference is not None:
            return
        self._sink.abort()


@dataclass(frozen=True, slots=True)
class _SafeCheckpointState:
    """An acknowledged boundary that can be cloned without live-state reads."""

    completed_stages: tuple[str, ...]
    next_permitted_stage: str | None
    workflow_state: Mapping[str, Any]
    workspace_patch: bytes | None
    state_objects: tuple[CheckpointStateObject, ...]
    evidence_object_digests: tuple[str, ...]


class _CheckpointPublicationError(RuntimeError):
    """A local checkpoint sink failed after input compatibility was established."""


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
    secret_scanner: SecretScanner,
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
            secret_scanner=secret_scanner,
            max_artifact_bytes=_MAX_ARTIFACT_BYTES,
            max_total_bytes=_MAX_TOTAL_OUTPUT_BYTES,
            finalization_reserve_bytes=_FINALIZATION_RESERVE_BYTES,
        )
    except (OSError, ValueError):
        return None
    return writer


def _next_permitted_stage(
    run_spec: RunSpec,
    completed_stages: Sequence[str],
) -> str | None:
    completed = set(completed_stages)
    allowed = set(run_spec.policy.allowed_stages)
    return next(
        (
            stage
            for stage in LEGACY_ORDERED_STAGES
            if stage in allowed and stage not in completed
        ),
        None,
    )


def _author_workflow_state(
    value: Mapping[str, Any],
) -> tuple[
    tuple[str, ...],
    bool,
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
]:
    allowed_keys = {
        "schema",
        "boundary",
        "completed_phases",
        "baseline_already_green",
        PRIVATE_STATE_WORKFLOW_KEY,
        PHASE_EVIDENCE_WORKFLOW_KEY,
        ALREADY_GREEN_WORKFLOW_KEY,
    }
    if (
        not isinstance(value, Mapping)
        or not set(value).issubset(allowed_keys)
        or value.get("schema") != _AUTHOR_WORKFLOW_STATE_SCHEMA
        or not isinstance(value.get("boundary"), str)
        or not value.get("boundary")
        or not isinstance(value.get("baseline_already_green"), bool)
    ):
        raise CheckpointError("checkpoint author workflow state is invalid")
    raw_phases = value.get("completed_phases")
    if (
        isinstance(raw_phases, str | bytes | bytearray)
        or not isinstance(raw_phases, Sequence)
        or any(not isinstance(phase, str) for phase in raw_phases)
    ):
        raise CheckpointError("checkpoint completed phases are invalid")
    phases = tuple(raw_phases)
    baseline_already_green = value["baseline_already_green"]
    valid_phases = (
        {("red",), ("red", "verification")}
        if baseline_already_green
        else {
            (),
            ("red",),
            ("red", "green"),
            ("red", "green", "refactor"),
            _EVIDENCE_PHASES,
        }
    )
    if phases not in valid_phases:
        raise CheckpointError("checkpoint completed phases are invalid")
    if baseline_already_green and phases[0] != "red":
        raise CheckpointError("checkpoint no-change baseline is inconsistent")
    private_descriptor = value.get(PRIVATE_STATE_WORKFLOW_KEY)
    phase_descriptor = value.get(PHASE_EVIDENCE_WORKFLOW_KEY)
    already_green_descriptor = value.get(ALREADY_GREEN_WORKFLOW_KEY)
    if private_descriptor is not None and not isinstance(
        private_descriptor,
        Mapping,
    ):
        raise CheckpointError("checkpoint private state descriptor is invalid")
    if phase_descriptor is not None and not isinstance(
        phase_descriptor,
        Mapping,
    ):
        raise CheckpointError("checkpoint phase evidence descriptor is invalid")
    if already_green_descriptor is not None and not isinstance(
        already_green_descriptor,
        Mapping,
    ):
        raise CheckpointError(
            "checkpoint already-green observation descriptor is invalid"
        )
    return (
        phases,
        baseline_already_green,
        cast(Mapping[str, Any] | None, private_descriptor),
        cast(Mapping[str, Any] | None, phase_descriptor),
        cast(Mapping[str, Any] | None, already_green_descriptor),
    )


def _verify_workflow_state(
    value: Mapping[str, Any],
) -> tuple[tuple[str, ...], Mapping[str, Any] | None]:
    allowed_keys = {
        "schema",
        "boundary",
        "completed_phases",
        PHASE_EVIDENCE_WORKFLOW_KEY,
    }
    if (
        not isinstance(value, Mapping)
        or not set(value).issubset(allowed_keys)
        or value.get("schema") != _VERIFY_WORKFLOW_STATE_SCHEMA
        or not isinstance(value.get("boundary"), str)
        or not value.get("boundary")
    ):
        raise CheckpointError("checkpoint verify workflow state is invalid")
    raw_phases = value.get("completed_phases")
    if (
        isinstance(raw_phases, str | bytes | bytearray)
        or not isinstance(raw_phases, Sequence)
        or any(not isinstance(phase, str) for phase in raw_phases)
    ):
        raise CheckpointError("checkpoint verify phases are invalid")
    phases = tuple(raw_phases)
    if phases not in {(), ("verification",)}:
        raise CheckpointError("checkpoint verify phases are invalid")
    descriptor = value.get(PHASE_EVIDENCE_WORKFLOW_KEY)
    if descriptor is not None and not isinstance(descriptor, Mapping):
        raise CheckpointError("checkpoint verify phase evidence is invalid")
    return (
        phases,
        cast(Mapping[str, Any] | None, descriptor),
    )


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
        writer.begin_finalization()
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
        if events is not None:
            try:
                events.abort()
            except Exception:
                pass
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
        secret_admission = admit_attempt_secrets(environment)
    except FactoryPolicyViolation:
        log("[factory] attempt credential source denied")
        # No fallback writer can safely serialize candidate identity fields
        # after only a prefix of the attempt's credential sources was scanned.
        return _EXIT_BY_REASON["policy_denied"]
    secret_scanner = secret_admission.scanner

    try:
        inputs = admit_inputs(
            expected_operation=expected_operation,
            run_spec_path=run_spec_path,
            output_dir=output_dir,
            environment=dict(secret_admission.environment),
        )
    except FactoryAdmissionError as exc:
        reason_code = exc.reason_code
        log(f"[factory] admission denied: {reason_code}")
        candidate_spec = _candidate_run_spec(run_spec_path)
        if candidate_spec is not None:
            try:
                secret_scanner.require_clean_chunks(
                    (canonical_json_bytes(candidate_spec),)
                )
            except FactoryPolicyViolation:
                log("[factory] attempt credential material reached protocol input")
                return _EXIT_BY_REASON["policy_denied"]
        failure_output = _safe_failure_output(
            output_dir=output_dir,
            candidate_spec=candidate_spec,
            secret_scanner=secret_scanner,
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
        if candidate_spec is not None:
            try:
                secret_scanner.require_clean_chunks(
                    (canonical_json_bytes(candidate_spec),)
                )
            except FactoryPolicyViolation:
                log("[factory] attempt credential material reached protocol input")
                return _EXIT_BY_REASON["policy_denied"]
        failure_output = _safe_failure_output(
            output_dir=output_dir,
            candidate_spec=candidate_spec,
            secret_scanner=secret_scanner,
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

    if inputs.checkpoint is not None:
        try:
            secret_scanner.require_clean_chunks(
                (
                    canonical_json_bytes(inputs.checkpoint.checkpoint),
                    *(
                        inputs.checkpoint.objects[path]
                        for path in sorted(inputs.checkpoint.objects)
                    ),
                )
            )
        except FactoryPolicyViolation:
            log("[factory] attempt credential material reached checkpoint input")
            return _EXIT_BY_REASON["policy_denied"]
        except (TypeError, ValueError):
            log("[factory] checkpoint secret scan failed")
            return _EXIT_BY_REASON["runner_failed"]

    try:
        validated_output = validate_output_root(inputs.output_dir)
    except ValueError:
        log("[factory] invalid output directory")
        return _EXIT_BY_REASON["invalid_input"]
    except OSError:
        log("[factory] output directory is unavailable")
        return _EXIT_BY_REASON["runner_failed"]
    try:
        writer = OutputWriter(
            validated_output,
            secret_scanner=secret_scanner,
            max_artifact_bytes=_MAX_ARTIFACT_BYTES,
            max_total_bytes=_MAX_TOTAL_OUTPUT_BYTES,
            finalization_reserve_bytes=_FINALIZATION_RESERVE_BYTES,
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
    except FactoryPolicyViolation:
        log("[factory] attempt credential material reached protocol output")
        if "events" in locals():
            try:
                events.abort()
            except Exception:
                pass
        return _EXIT_BY_REASON["policy_denied"]
    except (OSError, RuntimeError, ValueError):
        log("[factory] output writer failed")
        if "events" in locals():
            try:
                events.abort()
            except Exception:
                pass
        return _EXIT_BY_REASON["runner_failed"]

    candidate_completed_stages: tuple[str, ...] = (
        tuple(inputs.checkpoint.checkpoint.completed_stages)
        if inputs.checkpoint is not None
        else ()
    )
    completed_stages: tuple[str, ...] = ()
    completed_phases: list[str] = []
    phase_outcomes: list[PhaseExecutionOutcome] = []
    already_green_observation: RedAlreadyGreenObservation | None = None
    baseline_already_green = False
    latest_checkpoint: ArtifactReference | None = None
    checkpoint_sequence = (
        inputs.checkpoint.checkpoint.sequence if inputs.checkpoint is not None else 0
    )
    initial_consumed = (
        inputs.checkpoint.checkpoint.budgets.consumed
        if inputs.checkpoint is not None
        else ResourceBudget(wall_seconds=0, agent_turns=0, model_tokens=0)
    )
    write_checkpoint_at_boundary: Callable[..., ArtifactReference] | None = None
    record_cancellation_checkpoint: Callable[[], None] | None = None
    record_terminal_checkpoint: Callable[[str], None] | None = None
    last_safe_checkpoint_state: _SafeCheckpointState | None = None
    cancellation_event_emitted = False
    private_environment_root: Path | None = None

    def emit_cancellation_request() -> None:
        nonlocal cancellation_event_emitted
        if cancellation_event_emitted:
            return
        events.emit(
            "RunnerCancellationRequested",
            payload={"operation": expected_operation},
        )
        cancellation_event_emitted = True

    def try_record_terminal_checkpoint(boundary: str) -> None:
        nonlocal latest_checkpoint
        if record_terminal_checkpoint is None:
            return
        try:
            record_terminal_checkpoint(boundary)
        except Exception:
            latest_checkpoint = None
            log("[factory] terminal checkpoint could not be written")

    try:
        remaining_wall_seconds = (
            inputs.run_spec.policy.max_wall_seconds - initial_consumed.wall_seconds
        )
        deadline = Deadline.from_timeout(remaining_wall_seconds)
        attempt_started_monotonic = time.monotonic()
        private_environment_root = Path(
            tempfile.mkdtemp(prefix="ai-native-factory-runner-")
        )
        sterile_home = private_environment_root / "home"
        temp_dir = private_environment_root / "tmp"
        sterile_home.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        attempt_environment = dict(secret_admission.environment)
        inputs = replace(inputs, environment=attempt_environment)
        process_runner = FactoryProcessRunner(
            cancellation_token=cancellation_token,
            deadline=deadline,
        )
        git_environment = _git_environment(
            attempt_environment,
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
                latest_checkpoint=latest_checkpoint,
                events=events,
            )

        security_snapshot = capture_repository_security_snapshot(git_runtime)
        command_environment = build_child_environment(
            allowed_keys=inputs.run_spec.policy.allowed_environment_keys,
            source_env=attempt_environment,
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
                secret_scanner=secret_scanner,
            )

        def restore_workspace() -> None:
            restore_clean_author_workspace(
                inputs,
                git_runtime=git_runtime,
                security_snapshot=security_snapshot,
            )

        def emit_stage_event(status: str, stage: str) -> None:
            nonlocal completed_stages
            event_type = {
                "started": "StageStarted",
                "completed": "StageCompleted",
            }.get(status)
            if event_type is None:
                raise ValueError("author stage event status is invalid")
            events.emit(event_type, payload={"stage": stage})
            if status == "completed":
                if stage in completed_stages:
                    raise CheckpointRuntimeError(
                        "author stage completion was emitted more than once"
                    )
                completed_stages = (*completed_stages, stage)
                if write_checkpoint_at_boundary is None:
                    raise CheckpointRuntimeError(
                        "checkpoint writer is unavailable at a safe stage boundary"
                    )
                write_checkpoint_at_boundary(f"stage:{stage}")

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
                    "failure_classification": (completion.failure_classification),
                },
            )

        author_usage = AuthorUsage(
            agent_turns=initial_consumed.agent_turns,
            model_tokens=initial_consumed.model_tokens,
        )

        def record_author_usage(usage: AuthorUsage) -> None:
            nonlocal author_usage
            author_usage = usage

        checkpoint_manager = CheckpointManager(inputs.output_dir)
        restored_private_state = False
        if inputs.checkpoint is not None:
            effective_candidate_completed_stages = tuple(
                stage
                for stage in candidate_completed_stages
                if stage in inputs.run_spec.policy.allowed_stages
            )
            historical_run_spec = inputs.run_spec.model_copy(
                update={"policy": inputs.checkpoint.checkpoint.authority}
            )
            historical_inputs = replace(inputs, run_spec=historical_run_spec)
            ordered_allowed_stages = tuple(
                stage
                for stage in LEGACY_ORDERED_STAGES
                if stage in historical_run_spec.policy.allowed_stages
            )
            if (
                candidate_completed_stages
                != ordered_allowed_stages[: len(candidate_completed_stages)]
                or inputs.checkpoint.checkpoint.next_permitted_stage
                != _next_permitted_stage(
                    historical_run_spec,
                    candidate_completed_stages,
                )
            ):
                raise CheckpointError(
                    "checkpoint stage cursor is not a canonical safe boundary"
                )
            if expected_operation == "author":
                (
                    restored_phases,
                    baseline_already_green,
                    private_descriptor,
                    phase_descriptor,
                    already_green_descriptor,
                ) = _author_workflow_state(inputs.checkpoint.checkpoint.workflow_state)
                if (
                    baseline_already_green
                    and inputs.checkpoint.checkpoint.workspace_patch_digest is not None
                ):
                    raise CheckpointError(
                        "checkpoint no-change baseline may not restore workspace changes"
                    )
                if (
                    candidate_completed_stages
                    and (not restored_phases or restored_phases[0] != "red")
                ) or (
                    any(phase != "red" for phase in restored_phases)
                    and inputs.checkpoint.checkpoint.next_permitted_stage is not None
                ):
                    raise CheckpointError(
                        "checkpoint phase progress exceeds its stage cursor"
                    )
                completed_phases.extend(restored_phases)
                if phase_descriptor is not None:
                    try:
                        phase_outcomes.extend(
                            restore_phase_evidence(
                                historical_inputs,
                                writer=writer,
                                descriptor=phase_descriptor,
                                objects=inputs.checkpoint.objects,
                                secret_scanner=secret_scanner,
                                enforce_current_policy=False,
                            )
                        )
                    except FactoryPolicyViolation as exc:
                        raise CheckpointError(
                            "checkpoint phase evidence is invalid"
                        ) from exc
                if already_green_descriptor is not None:
                    try:
                        already_green_observation = restore_already_green_observation(
                            historical_inputs,
                            writer=writer,
                            descriptor=already_green_descriptor,
                            objects=inputs.checkpoint.objects,
                            secret_scanner=secret_scanner,
                            enforce_current_policy=False,
                        )
                    except FactoryPolicyViolation as exc:
                        raise CheckpointError(
                            "checkpoint already-green observation is invalid"
                        ) from exc
                restored_outcome_phases = tuple(
                    outcome.phase for outcome in phase_outcomes
                )
                if baseline_already_green:
                    expected_outcomes = tuple(
                        phase for phase in completed_phases if phase != "red"
                    )
                    if (
                        already_green_observation is None
                        or restored_outcome_phases != expected_outcomes
                    ):
                        raise CheckpointError(
                            "checkpoint no-change phase evidence is inconsistent"
                        )
                elif (
                    already_green_observation is not None
                    or restored_outcome_phases != tuple(completed_phases)
                ):
                    raise CheckpointError("checkpoint phase evidence is incomplete")
                if any(
                    not outcome.passed or outcome.cancelled or outcome.timed_out
                    for outcome in phase_outcomes
                ):
                    raise CheckpointError(
                        "checkpoint completed phase evidence did not pass"
                    )
                if private_descriptor is not None:
                    try:
                        restore_private_run_directory(
                            descriptor=private_descriptor,
                            objects=inputs.checkpoint.objects,
                            destination_run_dir=private_run_directory(
                                inputs,
                                private_environment_root,
                            ),
                            private_root=private_environment_root,
                            workspace_root=inputs.workspace,
                            secret_scanner=secret_scanner,
                        )
                    except FactoryPolicyViolation as exc:
                        raise CheckpointError(
                            "checkpoint private author state is invalid"
                        ) from exc
                    restored_private_state = True
                elif candidate_completed_stages:
                    raise CheckpointError(
                        "checkpoint omits required private author state"
                    )
                if restored_private_state:
                    try:
                        validate_restored_author_state(
                            inputs,
                            scratch_root=private_environment_root,
                            completed_stages=effective_candidate_completed_stages,
                        )
                        retarget_restored_author_state(
                            inputs,
                            scratch_root=private_environment_root,
                        )
                    except FactoryAuthorError as exc:
                        raise CheckpointError(
                            "checkpoint private author state is incompatible"
                        ) from exc
            else:
                restored_phases, phase_descriptor = _verify_workflow_state(
                    inputs.checkpoint.checkpoint.workflow_state
                )
                if inputs.checkpoint.checkpoint.workspace_patch_digest is not None:
                    raise CheckpointError(
                        "checkpoint verify workflow may not restore workspace changes"
                    )
                verification_completed = restored_phases == ("verification",)
                if verification_completed != (
                    candidate_completed_stages == ("verify",)
                    and inputs.checkpoint.checkpoint.next_permitted_stage is None
                ):
                    raise CheckpointError(
                        "checkpoint verify progress exceeds its stage cursor"
                    )
                completed_phases.extend(restored_phases)
                if phase_descriptor is not None:
                    try:
                        phase_outcomes.extend(
                            restore_phase_evidence(
                                historical_inputs,
                                writer=writer,
                                descriptor=phase_descriptor,
                                objects=inputs.checkpoint.objects,
                                secret_scanner=secret_scanner,
                                enforce_current_policy=False,
                            )
                        )
                    except FactoryPolicyViolation as exc:
                        raise CheckpointError(
                            "checkpoint verify phase evidence is invalid"
                        ) from exc
                if tuple(outcome.phase for outcome in phase_outcomes) != tuple(
                    completed_phases
                ):
                    raise CheckpointError(
                        "checkpoint verify phase evidence is incomplete"
                    )
                if candidate_completed_stages == ("verify",) and (
                    len(phase_outcomes) != 1
                    or not phase_outcomes[0].passed
                    or phase_outcomes[0].cancelled
                    or phase_outcomes[0].timed_out
                ):
                    raise CheckpointError(
                        "completed verify checkpoint evidence did not pass"
                    )

            try:
                checkpoint_manager.restore_transactionally(
                    inputs.checkpoint,
                    git_runtime=git_runtime,
                    patch_validator=lambda patch: validate_checkpoint_patch_paths(
                        inputs.run_spec.policy,
                        patch=patch,
                        git_runtime=git_runtime,
                    ),
                    postcondition=boundary_check,
                )
            except CheckpointCancelled:
                raise _Cancelled from None
            except CheckpointTimedOut:
                raise _TimedOut from None
            completed_stages = effective_candidate_completed_stages
            events.emit(
                "CheckpointRestored",
                payload={
                    "sequence": inputs.checkpoint.checkpoint.sequence,
                },
            )

        def capture_safe_checkpoint_state(
            boundary: str,
            *,
            runtime: FactoryGitRuntime | None = None,
        ) -> _SafeCheckpointState:
            if not isinstance(boundary, str) or not boundary or len(boundary) > 128:
                raise CheckpointRuntimeError("checkpoint boundary label is invalid")
            selected_runtime = runtime or git_runtime
            if expected_operation == "author":
                validate_author_boundary(
                    inputs,
                    git_runtime=selected_runtime,
                    security_snapshot=security_snapshot,
                    secret_scanner=secret_scanner,
                )
            patch = (
                capture_workspace_patch(
                    inputs,
                    git_runtime=selected_runtime,
                )
                if expected_operation == "author"
                else None
            )
            if (
                expected_operation == "author"
                and baseline_already_green
                and patch is not None
            ):
                raise FactoryPolicyViolation(
                    "already-satisfied behavior may not checkpoint repository changes"
                )
            state_objects: list[CheckpointStateObject] = []
            evidence_object_digests: list[str] = []
            workflow_state: dict[str, Any]
            if expected_operation == "author":
                workflow_state = {
                    "schema": _AUTHOR_WORKFLOW_STATE_SCHEMA,
                    "boundary": boundary,
                    "completed_phases": list(completed_phases),
                    "baseline_already_green": baseline_already_green,
                }
                run_directory = private_run_directory(
                    inputs,
                    private_environment_root,
                )
                if run_directory.exists() or run_directory.is_symlink():
                    private_snapshot = snapshot_private_run_directory(
                        run_directory,
                        private_root=private_environment_root,
                        workspace_root=inputs.workspace,
                        secret_scanner=secret_scanner,
                    )
                    workflow_state[PRIVATE_STATE_WORKFLOW_KEY] = (
                        private_snapshot.descriptor
                    )
                    state_objects.extend(private_snapshot.objects)
                if already_green_observation is not None:
                    already_green_snapshot = snapshot_already_green_observation(
                        inputs,
                        writer=writer,
                        observation=already_green_observation,
                        secret_scanner=secret_scanner,
                        enforce_current_policy=inputs.checkpoint is None,
                    )
                    workflow_state[ALREADY_GREEN_WORKFLOW_KEY] = (
                        already_green_snapshot.descriptor
                    )
                    state_objects.extend(already_green_snapshot.objects)
                    evidence_object_digests.extend(
                        item.digest for item in already_green_snapshot.objects
                    )
                if phase_outcomes:
                    phase_snapshot = snapshot_phase_evidence(
                        inputs,
                        writer=writer,
                        phase_outcomes=phase_outcomes,
                        secret_scanner=secret_scanner,
                        enforce_current_policy=inputs.checkpoint is None,
                    )
                    workflow_state[PHASE_EVIDENCE_WORKFLOW_KEY] = (
                        phase_snapshot.descriptor
                    )
                    state_objects.extend(phase_snapshot.objects)
                    evidence_object_digests.extend(
                        item.digest for item in phase_snapshot.objects
                    )
                next_permitted_stage = _next_permitted_stage(
                    inputs.run_spec,
                    completed_stages,
                )
            else:
                workflow_state = {
                    "schema": _VERIFY_WORKFLOW_STATE_SCHEMA,
                    "boundary": boundary,
                    "completed_phases": list(completed_phases),
                }
                if phase_outcomes:
                    phase_snapshot = snapshot_phase_evidence(
                        inputs,
                        writer=writer,
                        phase_outcomes=phase_outcomes,
                        secret_scanner=secret_scanner,
                        enforce_current_policy=inputs.checkpoint is None,
                    )
                    workflow_state[PHASE_EVIDENCE_WORKFLOW_KEY] = (
                        phase_snapshot.descriptor
                    )
                    state_objects.extend(phase_snapshot.objects)
                    evidence_object_digests.extend(
                        item.digest for item in phase_snapshot.objects
                    )
                next_permitted_stage = (
                    None if "verify" in completed_stages else "verify"
                )
            return _SafeCheckpointState(
                completed_stages=completed_stages,
                next_permitted_stage=next_permitted_stage,
                workflow_state=workflow_state,
                workspace_patch=patch,
                state_objects=tuple(state_objects),
                evidence_object_digests=tuple(dict.fromkeys(evidence_object_digests)),
            )

        def consumed_budget() -> ResourceBudget:
            elapsed = max(
                0,
                math.ceil(time.monotonic() - attempt_started_monotonic),
            )
            return ResourceBudget(
                wall_seconds=min(
                    inputs.run_spec.policy.max_wall_seconds,
                    initial_consumed.wall_seconds + elapsed,
                ),
                agent_turns=author_usage.agent_turns,
                model_tokens=author_usage.model_tokens,
            )

        def publish_checkpoint_state(
            state: _SafeCheckpointState,
            *,
            check_stop: bool,
        ) -> ArtifactReference:
            nonlocal checkpoint_sequence, latest_checkpoint
            nonlocal last_safe_checkpoint_state
            checkpoint_sequence += 1
            try:
                created_at = utc_timestamp()
                budget = consumed_budget()

                def build_bundle(
                    evidence_refs: Sequence[ArtifactReference] = (),
                ) -> Any:
                    return build_checkpoint_bundle(
                        run_spec=inputs.run_spec,
                        context_bundle_digest=inputs.context_digest,
                        sequence=checkpoint_sequence,
                        created_at=created_at,
                        completed_stages=state.completed_stages,
                        next_permitted_stage=cast(
                            Any,
                            state.next_permitted_stage,
                        ),
                        workflow_state=state.workflow_state,
                        consumed=budget,
                        workspace_patch=state.workspace_patch,
                        state_objects=state.state_objects,
                        evidence_refs=evidence_refs,
                        secret_scanner=secret_scanner,
                    )

                bundle = build_bundle()
                expected_evidence_digests = set(state.evidence_object_digests)
                evidence_refs = tuple(
                    reference
                    for reference in bundle.checkpoint.artifact_manifest
                    if reference.digest in expected_evidence_digests
                )
                if {
                    reference.digest for reference in evidence_refs
                } != expected_evidence_digests:
                    raise CheckpointRuntimeError(
                        "checkpoint evidence objects are missing from its manifest"
                    )
                if evidence_refs:
                    bundle = build_bundle(evidence_refs)
                checkpoint_content = canonical_json_bytes(bundle.checkpoint)
                checkpoint_reference = ArtifactReference(
                    path=(f"checkpoints/{checkpoint_sequence}/checkpoint.json"),
                    media_type=JSON_MEDIA_TYPE,
                    byte_size=len(checkpoint_content),
                    digest=sha256_digest(checkpoint_content),
                )
                bundle_references = (
                    *bundle.checkpoint.artifact_manifest,
                    checkpoint_reference,
                )
                try:
                    writer.publish_external_bundle(
                        bundle_references,
                        {
                            **bundle.objects,
                            checkpoint_reference.path: checkpoint_content,
                        },
                    )
                except FactoryPolicyViolation:
                    raise
                except Exception as exc:
                    raise _CheckpointPublicationError(
                        "checkpoint bundle could not be published"
                    ) from exc
            except Exception:
                checkpoint_sequence -= 1
                raise
            written_reference = checkpoint_reference
            events.emit(
                "CheckpointWritten",
                payload={
                    "sequence": bundle.checkpoint.sequence,
                    "boundary": state.workflow_state["boundary"],
                },
                artifact_refs=(written_reference,),
            )
            latest_checkpoint = written_reference
            last_safe_checkpoint_state = state
            if check_stop:
                if cancellation_token.cancelled:
                    raise _Cancelled
                if deadline.expired:
                    raise _TimedOut
            return written_reference

        def write_runtime_checkpoint(
            boundary: str,
            *,
            runtime: FactoryGitRuntime | None = None,
        ) -> ArtifactReference:
            state = capture_safe_checkpoint_state(boundary, runtime=runtime)
            return publish_checkpoint_state(state, check_stop=True)

        write_checkpoint_at_boundary = write_runtime_checkpoint
        last_safe_checkpoint_state = capture_safe_checkpoint_state(
            "restored" if inputs.checkpoint is not None else "admitted"
        )
        if inputs.checkpoint is not None:
            write_runtime_checkpoint("restored")

        def write_terminal_budget_checkpoint(boundary: str) -> None:
            nonlocal completed_stages
            if last_safe_checkpoint_state is None:
                return
            terminal_state = _SafeCheckpointState(
                completed_stages=last_safe_checkpoint_state.completed_stages,
                next_permitted_stage=(last_safe_checkpoint_state.next_permitted_stage),
                workflow_state={
                    **last_safe_checkpoint_state.workflow_state,
                    "boundary": boundary,
                },
                workspace_patch=last_safe_checkpoint_state.workspace_patch,
                state_objects=last_safe_checkpoint_state.state_objects,
                evidence_object_digests=(
                    last_safe_checkpoint_state.evidence_object_digests
                ),
            )
            publish_checkpoint_state(terminal_state, check_stop=False)
            completed_stages = terminal_state.completed_stages

        record_terminal_checkpoint = write_terminal_budget_checkpoint

        def record_cancellation() -> None:
            nonlocal latest_checkpoint
            emit_cancellation_request()
            try:
                write_terminal_budget_checkpoint("cancellation")
            except Exception:
                latest_checkpoint = None
                raise

        record_cancellation_checkpoint = record_cancellation

        if cancellation_token.cancelled:
            raise _Cancelled
        if deadline.expired:
            raise _TimedOut

        if expected_operation == "verify":
            if completed_stages == ("verify",):
                verification = finalize_verification_evidence(
                    inputs,
                    writer=writer,
                    phase_outcome=phase_outcomes[0],
                    clean_verification=True,
                )
            else:
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
                try_record_terminal_checkpoint("verification-failed")
                return _finish(
                    writer=writer,
                    operation=expected_operation,
                    outcome="failed",
                    reason_code="verification_failed",
                    message="A declared deterministic verification command failed.",
                    started_at=started_at,
                    spec=inputs.run_spec,
                    completed_stages=(),
                    latest_checkpoint=latest_checkpoint,
                    verification_evidence=verification.reference,
                    events=events,
                )
            if verification.phase_outcome is None:
                raise EvidenceSufficiencyError(
                    "clean verification omitted its phase outcome"
                )
            if not phase_outcomes:
                phase_outcomes.append(verification.phase_outcome)
            completed_stages = ("verify",)
            if "verification" not in completed_phases:
                completed_phases.append("verification")
                write_runtime_checkpoint("clean-verification")
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="succeeded",
                reason_code="completed",
                message="Clean verification completed successfully.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=completed_stages,
                latest_checkpoint=latest_checkpoint,
                verification_evidence=verification.reference,
                events=events,
            )

        if "red" not in completed_phases:
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
                if red.cancelled:
                    raise _Cancelled
                if red.timed_out:
                    raise _TimedOut
                phase_outcomes.append(red)
            except RedAlreadyGreen as exc:
                baseline_already_green = True
                already_green_observation = exc.observation
            except FalseRedEvidenceError:
                if cancellation_token.cancelled:
                    raise _Cancelled from None
                if deadline.expired:
                    raise _TimedOut from None
                raise
            completed_phases.append("red")
            write_runtime_checkpoint("red")

        gateway_secret_root = private_environment_root / "secrets"
        gateway_environment = materialize_attempt_secret_files(
            secret_admission,
            destination=gateway_secret_root,
        )
        gateway_inputs = replace(inputs, environment=gateway_environment)
        child_environment = build_child_environment(
            allowed_keys=(
                *inputs.run_spec.policy.allowed_environment_keys,
                *_GATEWAY_ONLY_ENVIRONMENT_KEYS,
            ),
            source_env=gateway_environment,
            sterile_home=sterile_home,
            temp_dir=temp_dir,
        )
        try:
            author = execute_author(
                gateway_inputs,
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
                completed_stages=completed_stages,
                resume_existing=restored_private_state,
                initial_agent_turns=initial_consumed.agent_turns,
                initial_model_tokens=initial_consumed.model_tokens,
            )
        finally:
            remove_materialized_attempt_secret_files(
                secret_admission,
                destination=gateway_secret_root,
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
            if "verification" not in completed_phases:
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
                    try_record_terminal_checkpoint("verification-failed")
                    return _finish(
                        writer=writer,
                        operation=expected_operation,
                        outcome="failed",
                        reason_code="verification_failed",
                        message="The already-satisfied behavior became invalid.",
                        started_at=started_at,
                        spec=inputs.run_spec,
                        completed_stages=completed_stages,
                        latest_checkpoint=latest_checkpoint,
                        events=events,
                    )
                phase_outcomes.append(no_change_verification)
                completed_phases.append("verification")
                write_runtime_checkpoint("verification")
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="no_change",
                reason_code="completed",
                message="The declared behavior was already satisfied.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=completed_stages,
                latest_checkpoint=latest_checkpoint,
                events=events,
            )

        for phase in ("green", "refactor", "verification"):
            if phase in completed_phases:
                continue
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
            if phase_outcome.cancelled:
                raise _Cancelled
            if phase_outcome.timed_out:
                raise _TimedOut
            if not phase_outcome.passed:
                try_record_terminal_checkpoint(f"{phase}-failed")
                return _finish(
                    writer=writer,
                    operation=expected_operation,
                    outcome="failed",
                    reason_code="verification_failed",
                    message=f"The declared {phase} phase failed.",
                    started_at=started_at,
                    spec=inputs.run_spec,
                    completed_stages=completed_stages,
                    latest_checkpoint=latest_checkpoint,
                    events=events,
                )
            phase_outcomes.append(phase_outcome)
            completed_phases.append(phase)
            write_runtime_checkpoint(phase)

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
        write_runtime_checkpoint("author-verification")
        change_set, change_reference = build_change_set(
            inputs,
            writer=writer,
            git_runtime=git_runtime,
            evidence=verification.evidence,
            evidence_reference=verification.reference,
            secret_scanner=secret_scanner,
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
            latest_checkpoint=latest_checkpoint,
            change_set=change_reference,
            events=events,
        )
    except FactoryAdmissionError as exc:
        reason_code = exc.reason_code
        outcome = "policy_denied" if reason_code == "policy_denied" else "invalid_input"
        log(f"[factory] workspace denied: {reason_code}")
        try_record_terminal_checkpoint("workspace-admission-failed")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome=outcome,
            reason_code=reason_code,
            message="Factory workspace admission failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except _CheckpointPublicationError:
        log("[factory] checkpoint publication failed")
        latest_checkpoint = None
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="failed",
            reason_code="runner_failed",
            message="A local checkpoint boundary could not be published safely.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except CheckpointError:
        log("[factory] checkpoint runtime state is incompatible")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="invalid_input",
            reason_code="checkpoint_incompatible",
            message="Checkpoint runtime state is incompatible with this attempt.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryClarificationRequired:
        log("[factory] blocked: immutable context is incomplete")
        try_record_terminal_checkpoint("missing-requirements")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="blocked",
            reason_code="missing_requirements",
            message="The immutable context is insufficient; no prompt was attempted.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryAuthorCancelled:
        emit_cancellation_request()
        if record_cancellation_checkpoint is not None:
            try:
                record_cancellation_checkpoint()
            except Exception:
                log("[factory] cancellation checkpoint could not be written")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed during an author stage.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryAuthorTimedOut:
        try_record_terminal_checkpoint("timed-out")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner deadline expired during an author stage.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryGitCancelled:
        emit_cancellation_request()
        if record_cancellation_checkpoint is not None:
            try:
                record_cancellation_checkpoint()
            except Exception:
                log("[factory] cancellation checkpoint could not be written")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed during repository inspection.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryGitTimedOut:
        try_record_terminal_checkpoint("timed-out")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner deadline expired during repository inspection.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except _Cancelled:
        emit_cancellation_request()
        if record_cancellation_checkpoint is not None:
            try:
                record_cancellation_checkpoint()
            except Exception:
                log("[factory] cancellation checkpoint could not be written")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="cancelled",
            reason_code="cancelled",
            message="Cancellation was observed at a safe boundary.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except _TimedOut:
        try_record_terminal_checkpoint("timed-out")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="timed_out",
            reason_code="timed_out",
            message="The runner wall-clock deadline expired.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except (
        FactoryPolicyViolation,
        ChangePolicyError,
        EvidenceSufficiencyError,
        FactoryGitError,
    ):
        log("[factory] runtime policy denied the operation")
        try_record_terminal_checkpoint("policy-denied")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="policy_denied",
            reason_code="policy_denied",
            message="Runtime policy denied the attempted operation.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except FactoryAuthorError:
        log("[factory] author workflow failed")
        try_record_terminal_checkpoint("author-failed")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="failed",
            reason_code="runner_failed",
            message="The factory author workflow failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
            events=events,
        )
    except Exception:
        log("[factory] runner failed")
        try_record_terminal_checkpoint("runner-failed")
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="failed",
            reason_code="runner_failed",
            message="The factory runner failed.",
            started_at=started_at,
            spec=inputs.run_spec,
            completed_stages=completed_stages,
            latest_checkpoint=latest_checkpoint,
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

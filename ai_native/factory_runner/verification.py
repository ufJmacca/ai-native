"""Deterministic phase execution and v1 verification evidence materialisation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Literal, cast

from ai_native import __version__
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    RunnerBuildIdentity,
)
from ai_native.factory_runner.contracts.verification_evidence import (
    EvidenceItem,
    EvidencePhase,
    EvidenceStatus,
    FailureClassification,
    TerminationReason,
    VerificationEvidence,
)
from ai_native.factory_runner.evidence import (
    EvidenceSufficiencyError,
    RedFailureObservation,
    build_authoring_evidence,
    classify_red_failure,
)
from ai_native.factory_runner.git_runtime import FactoryGitRuntime
from ai_native.factory_runner.outputs import (
    EMPTY_DIGEST,
    OutputWriter,
    capture_output_tree,
    enforce_output_tree_unchanged,
    utc_timestamp,
)
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessResult,
    FactoryProcessRunner,
)
from ai_native.factory_runner.process_policy import (
    FactoryPolicyViolation,
    build_child_environment,
    resolve_trusted_command,
    validate_declared_command,
)
from ai_native.factory_runner.protocol import contract_document_digest


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    passed: bool
    cancelled: bool
    timed_out: bool
    evidence: VerificationEvidence
    reference: ArtifactReference


@dataclass(frozen=True, slots=True)
class PhaseCommandStart:
    """A declared command is about to execute for one evidence phase."""

    phase: EvidencePhase
    index: int
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PhaseCommandCompletion:
    """A declared command has reached a safe post-process boundary."""

    phase: EvidencePhase
    index: int
    command: tuple[str, ...]
    actual_status: EvidenceStatus
    failure_classification: FailureClassification
    exit_code: int | None
    termination_reason: TerminationReason


@dataclass(frozen=True, slots=True)
class PhaseExecutionOutcome:
    """Runner-owned evidence items produced by one named TDD phase."""

    phase: EvidencePhase
    passed: bool
    cancelled: bool
    timed_out: bool
    items: tuple[EvidenceItem, ...]


class FalseRedEvidenceError(EvidenceSufficiencyError):
    """The observed failing command cannot prove the intended missing behaviour."""

    def __init__(self, failure_classification: FailureClassification) -> None:
        self.failure_classification = failure_classification
        super().__init__(
            "red phase did not prove the intended behavioral failure "
            f"({failure_classification})"
        )


PhaseStartedCallback = Callable[[PhaseCommandStart], None]
PhaseCompletedCallback = Callable[[PhaseCommandCompletion], None]
RedObservationFactory = Callable[
    [tuple[str, ...], int, FactoryProcessResult, bool],
    RedFailureObservation,
]


_VALID_PHASES: tuple[EvidencePhase, ...] = (
    "red",
    "green",
    "refactor",
    "verification",
)
_PYTEST_FAILED_TEST = re.compile(r"(?m)^(?:FAILED|ERROR)\s+([^\s]+(?:::[^\s]+)*)")


def _repository_snapshot(git_runtime: FactoryGitRuntime) -> bytes:
    """Capture the prepared repository state without invoking a shell."""

    fragments: list[bytes] = []
    for arguments in (
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        (
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ),
        (
            "diff",
            "--binary",
            "--full-index",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ),
    ):
        output = git_runtime.run(*arguments)
        fragments.extend(
            (
                b"\0".join(argument.encode() for argument in arguments),
                b"\0",
                output,
                b"\0",
            )
        )
    return b"".join(fragments)


def _failure_details(
    *,
    termination_reason: str,
    returncode: int | None,
    repository_changed: bool,
) -> tuple[
    Literal["passed", "failed"],
    int | None,
    Literal[
        "none",
        "assertion_failure",
        "test_failure",
        "infrastructure_error",
        "timeout",
    ],
]:
    if termination_reason == "timed_out":
        return "failed", None, "timeout"
    if termination_reason == "cancelled":
        return "failed", None, "infrastructure_error"
    if returncode is not None and returncode < 0:
        return "failed", returncode, "infrastructure_error"
    if returncode not in (None, 0):
        return "failed", returncode, "test_failure"
    if repository_changed:
        return "failed", 1, "assertion_failure"
    return "passed", 0, "none"


def _captured_bytes(binary: bytes, text: str) -> bytes:
    """Prefer the exact bounded byte capture, with compatibility for test doubles."""

    if binary or not text:
        return binary
    return text.encode("utf-8", errors="replace")


def _termination_reason(result: FactoryProcessResult) -> TerminationReason:
    if (
        result.termination_reason == "exited"
        and result.returncode is not None
        and result.returncode < 0
    ):
        return "signalled"
    return cast(TerminationReason, result.termination_reason)


def observe_declared_red_failure(
    command: tuple[str, ...],
    index: int,
    result: FactoryProcessResult,
    repository_changed: bool,
) -> RedFailureObservation:
    """Conservatively classify one declared Red command from bounded output."""

    intended_selectors = tuple(
        argument
        for argument in command[1:]
        if "::" in argument and not argument.startswith("-")
    )
    intended_test = (
        intended_selectors[0]
        if len(intended_selectors) == 1
        else f"declared-command-{index:03d}"
    )
    output = b"\n".join(
        (
            _captured_bytes(result.stdout_bytes, result.stdout),
            _captured_bytes(result.stderr_bytes, result.stderr),
        )
    )
    decoded = output.decode("utf-8", errors="replace")
    lowered = decoded.casefold()
    failed_tests = tuple(dict.fromkeys(_PYTEST_FAILED_TEST.findall(decoded)))

    termination_reason = result.termination_reason
    observed_failure = "unrelated_failure"
    if termination_reason == "timed_out":
        observed_failure = "timeout"
    elif termination_reason != "exited" or (
        result.returncode is not None and result.returncode < 0
    ):
        observed_failure = "infrastructure_error"
        termination_reason = "signalled"
    elif result.returncode in (None, 0):
        observed_failure = "infrastructure_error"
    elif repository_changed or result.stdout_truncated or result.stderr_truncated:
        observed_failure = "infrastructure_error"
    elif any(marker in lowered for marker in ("syntaxerror", "indentationerror")):
        observed_failure = "syntax_error"
    elif any(
        marker in lowered
        for marker in (
            "error collecting",
            "errors during collection",
            "collected 0 items",
            "no tests ran",
        )
    ):
        observed_failure = "collection_error"
    elif any(
        marker in lowered
        for marker in (
            "modulenotfounderror",
            "importerror",
            "no module named",
            "command not found",
        )
    ):
        observed_failure = "dependency_error"
    elif any(
        marker in lowered
        for marker in (
            "authentication required",
            "authentication failed",
            "credential",
            "unauthorized",
        )
    ):
        observed_failure = "credential_error"
    elif any(
        marker in lowered
        for marker in (
            "internal error",
            "segmentation fault",
            "resource temporarily unavailable",
        )
    ):
        observed_failure = "infrastructure_error"
    elif (
        "assertionerror" in lowered
        or "assertion failed" in lowered
        or re.search(r"(?m)^E\s+assert\b", decoded) is not None
    ):
        observed_failure = "assertion_failure"
        if not failed_tests:
            failed_tests = (intended_test,)

    return RedFailureObservation(
        exit_code=result.returncode,
        termination_reason=termination_reason,
        observed_failure=observed_failure,
        intended_test=intended_test,
        failed_tests=failed_tests,
    )


def execute_declared_phase(
    inputs: ValidatedInputs,
    *,
    phase: EvidencePhase,
    writer: OutputWriter,
    process_runner: FactoryProcessRunner,
    cancellation_token: CancellationToken,
    deadline: Deadline,
    sterile_home: Path,
    temp_dir: Path,
    boundary_check: Callable[[], None],
    git_runtime: FactoryGitRuntime,
    on_command_started: PhaseStartedCallback | None = None,
    on_command_completed: PhaseCompletedCallback | None = None,
    red_observation_factory: RedObservationFactory = observe_declared_red_failure,
) -> PhaseExecutionOutcome:
    """Run the declared commands once and return runner-owned phase items."""

    if phase not in _VALID_PHASES:
        raise ValueError(f"unsupported verification phase: {phase}")
    spec = inputs.run_spec
    child_environment = build_child_environment(
        allowed_keys=spec.policy.allowed_environment_keys,
        source_env=inputs.environment,
        sterile_home=sterile_home,
        temp_dir=temp_dir,
    )
    items: list[EvidenceItem] = []
    any_cancelled = False
    any_timed_out = False

    for index, command in enumerate(spec.policy.allowed_commands, start=1):
        declared_command = tuple(command)
        validate_declared_command(command, spec.policy.allowed_commands)
        resolved_command = resolve_trusted_command(
            command,
            environment=child_environment,
            prohibited_roots=(inputs.workspace, inputs.output_dir),
        )
        if on_command_started is not None:
            on_command_started(
                PhaseCommandStart(
                    phase=phase,
                    index=index,
                    command=declared_command,
                )
            )
        boundary_check()
        before = _repository_snapshot(git_runtime)
        started_at = utc_timestamp()
        started_monotonic = time.monotonic()
        try:
            output_snapshot = capture_output_tree(inputs.output_dir)
            try:
                result = process_runner.run(
                    resolved_command,
                    cwd=inputs.workspace,
                    environment=child_environment,
                    timeout_seconds=deadline.remaining_seconds(),
                )
            finally:
                try:
                    enforce_output_tree_unchanged(output_snapshot)
                except (OSError, ValueError) as exc:
                    raise FactoryPolicyViolation(
                        "verification command modified protocol output"
                    ) from exc
        finally:
            boundary_check()
        finished_at = utc_timestamp()
        duration = max(0.0, time.monotonic() - started_monotonic)
        after = _repository_snapshot(git_runtime)
        repository_changed = before != after
        if result.termination_reason == "cancelled":
            any_cancelled = True
        if result.termination_reason == "timed_out":
            any_timed_out = True

        stdout_reference = writer.write_bytes(
            f"evidence/objects/{phase}-command-{index:03d}.stdout",
            _captured_bytes(result.stdout_bytes, result.stdout),
            media_type="text/plain",
        )
        stderr_reference = writer.write_bytes(
            f"evidence/objects/{phase}-command-{index:03d}.stderr",
            _captured_bytes(result.stderr_bytes, result.stderr),
            media_type="text/plain",
        )
        evidence_termination_reason = _termination_reason(result)
        if phase == "red":
            observation = red_observation_factory(
                declared_command,
                index,
                result,
                repository_changed,
            )
            decision = classify_red_failure(observation)
            completion = PhaseCommandCompletion(
                phase=phase,
                index=index,
                command=declared_command,
                actual_status="failed",
                failure_classification=decision.failure_classification,
                exit_code=result.returncode,
                termination_reason=evidence_termination_reason,
            )
            if on_command_completed is not None:
                on_command_completed(completion)
            if not decision.accepted:
                raise FalseRedEvidenceError(decision.failure_classification)
            actual_status: Literal["passed", "failed"] = "failed"
            evidence_exit_code = result.returncode
            classification: FailureClassification = "expected_behavioral_failure"
            expected_status: Literal["passed", "failed"] = "failed"
        else:
            actual_status, evidence_exit_code, classification = _failure_details(
                termination_reason=evidence_termination_reason,
                returncode=result.returncode,
                repository_changed=repository_changed,
            )
            expected_status = "passed"

        item = EvidenceItem(
            phase=phase,
            command=declared_command,
            working_directory=".",
            environment_keys=tuple(spec.policy.allowed_environment_keys),
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            exit_code=evidence_exit_code,
            termination_reason=evidence_termination_reason,
            expected_status=expected_status,
            actual_status=actual_status,
            failure_classification=classification,
            stdout=stdout_reference,
            stderr=stderr_reference,
            test_reports=(),
            tool_versions={},
            repository_files_changed=repository_changed,
        )
        items.append(item)
        if phase != "red" and on_command_completed is not None:
            on_command_completed(
                PhaseCommandCompletion(
                    phase=phase,
                    index=index,
                    command=declared_command,
                    actual_status=actual_status,
                    failure_classification=classification,
                    exit_code=evidence_exit_code,
                    termination_reason=evidence_termination_reason,
                )
            )
        if phase == "red":
            break
        if actual_status != "passed":
            break
        if cancellation_token.cancelled or deadline.expired:
            any_cancelled = cancellation_token.cancelled
            any_timed_out = deadline.expired and not any_cancelled
            break

    if not items:
        raise ValueError(f"{phase} phase requires at least one declared command")
    boundary_check()

    passed = (
        items[0].failure_classification == "expected_behavioral_failure"
        if phase == "red"
        else all(item.actual_status == "passed" for item in items)
    )
    return PhaseExecutionOutcome(
        phase=phase,
        passed=passed,
        cancelled=any_cancelled,
        timed_out=any_timed_out,
        items=tuple(items),
    )


def finalize_authoring_evidence(
    inputs: ValidatedInputs,
    *,
    writer: OutputWriter,
    phase_outcomes: tuple[PhaseExecutionOutcome, ...],
    advisory_observations: tuple[str, ...] = (),
) -> VerificationOutcome:
    """Write the single author evidence document after all four phases."""

    if inputs.run_spec.operation != "author":
        raise EvidenceSufficiencyError(
            "authoring evidence requires an author operation"
        )
    items: list[EvidenceItem] = []
    for outcome in phase_outcomes:
        if outcome.cancelled or outcome.timed_out or not outcome.passed:
            raise EvidenceSufficiencyError(
                f"{outcome.phase} phase did not complete as expected"
            )
        if any(item.phase != outcome.phase for item in outcome.items):
            raise EvidenceSufficiencyError(
                f"{outcome.phase} outcome contains a mismatched evidence phase"
            )
        items.extend(outcome.items)
    evidence = build_authoring_evidence(
        created_at=utc_timestamp(),
        identity=inputs.run_spec.identity,
        repository=inputs.run_spec.repository,
        runner=RunnerBuildIdentity(
            version=__version__,
            image=None,
            source_commit=None,
        ),
        context_digest=inputs.context_digest,
        items=tuple(items),
        advisory_observations=advisory_observations,
    )
    reference = writer.write_json(
        "evidence/verification-evidence.json",
        evidence.model_dump(mode="json"),
    )
    return VerificationOutcome(
        passed=True,
        cancelled=False,
        timed_out=False,
        evidence=evidence,
        reference=reference,
    )


def execute_verification(
    inputs: ValidatedInputs,
    *,
    writer: OutputWriter,
    process_runner: FactoryProcessRunner,
    cancellation_token: CancellationToken,
    deadline: Deadline,
    sterile_home: Path,
    temp_dir: Path,
    clean_verification: bool,
    boundary_check: Callable[[], None],
    git_runtime: FactoryGitRuntime,
    on_command_started: PhaseStartedCallback | None = None,
    on_command_completed: PhaseCompletedCallback | None = None,
) -> VerificationOutcome:
    """Compatibility entry point for one deterministic verification phase."""

    spec = inputs.run_spec
    phase_outcome = execute_declared_phase(
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
        on_command_started=on_command_started,
        on_command_completed=on_command_completed,
    )
    overall_status = "passed" if phase_outcome.passed else "failed"
    change_set_digest = (
        inputs.change_set.change_set_digest
        if clean_verification and inputs.change_set is not None
        else None
    )
    payload = {
        "protocol": "factory-runner-protocol/v1",
        "schema": "verification-evidence/v1",
        "schema_version": 1,
        "created_at": utc_timestamp(),
        "identity": spec.identity.model_dump(mode="json"),
        "repository": spec.repository.model_dump(mode="json"),
        "environment_kind": (
            "clean_verification" if clean_verification else "authoring"
        ),
        "runner": {
            "version": __version__,
            "image": None,
            "source_commit": None,
        },
        "context_digest": inputs.context_digest,
        "change_set_digest": change_set_digest,
        "items": [item.model_dump(mode="json") for item in phase_outcome.items],
        "overall_status": overall_status,
        "advisory_observations": [],
        "evidence_set_digest": EMPTY_DIGEST,
    }
    payload["evidence_set_digest"] = contract_document_digest(payload)
    evidence = VerificationEvidence.model_validate(payload)
    reference = writer.write_json(
        "evidence/verification-evidence.json",
        evidence.model_dump(mode="json"),
    )
    return VerificationOutcome(
        passed=overall_status == "passed",
        cancelled=phase_outcome.cancelled,
        timed_out=phase_outcome.timed_out,
        evidence=evidence,
        reference=reference,
    )


__all__ = [
    "FalseRedEvidenceError",
    "PhaseCommandCompletion",
    "PhaseCommandStart",
    "PhaseExecutionOutcome",
    "VerificationOutcome",
    "execute_declared_phase",
    "execute_verification",
    "finalize_authoring_evidence",
    "observe_declared_red_failure",
]

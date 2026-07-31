"""Deterministic phase execution and v1 verification evidence materialisation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Literal, cast

from ai_native import __version__
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    ENVIRONMENT_KEY_PATTERN,
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
    phase_outcome: PhaseExecutionOutcome | None = None


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
class PhaseEvidenceAuthority:
    """Non-executable provenance for the policy that produced phase evidence."""

    allowed_commands: tuple[tuple[str, ...], ...]
    allowed_environment_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_commands, tuple)
            or len(set(self.allowed_commands)) != len(self.allowed_commands)
            or any(
                not isinstance(command, tuple)
                or not command
                or any(
                    not isinstance(argument, str)
                    or not argument
                    or len(argument) > 16_384
                    or "\x00" in argument
                    for argument in command
                )
                for command in self.allowed_commands
            )
            or not isinstance(self.allowed_environment_keys, tuple)
            or len(set(self.allowed_environment_keys))
            != len(self.allowed_environment_keys)
            or any(
                not isinstance(key, str)
                or len(key) > 256
                or re.fullmatch(ENVIRONMENT_KEY_PATTERN, key) is None
                for key in self.allowed_environment_keys
            )
        ):
            raise ValueError("phase evidence authority is invalid")


def _phase_evidence_authority(inputs: ValidatedInputs) -> PhaseEvidenceAuthority:
    policy = inputs.run_spec.policy
    return PhaseEvidenceAuthority(
        allowed_commands=tuple(tuple(command) for command in policy.allowed_commands),
        allowed_environment_keys=tuple(policy.allowed_environment_keys),
    )


@dataclass(frozen=True, slots=True)
class PhaseExecutionOutcome:
    """Runner-owned evidence items produced by one named TDD phase."""

    phase: EvidencePhase
    passed: bool
    cancelled: bool
    timed_out: bool
    items: tuple[EvidenceItem, ...]
    evidence_authority: PhaseEvidenceAuthority | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class RedAlreadyGreenObservation:
    """A successful pre-authoring probe that proves explicit no-change."""

    command: tuple[str, ...]
    environment_keys: tuple[str, ...]
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout: ArtifactReference
    stderr: ArtifactReference
    evidence_authority: PhaseEvidenceAuthority | None = field(
        default=None,
        compare=False,
        repr=False,
    )


class FalseRedEvidenceError(EvidenceSufficiencyError):
    """The observed failing command cannot prove the intended missing behaviour."""

    def __init__(self, failure_classification: FailureClassification) -> None:
        self.failure_classification = failure_classification
        super().__init__(
            "red phase did not prove the intended behavioral failure "
            f"({failure_classification})"
        )


class RedAlreadyGreen(EvidenceSufficiencyError):
    """The declared behavior already passes before authoring begins."""

    def __init__(self, observation: RedAlreadyGreenObservation) -> None:
        if not isinstance(observation, RedAlreadyGreenObservation):
            raise ValueError("already-green observation is invalid")
        self.observation = observation
        super().__init__("red probe found the declared behavior already satisfied")


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
    evidence_authority = _phase_evidence_authority(inputs)
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
            if (
                result.termination_reason == "exited"
                and result.returncode == 0
                and not repository_changed
                and not result.stdout_truncated
                and not result.stderr_truncated
            ):
                if on_command_completed is not None:
                    on_command_completed(
                        PhaseCommandCompletion(
                            phase=phase,
                            index=index,
                            command=declared_command,
                            actual_status="passed",
                            failure_classification="none",
                            exit_code=0,
                            termination_reason="exited",
                        )
                    )
                raise RedAlreadyGreen(
                    RedAlreadyGreenObservation(
                        command=declared_command,
                        environment_keys=tuple(spec.policy.allowed_environment_keys),
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration,
                        stdout=stdout_reference,
                        stderr=stderr_reference,
                        evidence_authority=evidence_authority,
                    )
                )
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
        evidence_authority=evidence_authority,
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
    return finalize_verification_evidence(
        inputs,
        writer=writer,
        phase_outcome=phase_outcome,
        clean_verification=clean_verification,
    )


def finalize_verification_evidence(
    inputs: ValidatedInputs,
    *,
    writer: OutputWriter,
    phase_outcome: PhaseExecutionOutcome,
    clean_verification: bool,
) -> VerificationOutcome:
    """Materialise verification evidence from one completed or restored phase."""

    if (
        not isinstance(phase_outcome, PhaseExecutionOutcome)
        or phase_outcome.phase != "verification"
        or not phase_outcome.items
    ):
        raise EvidenceSufficiencyError(
            "verification evidence requires one verification phase outcome"
        )
    spec = inputs.run_spec
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
        phase_outcome=phase_outcome,
    )


__all__ = [
    "FalseRedEvidenceError",
    "PhaseCommandCompletion",
    "PhaseCommandStart",
    "PhaseEvidenceAuthority",
    "PhaseExecutionOutcome",
    "RedAlreadyGreenObservation",
    "RedAlreadyGreen",
    "VerificationOutcome",
    "execute_declared_phase",
    "execute_verification",
    "finalize_authoring_evidence",
    "finalize_verification_evidence",
    "observe_declared_red_failure",
]

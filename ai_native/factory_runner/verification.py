"""Deterministic command execution and minimal v1 evidence materialisation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Literal

from ai_native import __version__
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.verification_evidence import (
    EvidenceItem,
    VerificationEvidence,
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
    if returncode not in (None, 0):
        return "failed", returncode, "test_failure"
    if repository_changed:
        return "failed", 1, "assertion_failure"
    return "passed", 0, "none"


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
) -> VerificationOutcome:
    """Run exactly the declared commands and write genuine v1 evidence."""

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
        validate_declared_command(command, spec.policy.allowed_commands)
        resolved_command = resolve_trusted_command(
            command,
            environment=child_environment,
            prohibited_roots=(inputs.workspace, inputs.output_dir),
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
        actual_status, evidence_exit_code, classification = _failure_details(
            termination_reason=result.termination_reason,
            returncode=result.returncode,
            repository_changed=repository_changed,
        )
        if result.termination_reason == "cancelled":
            any_cancelled = True
        if result.termination_reason == "timed_out":
            any_timed_out = True

        stdout_reference = writer.write_bytes(
            f"evidence/objects/command-{index:03d}.stdout",
            result.stdout.encode("utf-8", errors="replace"),
            media_type="text/plain",
        )
        stderr_reference = writer.write_bytes(
            f"evidence/objects/command-{index:03d}.stderr",
            result.stderr.encode("utf-8", errors="replace"),
            media_type="text/plain",
        )
        item = EvidenceItem(
            phase="verification",
            command=tuple(command),
            working_directory=".",
            environment_keys=tuple(spec.policy.allowed_environment_keys),
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            exit_code=evidence_exit_code,
            termination_reason=(
                result.termination_reason
                if result.termination_reason in {"exited", "timed_out", "cancelled"}
                else "signalled"
            ),
            expected_status="passed",
            actual_status=actual_status,
            failure_classification=classification,
            stdout=stdout_reference,
            stderr=stderr_reference,
            test_reports=(),
            tool_versions={},
            repository_files_changed=repository_changed,
        )
        items.append(item)
        if actual_status != "passed":
            break
        if cancellation_token.cancelled or deadline.expired:
            any_cancelled = cancellation_token.cancelled
            any_timed_out = deadline.expired and not any_cancelled
            break

    if not items:
        raise ValueError("verification requires at least one declared command")
    boundary_check()

    overall_status = (
        "failed" if any(item.actual_status == "failed" for item in items) else "passed"
    )
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
        "items": [item.model_dump(mode="json") for item in items],
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
        cancelled=any_cancelled,
        timed_out=any_timed_out,
        evidence=evidence,
        reference=reference,
    )


__all__ = ["VerificationOutcome", "execute_verification"]

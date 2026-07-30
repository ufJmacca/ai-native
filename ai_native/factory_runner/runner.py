"""Coordinator for bounded non-interactive factory author and verify runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import shutil
import tempfile
from typing import Any, Literal

from ai_native.factory_runner.admission import (
    FactoryAdmissionError,
    admit_inputs,
    validate_workspace,
)
from ai_native.factory_runner.author import (
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
    restore_clean_author_workspace,
    validate_author_boundary,
)
from ai_native.factory_runner.contracts.run_spec import RunSpec
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
from ai_native.factory_runner.verification import execute_verification


Operation = Literal["author", "verify"]
_GATEWAY_ONLY_ENVIRONMENT_KEYS = ("ATTEMPT_GATEWAY_TOKEN_FILE",)

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
) -> tuple[OutputWriter, str] | None:
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
        writer = OutputWriter(validated_output)
        event_reference = writer.write_events_placeholder()
    except (OSError, ValueError):
        return None
    return writer, event_reference.digest


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
    change_set: Any | None = None,
    verification_evidence: Any | None = None,
    event_stream_digest: str,
) -> int:
    finished_at = utc_timestamp()
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
        change_set=change_set,
        verification_evidence=verification_evidence,
        event_stream_digest=event_stream_digest,
    )
    writer.write_completion(result=result, result_reference=reference)
    return _EXIT_BY_REASON[reason_code]


def execute_factory(
    *,
    expected_operation: Operation,
    run_spec_path: Path,
    output_dir: Path,
    environment: Mapping[str, str],
    cancellation_token: CancellationToken,
    log: Callable[[str], None],
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
        writer, event_digest = failure_output
        outcome = "policy_denied" if reason_code == "policy_denied" else "invalid_input"
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome=outcome,
            reason_code=reason_code,
            message="Factory input admission failed.",
            started_at=started_at,
            spec=candidate_spec,
            event_stream_digest=event_digest,
        )
    except Exception:
        log("[factory] admission failed")
        failure_output = _safe_failure_output(
            output_dir=output_dir,
            candidate_spec=_candidate_run_spec(run_spec_path),
        )
        if failure_output is None:
            return _EXIT_BY_REASON["invalid_input"]
        writer, event_digest = failure_output
        return _finish(
            writer=writer,
            operation=expected_operation,
            outcome="invalid_input",
            reason_code="invalid_input",
            message="Factory input admission failed.",
            started_at=started_at,
            spec=None,
            event_stream_digest=event_digest,
        )

    try:
        validated_output = validate_output_root(inputs.output_dir)
        writer = OutputWriter(validated_output)
        event_reference = writer.write_events_placeholder()
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
                event_stream_digest=event_reference.digest,
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
                    event_stream_digest=event_reference.digest,
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
                event_stream_digest=event_reference.digest,
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
        )
        completed_stages = author.completed_stages
        if cancellation_token.cancelled:
            raise _Cancelled
        if deadline.expired:
            raise _TimedOut

        verification = execute_verification(
            inputs,
            writer=writer,
            process_runner=process_runner,
            cancellation_token=cancellation_token,
            deadline=deadline,
            sterile_home=sterile_home,
            temp_dir=temp_dir,
            clean_verification=False,
            boundary_check=boundary_check,
            git_runtime=git_runtime,
        )
        if verification.cancelled:
            raise _Cancelled
        if verification.timed_out:
            raise _TimedOut
        if not verification.passed:
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="failed",
                reason_code="verification_failed",
                message="A declared authoring verification command failed.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=completed_stages,
                event_stream_digest=event_reference.digest,
            )

        change_set, change_reference = build_change_set(
            inputs,
            writer=writer,
            git_runtime=git_runtime,
            evidence=verification.evidence,
            evidence_reference=verification.reference,
        )
        if change_set is None or change_reference is None:
            return _finish(
                writer=writer,
                operation=expected_operation,
                outcome="no_change",
                reason_code="completed",
                message="Authoring completed without repository changes.",
                started_at=started_at,
                spec=inputs.run_spec,
                completed_stages=completed_stages,
                event_stream_digest=event_reference.digest,
            )
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
        )
    except (FactoryPolicyViolation, ChangePolicyError, FactoryGitError):
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
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
            event_stream_digest=event_reference.digest,
        )
    finally:
        if private_environment_root is not None:
            shutil.rmtree(private_environment_root, ignore_errors=True)


class _Cancelled(RuntimeError):
    pass


class _TimedOut(RuntimeError):
    pass


__all__ = ["execute_factory"]

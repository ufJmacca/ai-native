from __future__ import annotations

from collections.abc import Callable
import json
import sys

from ai_native.factory_runner.protocol import validate_contract
from tests.factory_runner.integration._support import (
    AUTHORED_APP,
    FactoryInvocation,
    assert_valid_completion,
    git_output,
    git_status,
    invoke_factory,
    load_valid_result,
    load_valid_verification_evidence,
)


def test_factory_verify_runs_declared_command_without_agent_authoring(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(
        operation="verify",
        verification_passes=True,
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert not invocation.marker_path.exists()
    assert (invocation.workspace / "app.py").read_text(encoding="utf-8") == AUTHORED_APP
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == " M app.py"

    result = load_valid_result(invocation)
    assert result.operation == "verify"
    assert result.outcome == "succeeded"
    assert result.completed_stages == ("verify",)
    assert result.change_set is None
    assert result.verification_evidence is not None
    evidence = load_valid_verification_evidence(invocation, result)
    assert evidence.environment_kind == "clean_verification"
    assert evidence.overall_status == "passed"
    assert_valid_completion(invocation, result)


def test_factory_verify_reports_deterministic_command_failure_without_authoring(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(
        operation="verify",
        verification_passes=False,
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 6, completed.stderr
    assert completed.stdout == ""
    assert not invocation.marker_path.exists()
    assert (invocation.workspace / "app.py").read_text(encoding="utf-8") == AUTHORED_APP
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == " M app.py"

    result = load_valid_result(invocation)
    assert result.operation == "verify"
    assert result.outcome == "failed"
    assert result.reason_code == "verification_failed"
    assert result.change_set is None


def test_factory_verify_repairs_and_rejects_protocol_output_tampering(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="verify")
    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    events_path = invocation.output_dir / "events.ndjson"
    run_spec["policy"]["allowed_commands"] = [
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from pathlib import Path; "
                f"Path({str(events_path)!r}).write_text('tampered')"
            ),
        ]
    ]
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 3, completed.stderr
    events = tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in events_path.read_bytes().splitlines()
    )
    assert tuple(event.sequence for event in events) == tuple(range(1, len(events) + 1))
    assert events[-1].event_type == "RunnerFailed"
    result = load_valid_result(invocation)
    assert result.outcome == "policy_denied"
    assert result.reason_code == "policy_denied"

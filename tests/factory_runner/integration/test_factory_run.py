from __future__ import annotations

from collections.abc import Callable
import json
import subprocess
import sys
import time

from ai_native.factory_runner.protocol import validate_contract
from tests.factory_runner.integration._support import (
    AUTHORED_APP,
    FactoryInvocation,
    REPOSITORY_ROOT,
    assert_valid_completion,
    factory_command,
    factory_environment,
    git_output,
    git_status,
    invoke_factory,
    load_valid_change_set,
    load_valid_result,
)


def test_factory_author_runs_unattended_without_committing(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert invocation.marker_path.exists()
    assert (invocation.workspace / "app.py").read_text(encoding="utf-8") == AUTHORED_APP
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == " M app.py"

    result = load_valid_result(invocation)
    assert result.operation == "author"
    assert result.outcome == "succeeded"
    assert result.identity is not None
    assert result.identity.attempt_id == "attempt-an-02"
    assert result.repository is not None
    assert result.repository.base_commit_sha == invocation.base_sha
    assert result.change_set is not None
    assert result.verification_evidence is None
    change_set = load_valid_change_set(invocation, result)
    assert [
        criterion.model_dump(mode="json")
        for criterion in change_set.acceptance_criteria_results
    ] == [
        {
            "criterion": "greeting('Codex') returns 'Hello, Codex!'",
            "status": "not_run",
        }
    ]
    assert_valid_completion(invocation, result)


def test_factory_author_first_prompt_contains_all_admitted_context(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(
        invocation,
        agent_mode="assert-first-prompt-context",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "succeeded"


def test_factory_author_blocks_missing_acceptance_criteria_without_prompting(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(
        operation="author",
        acceptance_criteria=[],
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 4, completed.stderr
    assert completed.stdout == ""
    assert not invocation.marker_path.exists()
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == ""

    result = load_valid_result(invocation)
    assert result.operation == "author"
    assert result.outcome == "blocked"
    assert result.reason_code == "missing_requirements"
    assert result.change_set is None
    assert result.verification_evidence is None


def test_factory_author_maps_stage_clarification_to_blocked_without_prompting(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="blocked")

    assert completed.returncode == 4, completed.stderr
    assert completed.stdout == ""
    assert invocation.marker_path.exists()
    assert git_status(invocation.workspace) == ""
    result = load_valid_result(invocation)
    assert result.outcome == "blocked"
    assert result.reason_code == "missing_requirements"


def test_factory_author_maps_sigterm_during_agent_work_to_cancelled(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    process = subprocess.Popen(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=factory_environment(invocation, agent_mode="sleep"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    marker_deadline = time.monotonic() + 10
    while not invocation.marker_path.exists() and time.monotonic() < marker_deadline:
        time.sleep(0.01)
    assert invocation.marker_path.exists()

    process.terminate()
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 8, stderr
    assert stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "cancelled"
    assert result.reason_code == "cancelled"


def test_factory_author_enforces_one_shared_wall_deadline(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    run_spec["policy"]["max_wall_seconds"] = 1
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = invoke_factory(invocation, agent_mode="sleep")

    assert completed.returncode == 9, completed.stderr
    assert completed.stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "timed_out"
    assert result.reason_code == "timed_out"


def test_factory_author_rejects_git_security_metadata_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="mutate-git-config")

    assert completed.returncode == 3, completed.stderr
    assert completed.stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "policy_denied"
    assert result.reason_code == "policy_denied"


def test_factory_author_preflights_command_executables_before_agent_work(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    run_spec["policy"]["allowed_commands"] = [["/factory/does-not-exist", "--check"]]
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 3, completed.stderr
    assert not invocation.marker_path.exists()
    assert git_status(invocation.workspace) == ""
    result = load_valid_result(invocation)
    assert result.outcome == "policy_denied"
    assert result.reason_code == "policy_denied"


def test_factory_author_repairs_and_rejects_protocol_output_tampering(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
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

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 3, completed.stderr
    events = tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in events_path.read_bytes().splitlines()
    )
    assert tuple(event.sequence for event in events) == tuple(
        range(1, len(events) + 1)
    )
    assert events[-1].event_type == "RunnerFailed"
    result = load_valid_result(invocation)
    assert result.outcome == "policy_denied"
    assert result.reason_code == "policy_denied"

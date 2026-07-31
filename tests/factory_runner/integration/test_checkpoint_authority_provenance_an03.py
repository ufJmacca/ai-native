from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import shutil
import subprocess

import pytest

from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.run_result import RunResult
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.protocol import (
    sha256_digest,
    validate_contract,
    verify_contract_digest,
)
from tests.factory_runner.integration._support import (
    FactoryInvocation,
    invoke_factory,
    load_valid_result,
)


def _checkpoint(
    invocation: FactoryInvocation,
    result: RunResult,
) -> Checkpoint:
    reference = result.latest_checkpoint
    assert reference is not None
    content = (invocation.output_dir / reference.path).read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    checkpoint = validate_contract(
        content,
        expected_schema="checkpoint/v1",
    )
    assert isinstance(checkpoint, Checkpoint)
    verify_contract_digest(checkpoint)
    return checkpoint


def _events(invocation: FactoryInvocation) -> tuple[RunnerEvent, ...]:
    events = tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in (invocation.output_dir / "events.ndjson").read_bytes().splitlines()
    )
    assert all(isinstance(event, RunnerEvent) for event in events)
    return events  # type: ignore[return-value]


def _reset_workspace(invocation: FactoryInvocation) -> None:
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=invocation.workspace,
        check=True,
        capture_output=True,
    )


def _replacement(
    invocation: FactoryInvocation,
    result: RunResult,
    checkpoint: Checkpoint,
    *,
    label: str,
) -> FactoryInvocation:
    reference = result.latest_checkpoint
    assert reference is not None
    resume_root = invocation.input_dir / "resume"
    shutil.copytree(
        invocation.output_dir / "checkpoints",
        resume_root / "checkpoints",
        dirs_exist_ok=True,
    )
    output_dir = invocation.output_dir.with_name(f"output-{label}")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    payload["identity"]["attempt_id"] = f"attempt-an-03-{label}"
    payload["resume"] = {
        "checkpoint_path": str((resume_root / reference.path).resolve()),
        "expected_digest": checkpoint.checkpoint_digest,
    }
    payload["outputs"]["output_dir"] = str(output_dir.resolve())
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return replace(
        invocation,
        output_dir=output_dir,
        marker_path=invocation.marker_path.with_name(f"agent-calls-{label}.log"),
    )


def _assert_no_replay(invocation: FactoryInvocation) -> None:
    event_types = tuple(event.event_type for event in _events(invocation))
    assert event_types.count("CheckpointRestored") == 1
    assert "StageStarted" not in event_types
    assert "TestStarted" not in event_types
    assert not invocation.marker_path.exists()


@pytest.mark.parametrize(
    ("narrow_commands", "narrow_environment"),
    (
        (True, False),
        (False, True),
    ),
    ids=("allowed-commands", "allowed-environment-keys"),
)
def test_chained_resume_preserves_historical_evidence_authority_after_narrowing(
    factory_invocation: Callable[..., FactoryInvocation],
    narrow_commands: bool,
    narrow_environment: bool,
) -> None:
    source = factory_invocation(operation="author")
    source_payload = json.loads(source.run_spec_path.read_text(encoding="utf-8"))
    first_command = list(source_payload["policy"]["allowed_commands"][0])
    second_command = [*first_command[:-1], first_command[-1] + "; pass"]
    source_payload["policy"]["allowed_commands"] = [
        first_command,
        second_command,
    ]
    source_payload["policy"]["allowed_environment_keys"] = ["PATH"]
    source.run_spec_path.write_text(
        json.dumps(source_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    first_completed = invoke_factory(source, agent_mode="author")

    assert first_completed.returncode == 0, first_completed.stderr
    first_result = load_valid_result(source)
    first_checkpoint = _checkpoint(source, first_result)
    _reset_workspace(source)
    second = _replacement(
        source,
        first_result,
        first_checkpoint,
        label=("narrow-commands-b" if narrow_commands else "narrow-environment-b"),
    )
    second_payload = json.loads(second.run_spec_path.read_text(encoding="utf-8"))
    if narrow_commands:
        second_payload["policy"]["allowed_commands"] = [first_command]
    if narrow_environment:
        second_payload["policy"]["allowed_environment_keys"] = []
    second.run_spec_path.write_text(
        json.dumps(second_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    second_completed = invoke_factory(second, agent_mode="fail-if-called")

    assert second_completed.returncode == 0, second_completed.stderr
    second_result = load_valid_result(second)
    second_checkpoint = _checkpoint(second, second_result)
    expected_commands = (
        (tuple(first_command),)
        if narrow_commands
        else (tuple(first_command), tuple(second_command))
    )
    expected_environment = () if narrow_environment else ("PATH",)
    assert second_checkpoint.authority.allowed_commands == expected_commands
    assert second_checkpoint.authority.allowed_environment_keys == expected_environment
    _assert_no_replay(second)

    _reset_workspace(second)
    third = _replacement(
        second,
        second_result,
        second_checkpoint,
        label=("narrow-commands-c" if narrow_commands else "narrow-environment-c"),
    )

    third_completed = invoke_factory(third, agent_mode="fail-if-called")

    assert third_completed.returncode == 0, third_completed.stderr
    third_result = load_valid_result(third)
    third_checkpoint = _checkpoint(third, third_result)
    assert third_result.reason_code == "completed"
    assert third_checkpoint.authority.allowed_commands == expected_commands
    assert third_checkpoint.authority.allowed_environment_keys == expected_environment
    _assert_no_replay(third)

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import time

from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.protocol import (
    sha256_digest,
    validate_contract,
    verify_contract_digest,
)
from tests.factory_runner.admission._fixtures import filesystem_snapshot
from tests.factory_runner.integration._support import (
    AUTHORED_APP,
    FactoryInvocation,
    REPOSITORY_ROOT,
    factory_command,
    factory_environment,
    invoke_factory,
    load_valid_result,
)


def _events(invocation: FactoryInvocation) -> tuple[RunnerEvent, ...]:
    return tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in (invocation.output_dir / "events.ndjson").read_bytes().splitlines()
    )


def _checkpoint(invocation: FactoryInvocation, result: object) -> Checkpoint:
    reference = result.latest_checkpoint
    assert reference is not None
    content = (invocation.output_dir / reference.path).read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    model = validate_contract(content, expected_schema="checkpoint/v1")
    assert isinstance(model, Checkpoint)
    verify_contract_digest(model)
    for artifact in model.artifact_manifest:
        payload = (invocation.output_dir / artifact.path).read_bytes()
        assert len(payload) == artifact.byte_size
        assert sha256_digest(payload) == artifact.digest
    return model


def _cancel_after_loop(
    factory_invocation: Callable[..., FactoryInvocation],
) -> tuple[FactoryInvocation, object, Checkpoint]:
    invocation = factory_invocation(operation="author")
    pause_marker = invocation.marker_path.with_name("verification-agent.started")
    process = subprocess.Popen(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=factory_environment(
            invocation,
            agent_mode="author-pause-verify",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not pause_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pause_marker.exists()
    process.terminate()
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 8, stderr
    assert stdout == ""

    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    events = _events(invocation)
    event_types = tuple(event.event_type for event in events)
    cancellation = event_types.index("RunnerCancellationRequested")
    checkpoint_written = max(
        index
        for index, event_type in enumerate(event_types)
        if event_type == "CheckpointWritten"
    )
    assert cancellation < checkpoint_written
    assert events[checkpoint_written].artifact_refs == (result.latest_checkpoint,)
    assert result.outcome == "cancelled"
    assert checkpoint.completed_stages[-1] == "loop"
    assert checkpoint.next_permitted_stage == "verify"
    assert checkpoint.workspace_patch_digest is not None
    return invocation, result, checkpoint


def _replacement(
    invocation: FactoryInvocation,
    result: object,
    checkpoint: Checkpoint,
    *,
    label: str,
) -> FactoryInvocation:
    resume_root = invocation.input_dir / "resume"
    shutil.copytree(
        invocation.output_dir / "checkpoints",
        resume_root / "checkpoints",
    )
    reference = result.latest_checkpoint
    assert reference is not None
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


def _reset_workspace(invocation: FactoryInvocation) -> None:
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=invocation.workspace,
        check=True,
        capture_output=True,
    )


def test_successful_author_result_references_a_valid_latest_checkpoint(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    writes = [
        event for event in _events(invocation)
        if event.event_type == "CheckpointWritten"
    ]
    assert writes[-1].artifact_refs == (result.latest_checkpoint,)
    assert checkpoint.completed_stages == result.completed_stages
    assert checkpoint.next_permitted_stage is None


def test_replacement_restores_and_continues_without_replaying_stages(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    assert (original.workspace / "app.py").read_text() != AUTHORED_APP
    resumed = _replacement(original, cancelled, stored, label="resumed")

    completed = invoke_factory(resumed, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(resumed)
    continued = _checkpoint(resumed, result)
    events = _events(resumed)
    started = tuple(
        event.sanitised_payload["stage"]
        for event in events
        if event.event_type == "StageStarted"
    )
    assert tuple(event.event_type for event in events).index(
        "CheckpointRestored"
    ) < tuple(event.event_type for event in events).index("StageStarted")
    assert started == ("verify",)
    assert (resumed.workspace / "app.py").read_text() == AUTHORED_APP
    assert continued.sequence > stored.sequence
    assert continued.completed_stages == (*stored.completed_stages, "verify")


def test_corrupt_resume_is_rejected_before_workspace_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    resumed = _replacement(original, cancelled, stored, label="corrupt")
    patch = next(
        artifact
        for artifact in stored.artifact_manifest
        if artifact.digest == stored.workspace_patch_digest
    )
    object_path = resumed.input_dir / "resume" / patch.path
    object_path.write_bytes(b"x" * object_path.stat().st_size)
    before = filesystem_snapshot(resumed.workspace)

    completed = invoke_factory(resumed, agent_mode="fail-if-called")

    assert completed.returncode == 5, completed.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before

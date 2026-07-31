from __future__ import annotations

from collections.abc import Callable
import json

from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.protocol import validate_contract
from tests.factory_runner.integration._support import (
    FactoryInvocation,
    invoke_factory,
    load_valid_change_set,
    load_valid_result,
)


def _events(invocation: FactoryInvocation) -> tuple[RunnerEvent, ...]:
    content = (invocation.output_dir / "events.ndjson").read_bytes()
    return tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in content.splitlines()
    )


def _artifact_bytes(
    invocation: FactoryInvocation,
    payload: dict[str, object],
) -> bytes:
    reference = ArtifactReference.model_validate(payload)
    content = (invocation.output_dir / reference.path).read_bytes()
    assert reference.byte_size == len(content)
    assert reference.digest == sha256_digest(content)
    return content


def test_completed_author_has_ordered_events_and_an_acyclic_manifest_chain(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    event_bytes = (invocation.output_dir / "events.ndjson").read_bytes()
    events = _events(invocation)
    event_types = tuple(event.event_type for event in events)
    assert tuple(event.sequence for event in events) == tuple(
        range(1, len(events) + 1)
    )
    assert event_types[:2] == ("RunnerStarted", "InputValidated")
    assert event_types[-1] == "RunnerCompleted"
    assert "StageStarted" in event_types
    assert "StageCompleted" in event_types
    assert "VerificationEvidenceWritten" in event_types
    assert "ChangeSetWritten" in event_types
    assert result.event_stream_digest == sha256_digest(event_bytes)

    manifest_bytes = (
        invocation.output_dir / "protocol-manifest.json"
    ).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert result.output_manifest_digest == sha256_digest(manifest_bytes)
    manifest_paths = tuple(
        entry["path"] for entry in manifest["artifacts"]
    )
    assert manifest_paths == tuple(sorted(manifest_paths))
    assert "events.ndjson" in manifest_paths
    assert "changeset/change.patch" in manifest_paths
    assert "changeset/change-set.json" in manifest_paths
    assert "result/run-result.json" not in manifest_paths
    assert "completion.json" not in manifest_paths

    completion = json.loads(
        (invocation.output_dir / "completion.json").read_bytes()
    )
    assert _artifact_bytes(invocation, completion["protocol_manifest"]) == (
        manifest_bytes
    )
    _artifact_bytes(invocation, completion["run_result"])


def test_stdout_event_stream_is_byte_identical_when_negotiated(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="verify")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    payload["outputs"]["stream_events_to_stdout"] = True
    payload["capabilities"]["optional"] = ["structured-events"]
    invocation.run_spec_path.write_text(json.dumps(payload), encoding="utf-8")

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 0, completed.stderr
    event_bytes = (invocation.output_dir / "events.ndjson").read_bytes()
    assert completed.stdout.encode() == event_bytes
    assert tuple(event.event_type for event in _events(invocation))[-1] == (
        "RunnerCompleted"
    )


def test_author_changeset_binds_complete_runner_owned_tdd_evidence(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    change_set = load_valid_change_set(invocation, result)
    evidence_reference = change_set.evidence_refs[0]
    evidence = validate_contract(
        (invocation.output_dir / evidence_reference.path).read_bytes(),
        expected_schema="verification-evidence/v1",
    )
    assert evidence.environment_kind == "authoring"
    assert tuple(item.phase for item in evidence.items) == (
        "red",
        "green",
        "refactor",
        "verification",
    )
    assert evidence.items[0].failure_classification == (
        "expected_behavioral_failure"
    )
    assert evidence.overall_status == "passed"


def test_terminal_failure_uses_runner_failed_event_and_still_completes(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(
        operation="verify",
        verification_passes=False,
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 6, completed.stderr
    assert tuple(event.event_type for event in _events(invocation))[-1] == (
        "RunnerFailed"
    )
    assert (invocation.output_dir / "completion.json").is_file()

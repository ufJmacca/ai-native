from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

import pytest

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.common import (
    RepositoryIdentity,
    RunIdentity,
)
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.events import EventSink
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root


PROTOCOL = "factory-runner-protocol/v1"
TIMESTAMP = "2026-07-31T00:00:00Z"


def _event(
    sequence: int,
    event_type: str,
    *,
    payload: dict[str, object] | None = None,
) -> RunnerEvent:
    return RunnerEvent.model_validate(
        {
            "protocol": PROTOCOL,
            "schema": "runner-event/v1",
            "schema_version": 1,
            "run_id": "run-an-03",
            "attempt_id": "attempt-an-03",
            "sequence": sequence,
            "timestamp": TIMESTAMP,
            "event_type": event_type,
            "correlation_id": "correlation-an-03",
            "causation_id": None,
            "sanitised_payload": payload or {},
            "artifact_refs": [],
        }
    )


def _writer(tmp_path: Path) -> OutputWriter:
    return OutputWriter(validate_output_root(tmp_path / "output"))


def _event_bytes(*events: RunnerEvent) -> bytes:
    return b"".join(
        canonical_json_bytes(event.model_dump(mode="json")) + b"\n"
        for event in events
    )


def _write_no_change_result(
    writer: OutputWriter,
    *,
    event_stream_digest: str,
):
    return writer.write_run_result(
        operation="author",
        outcome="no_change",
        reason_code="completed",
        message="The AN-03 event fixture completed without changes.",
        started_at=TIMESTAMP,
        finished_at=TIMESTAMP,
        identity=RunIdentity(
            work_item_id="work-item-an-03",
            work_item_revision_id="revision-an-03",
            delivery_phase_id="AN-03",
            run_id="run-an-03",
            attempt_id="attempt-an-03",
            correlation_id="correlation-an-03",
        ),
        repository=RepositoryIdentity(
            repository_id="fixture-repository",
            display_name="fixture/target-repository",
            base_commit_sha="a" * 40,
        ),
        completed_stages=(),
        event_stream_digest=event_stream_digest,
    )


def test_event_sink_writes_ordered_canonical_runner_event_ndjson(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    sink = EventSink(writer=writer)
    started = _event(1, "RunnerStarted")
    validated = _event(2, "InputValidated", payload={"operation": "author"})

    sink.append(started)
    sink.append(validated)
    reference = sink.finalize()

    content = (writer.root / reference.path).read_bytes()
    assert reference.path == "events.ndjson"
    assert reference.media_type == "application/x-ndjson"
    assert content == _event_bytes(started, validated)
    decoded = [
        RunnerEvent.model_validate(json.loads(line))
        for line in content.splitlines()
    ]
    assert [event.sequence for event in decoded] == [1, 2]
    assert [event.event_type for event in decoded] == [
        "RunnerStarted",
        "InputValidated",
    ]


def test_event_sink_rejects_duplicate_or_gapped_sequences_and_post_finalize_writes(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    sink = EventSink(writer=writer)
    first = _event(1, "RunnerStarted")
    second = _event(2, "InputValidated")

    sink.append(first)
    with pytest.raises(ValueError, match="sequence"):
        sink.append(first)
    with pytest.raises(ValueError, match="sequence"):
        sink.append(_event(3, "StageStarted"))
    sink.append(second)
    reference = sink.finalize()

    assert (writer.root / reference.path).read_bytes() == _event_bytes(first, second)
    with pytest.raises(RuntimeError, match="final"):
        sink.append(_event(3, "StageStarted"))
    with pytest.raises(RuntimeError, match="final"):
        sink.finalize()


def test_event_sink_stdout_mirror_is_byte_identical_to_the_final_file(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    stdout = BytesIO()
    sink = EventSink(writer=writer, stdout=stdout)
    events = (
        _event(1, "RunnerStarted"),
        _event(2, "StageStarted", payload={"stage": "plan"}),
        _event(3, "StageCompleted", payload={"stage": "plan"}),
    )

    for event in events:
        sink.append(event)
    reference = sink.finalize()

    file_bytes = (writer.root / reference.path).read_bytes()
    assert file_bytes == _event_bytes(*events)
    assert stdout.getvalue() == file_bytes
    assert reference.byte_size == len(file_bytes)
    assert reference.digest == sha256_digest(file_bytes)


def test_completion_seals_the_output_writer_and_is_the_last_created_artifact(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    sink = EventSink(writer=writer)
    sink.append(_event(1, "RunnerStarted"))
    sink.append(_event(2, "RunnerCompleted"))
    event_reference = sink.finalize()
    writer.write_protocol_manifest(event_stream=event_reference)
    result, result_reference = _write_no_change_result(
        writer,
        event_stream_digest=event_reference.digest,
    )
    before_completion = {
        path.relative_to(writer.root).as_posix()
        for path in writer.root.rglob("*")
        if path.is_file()
    }

    writer.write_completion(result=result, result_reference=result_reference)

    after_completion = {
        path.relative_to(writer.root).as_posix()
        for path in writer.root.rglob("*")
        if path.is_file()
    }
    assert after_completion - before_completion == {"completion.json"}
    with pytest.raises(RuntimeError, match="completion|final"):
        writer.write_bytes("late-artifact.txt", b"must not be written")
    with pytest.raises(RuntimeError, match="completion|final"):
        writer.write_protocol_manifest(event_stream=event_reference)
    assert not (writer.root / "late-artifact.txt").exists()


def test_protocol_manifest_binds_the_final_event_stream_reference(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    sink = EventSink(writer=writer)
    sink.append(_event(1, "RunnerStarted"))
    sink.append(_event(2, "RunnerCompleted"))
    event_reference = sink.finalize()

    manifest_reference = writer.write_protocol_manifest(
        event_stream=event_reference,
    )

    manifest_bytes = (writer.root / manifest_reference.path).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_reference.path == "protocol-manifest.json"
    assert manifest_reference.media_type == "application/json"
    assert manifest_reference.byte_size == len(manifest_bytes)
    assert manifest_reference.digest == sha256_digest(manifest_bytes)
    assert manifest["protocol"] == PROTOCOL
    assert manifest["schema_version"] == 1
    assert manifest["event_stream"] == event_reference.model_dump(mode="json")

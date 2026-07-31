from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from ai_native.factory_runner.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from ai_native.factory_runner.contracts.change_set import ChangeSet
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.run_result import RunResult
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.contracts.terminal_output import (
    CompletionManifest,
    ProtocolManifest,
)
from ai_native.factory_runner.contracts.verification_evidence import (
    VerificationEvidence,
)
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root
from ai_native.factory_runner.protocol import (
    changed_file_manifest_digest,
    load_contract_schema,
    validate_contract,
    verify_contract_digest,
)
from scripts.generate_factory_runner_goldens import runtime_golden_drift
from tests.factory_runner.contract._support import (
    completion_manifest,
    protocol_manifest,
    run_result,
)
from tests.factory_runner.contract._schema_support import REPOSITORY_ROOT


RUNTIME_GOLDEN_ROOT = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "factory_runner" / "runtime-golden"
)
_TERMINAL_PATHS = {
    "completion.json",
    "protocol-manifest.json",
    "result/run-result.json",
}
_FORMAL_DIGEST_SCHEMAS = {
    "change-set/v1",
    "checkpoint/v1",
    "run-result/v1",
    "verification-evidence/v1",
}
_INTERNAL_STATE_SCHEMAS = {
    "already-green-observation-state/v1",
    "already-green-observation/v1",
    "factory-author-workflow/v1",
    "phase-evidence-state/v1",
    "phase-execution-outcomes/v1",
    "private-author-state/v1",
}
_REFERENCE_KEYS = {"path", "media_type", "byte_size", "digest"}


def _assert_independent_schema(payload: Any, schema: str) -> None:
    errors = tuple(
        Draft202012Validator(
            load_contract_schema(schema),
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )
    assert errors == ()


def _load_formal_contract(
    root: Path,
    relative_path: str,
    schema: str,
) -> Any:
    content = (root / relative_path).read_bytes()
    payload = json.loads(content)
    assert canonical_json_bytes(payload) == content
    validated = validate_contract(content, expected_schema=schema)
    _assert_independent_schema(payload, schema)
    if schema in _FORMAL_DIGEST_SCHEMAS:
        verify_contract_digest(validated)
    return validated


def _assert_reference(root: Path, value: Any) -> bytes:
    reference = ArtifactReference.model_validate(
        value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    )
    relative = Path(reference.path)
    assert relative.as_posix() == reference.path
    assert not relative.is_absolute()
    assert relative.parts
    assert all(part not in {"", ".", ".."} for part in relative.parts)
    artifact = root / relative
    assert artifact.is_file()
    assert not artifact.is_symlink()
    content = artifact.read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    return content


def _assert_nested_references(root: Path, value: Any) -> None:
    if isinstance(value, dict):
        if set(value) == _REFERENCE_KEYS:
            _assert_reference(root, value)
            return
        for item in value.values():
            _assert_nested_references(root, item)
    elif isinstance(value, list):
        for item in value:
            _assert_nested_references(root, item)


def _assert_checkpoint_object_digests(
    value: Any,
    *,
    checkpoint: Checkpoint,
) -> None:
    if isinstance(value, dict):
        object_digest = value.get("object_digest")
        if isinstance(object_digest, str):
            matches = tuple(
                reference
                for reference in checkpoint.artifact_manifest
                if reference.digest == object_digest
            )
            assert len(matches) == 1
            if isinstance(value.get("byte_size"), int):
                assert matches[0].byte_size == value["byte_size"]
        for item in value.values():
            _assert_checkpoint_object_digests(item, checkpoint=checkpoint)
    elif isinstance(value, list):
        for item in value:
            _assert_checkpoint_object_digests(item, checkpoint=checkpoint)


def _assert_formal_json_artifact(
    root: Path,
    reference: ArtifactReference,
) -> None:
    if reference.media_type != "application/json":
        return
    content = _assert_reference(root, reference)
    payload = json.loads(content)
    assert canonical_json_bytes(payload) == content
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if schema in _INTERNAL_STATE_SCHEMAS or schema is None:
        return
    assert isinstance(schema, str)
    validated = validate_contract(content, expected_schema=schema)
    _assert_independent_schema(payload, schema)
    if schema in _FORMAL_DIGEST_SCHEMAS:
        verify_contract_digest(validated)


def test_protocol_manifest_requires_one_sorted_canonical_event_reference() -> None:
    valid = protocol_manifest()
    validated = validate_contract(
        valid,
        expected_schema="protocol-manifest/v1",
    )
    assert validated.model_dump(mode="json") == valid

    for mutate in (
        lambda payload: payload["artifacts"].reverse(),
        lambda payload: payload["artifacts"].append(payload["artifacts"][0]),
        lambda payload: payload["artifacts"].pop(0),
        lambda payload: payload["event_stream"].update(
            {"path": "nested/events.ndjson"}
        ),
    ):
        invalid = deepcopy(valid)
        mutate(invalid)
        with pytest.raises(ValueError):
            validate_contract(
                invalid,
                expected_schema="protocol-manifest/v1",
            )


def test_protocol_manifest_schema_requires_event_and_forbids_terminal_cycles() -> None:
    validator = Draft202012Validator(
        load_contract_schema("protocol-manifest/v1"),
        format_checker=FormatChecker(),
    )
    valid = protocol_manifest()
    assert list(validator.iter_errors(valid)) == []

    missing_event = deepcopy(valid)
    missing_event["artifacts"] = [
        reference
        for reference in missing_event["artifacts"]
        if reference["path"] != "events.ndjson"
    ]
    assert list(validator.iter_errors(missing_event))

    terminal_cycle = deepcopy(valid)
    terminal_cycle["artifacts"].append(
        {
            "path": "completion.json",
            "media_type": "application/json",
            "byte_size": 1,
            "digest": "sha256:" + ("f" * 64),
        }
    )
    terminal_cycle["artifacts"].sort(key=lambda reference: reference["path"])
    assert list(validator.iter_errors(terminal_cycle))


def test_completion_binds_canonical_result_and_protocol_manifest() -> None:
    valid = completion_manifest()
    validated = validate_contract(valid, expected_schema="completion/v1")
    assert validated.model_dump(mode="json") == valid

    mismatched_digest = deepcopy(valid)
    mismatched_digest["output_manifest_digest"] = "sha256:" + ("c" * 64)
    with pytest.raises(ValueError):
        validate_contract(mismatched_digest, expected_schema="completion/v1")

    for field, unsafe_path in (
        ("protocol_manifest", "nested/protocol-manifest.json"),
        ("run_result", "nested/run-result.json"),
    ):
        invalid = deepcopy(valid)
        invalid[field]["path"] = unsafe_path
        with pytest.raises(ValueError):
            validate_contract(invalid, expected_schema="completion/v1")


def test_output_writer_emits_documents_accepted_by_the_formal_contracts(
    tmp_path: Path,
) -> None:
    writer = OutputWriter(validate_output_root(tmp_path / "output"))
    event_stream = writer.write_events_placeholder()
    protocol_reference = writer.write_protocol_manifest(
        event_stream=event_stream,
    )
    protocol_document = validate_contract(
        (writer.root / protocol_reference.path).read_bytes(),
        expected_schema="protocol-manifest/v1",
    )
    assert protocol_document.event_stream.model_dump(mode="json") == (
        event_stream.model_dump(mode="json")
    )

    result_payload = run_result(outcome="no_change")
    result_payload.update(
        {
            "change_set": None,
            "completed_stages": [],
            "event_stream_digest": event_stream.digest,
            "latest_checkpoint": None,
            "output_manifest_digest": protocol_reference.digest,
            "verification_evidence": None,
        }
    )
    result_payload["result_digest"] = "sha256:" + ("0" * 64)
    result = validate_contract(result_payload, expected_schema="run-result/v1")
    result_reference = writer.write_json(
        "result/run-result.json",
        result.model_dump(mode="json"),
        record=False,
    )
    writer.write_completion(
        result=result,
        result_reference=result_reference,
        protocol_manifest=protocol_reference,
    )

    completion_bytes = (writer.root / "completion.json").read_bytes()
    completion_document = validate_contract(
        completion_bytes,
        expected_schema="completion/v1",
    )
    assert completion_document.protocol_manifest.model_dump(mode="json") == (
        protocol_reference.model_dump(mode="json")
    )
    assert completion_document.output_manifest_digest == sha256_digest(
        (writer.root / protocol_reference.path).read_bytes()
    )
    assert json.loads(completion_bytes)["schema"] == "completion/v1"


def test_writer_generated_terminal_goldens_have_no_drift() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_factory_runner_goldens.py",
            "--check",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("variant", "expected_outcome", "has_change_set"),
    (
        ("author-success", "succeeded", True),
        ("author-no-change", "no_change", False),
    ),
)
def test_real_cli_runtime_goldens_cover_author_terminal_outcomes(
    variant: str,
    expected_outcome: str,
    has_change_set: bool,
) -> None:
    root = RUNTIME_GOLDEN_ROOT / variant
    files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    assert _TERMINAL_PATHS.issubset(files)

    completion = _load_formal_contract(
        root,
        "completion.json",
        "completion/v1",
    )
    assert isinstance(completion, CompletionManifest)
    protocol = _load_formal_contract(
        root,
        "protocol-manifest.json",
        "protocol-manifest/v1",
    )
    assert isinstance(protocol, ProtocolManifest)
    result = _load_formal_contract(
        root,
        "result/run-result.json",
        "run-result/v1",
    )
    assert isinstance(result, RunResult)

    assert result.outcome == expected_outcome
    assert result.operation == "author"
    assert result.reason_code == "completed"
    assert (result.change_set is not None) is has_change_set
    assert (root / "changeset" / "change.patch").is_file() is has_change_set
    assert result.verification_evidence is None

    assert completion.outcome == result.outcome
    assert completion.completed_at == result.finished_at
    assert completion.output_manifest_digest == result.output_manifest_digest
    assert completion.protocol_manifest is not None
    assert completion.protocol_manifest.digest == result.output_manifest_digest
    assert (
        _assert_reference(root, completion.protocol_manifest)
        == (root / "protocol-manifest.json").read_bytes()
    )
    assert (
        _assert_reference(root, completion.run_result)
        == (root / "result" / "run-result.json").read_bytes()
    )

    manifest_paths = {reference.path for reference in protocol.artifacts}
    assert manifest_paths == files - _TERMINAL_PATHS
    assert protocol.event_stream.digest == result.event_stream_digest
    assert completion.protocol_manifest.digest == sha256_digest(
        (root / "protocol-manifest.json").read_bytes()
    )
    for reference in protocol.artifacts:
        _assert_reference(root, reference)
        _assert_formal_json_artifact(root, reference)

    event_content = _assert_reference(root, protocol.event_stream)
    event_lines = event_content.splitlines()
    events: list[RunnerEvent] = []
    for line in event_lines:
        payload = json.loads(line)
        assert canonical_json_bytes(payload) == line
        event = validate_contract(line, expected_schema="runner-event/v1")
        assert isinstance(event, RunnerEvent)
        _assert_independent_schema(payload, "runner-event/v1")
        _assert_nested_references(root, payload)
        events.append(event)
    assert tuple(event.sequence for event in events) == tuple(range(1, len(events) + 1))
    assert events[0].event_type == "RunnerStarted"
    assert events[-1].event_type == "RunnerCompleted"
    assert all(event.run_id == result.identity.run_id for event in events)
    assert all(event.attempt_id == result.identity.attempt_id for event in events)
    assert any(event.event_type == "CheckpointWritten" for event in events)
    assert any(event.event_type == "TestStarted" for event in events)
    assert any(event.event_type == "TestCompleted" for event in events)

    checkpoint_paths = sorted(
        (root / "checkpoints").glob("*/checkpoint.json"),
        key=lambda path: int(path.parent.name),
    )
    assert len(checkpoint_paths) > 1
    checkpoints: list[Checkpoint] = []
    for sequence, checkpoint_path in enumerate(checkpoint_paths, start=1):
        relative_path = checkpoint_path.relative_to(root).as_posix()
        checkpoint = _load_formal_contract(
            root,
            relative_path,
            "checkpoint/v1",
        )
        assert isinstance(checkpoint, Checkpoint)
        assert checkpoint.sequence == sequence
        assert checkpoint.identity == result.identity
        assert checkpoint.repository == result.repository
        assert (
            tuple(reference.digest for reference in checkpoint.artifact_manifest)
            == checkpoint.object_digests
        )
        assert set(checkpoint.evidence_refs).issubset(set(checkpoint.artifact_manifest))
        checkpoint_objects = {
            path.relative_to(root).as_posix()
            for path in checkpoint_path.parent.joinpath("objects").iterdir()
            if path.is_file()
        }
        assert checkpoint_objects == {
            reference.path for reference in checkpoint.artifact_manifest
        }
        for reference in checkpoint.artifact_manifest:
            _assert_reference(root, reference)
            _assert_formal_json_artifact(root, reference)
        payload = json.loads(checkpoint_path.read_bytes())
        _assert_nested_references(root, payload)
        _assert_checkpoint_object_digests(payload, checkpoint=checkpoint)
        if checkpoint.workspace_patch_digest is not None:
            assert checkpoint.workspace_patch_digest in checkpoint.object_digests
        checkpoints.append(checkpoint)

    assert result.latest_checkpoint is not None
    assert (
        result.latest_checkpoint.path
        == checkpoint_paths[-1].relative_to(root).as_posix()
    )
    assert _assert_reference(root, result.latest_checkpoint) == (
        checkpoint_paths[-1].read_bytes()
    )

    phase_log_paths = {
        path for path in manifest_paths if path.startswith("evidence/objects/")
    }
    assert {
        "evidence/objects/red-command-001.stdout",
        "evidence/objects/red-command-001.stderr",
        "evidence/objects/verification-command-001.stdout",
        "evidence/objects/verification-command-001.stderr",
    }.issubset(phase_log_paths)

    if has_change_set:
        assert result.change_set is not None
        change_set = _load_formal_contract(
            root,
            result.change_set.path,
            "change-set/v1",
        )
        assert isinstance(change_set, ChangeSet)
        assert change_set.identity == result.identity
        assert change_set.repository == result.repository
        assert change_set.diff_digest == changed_file_manifest_digest(
            change_set.changed_files
        )
        assert _assert_reference(root, change_set.patch)
        assert change_set.evidence_refs
        evidence_documents = []
        for reference in change_set.evidence_refs:
            evidence = _load_formal_contract(
                root,
                reference.path,
                "verification-evidence/v1",
            )
            assert isinstance(evidence, VerificationEvidence)
            assert evidence.identity == result.identity
            assert evidence.repository == result.repository
            assert evidence.environment_kind == "authoring"
            assert evidence.context_digest == change_set.context_digest
            assert evidence.evidence_set_digest == change_set.evidence_set_digest
            for item in evidence.items:
                _assert_reference(root, item.stdout)
                _assert_reference(root, item.stderr)
                for report in item.test_reports:
                    _assert_reference(root, report)
            evidence_documents.append(evidence)
        assert evidence_documents
        assert {
            "evidence/objects/green-command-001.stdout",
            "evidence/objects/refactor-command-001.stdout",
        }.issubset(phase_log_paths)
    else:
        assert result.change_set is None
        assert not any(path.startswith("changeset/") for path in files)
        assert not (root / "evidence" / "verification-evidence.json").exists()
        assert not any("/green-command-" in path for path in phase_log_paths)
        assert not any("/refactor-command-" in path for path in phase_log_paths)

    for relative_path in sorted(files):
        if not relative_path.endswith(".json"):
            continue
        payload = json.loads((root / relative_path).read_bytes())
        _assert_nested_references(root, payload)


def test_runtime_golden_check_detects_missing_changed_and_extra_files(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "runtime-golden"
    expected = {
        "author-no-change/events.ndjson": b"no-change\n",
        "author-success/events.ndjson": b"success\n",
        "author-success/completion.json": b"completion\n",
    }
    for relative_path, content in expected.items():
        path = output_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert runtime_golden_drift(output_dir, expected=expected) == ()

    (output_dir / "author-no-change" / "events.ndjson").unlink()
    (output_dir / "author-success" / "events.ndjson").write_bytes(b"changed\n")
    (output_dir / "author-success" / "unexpected.log").write_bytes(b"extra\n")

    assert set(runtime_golden_drift(output_dir, expected=expected)) == {
        "missing: author-no-change/events.ndjson",
        "changed: author-success/events.ndjson",
        "extra: author-success/unexpected.log",
    }

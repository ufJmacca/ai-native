from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root
from ai_native.factory_runner.protocol import load_contract_schema, validate_contract
from tests.factory_runner.contract._support import (
    completion_manifest,
    protocol_manifest,
    run_result,
)
from tests.factory_runner.contract._schema_support import REPOSITORY_ROOT


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

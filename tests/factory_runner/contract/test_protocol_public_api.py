from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ai_native.factory_runner import protocol as protocol_module
from ai_native.factory_runner.protocol import (
    ContractValidationError,
    contract_document_digest,
    decode_json_document,
    validate_contract,
    verify_contract_digest,
    verify_digest,
)
from tests.factory_runner.contract._schema_support import REPOSITORY_ROOT
from tests.factory_runner.contract._support import (
    BUILDERS,
    bind_changed_file_manifest_digest,
    change_set,
    changed_file,
    context_bundle,
    run_result,
    run_spec,
    verification_evidence,
)


def test_public_validation_accepts_models_mappings_and_json_without_mutation() -> None:
    payload = run_spec()
    original = deepcopy(payload)

    mapped = validate_contract(payload, expected_schema="run-spec/v1")
    encoded = validate_contract(
        json.dumps(payload).encode("utf-8"),
        expected_schema="run-spec/v1",
    )
    modeled = validate_contract(mapped, expected_schema="run-spec/v1")

    assert mapped.model_dump(mode="json") == payload
    assert encoded == mapped
    assert modeled == mapped
    assert payload == original


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"protocol": "factory-runner-protocol/v2"}, "unsupported_protocol"),
        ({"schema": "unknown/v1"}, "unsupported_schema"),
        ({"schema_version": 2}, "unsupported_schema_version"),
        ({"unexpected": "field"}, "invalid_input"),
    ],
)
def test_public_validation_maps_failures_to_stable_codes(
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    payload = run_spec()
    payload.update(mutation)

    with pytest.raises(ContractValidationError) as exc_info:
        validate_contract(payload)

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    "raw",
    [
        b'{"protocol":"v1","protocol":"v1"}',
        b'{"nested":{"value":1,"value":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
        '{"value":1}'.encode("utf-16"),
    ],
)
def test_public_json_decoder_rejects_duplicates_and_non_finite_numbers(
    raw: bytes,
) -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        decode_json_document(raw)

    assert exc_info.value.code == "invalid_json"


def test_public_json_decoder_maps_huge_integer_tokens_to_invalid_json() -> None:
    raw = b'{"value":' + (b"9" * 5000) + b"}"

    with pytest.raises(ContractValidationError) as exc_info:
        decode_json_document(raw)

    assert exc_info.value.code == "invalid_json"


@pytest.mark.parametrize(
    ("model_name", "digest_field"),
    [
        ("ContextBundle", "bundle_digest"),
        ("Checkpoint", "checkpoint_digest"),
        ("VerificationEvidence", "evidence_set_digest"),
        ("ChangeSet", "change_set_digest"),
        ("RunResult", "result_digest"),
    ],
)
def test_self_digest_verification_removes_only_its_own_top_level_field(
    model_name: str,
    digest_field: str,
) -> None:
    payload = BUILDERS[model_name]()
    payload[digest_field] = contract_document_digest(payload)
    verify_contract_digest(payload)

    tampered = deepcopy(payload)
    tampered["identity"]["correlation_id"] = "tampered-correlation"
    with pytest.raises(ContractValidationError) as exc_info:
        verify_contract_digest(tampered)
    assert exc_info.value.code == "digest_mismatch"


def test_raw_digest_verification_has_a_stable_mismatch_code() -> None:
    content = b"artifact bytes"
    expected = "sha256:" + "0" * 64

    with pytest.raises(ContractValidationError) as exc_info:
        verify_digest(content, expected)

    assert exc_info.value.code == "digest_mismatch"


def test_contract_only_public_import_does_not_load_workflow_or_control_plane(
    tmp_path: Path,
) -> None:
    script = f"""
import json
import sys
sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
import ai_native.factory_runner.protocol
forbidden = (
    "ai_native.adapters",
    "ai_native.orchestrator",
    "ai_native.run_registry",
    "ai_native.stages",
    "ai_native.workflow_stages",
    "docker",
    "github",
    "playwright",
    "psycopg",
    "temporalio",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps(loaded))
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_context_bundle_digest_does_not_mutate_mapping() -> None:
    payload = context_bundle()
    original = deepcopy(payload)

    contract_document_digest(payload)

    assert payload == original


def test_changed_file_manifest_digest_is_deterministic_and_order_sensitive() -> None:
    first = changed_file()
    second = changed_file("add")
    second["path"] = "src/new.py"
    payload = change_set()
    payload["changed_files"] = [first, second]
    bind_changed_file_manifest_digest(payload)

    api_digest = protocol_module.changed_file_manifest_digest

    assert api_digest(payload["changed_files"]) == payload["diff_digest"]
    assert (
        api_digest(tuple(reversed(payload["changed_files"]))) != payload["diff_digest"]
    )


@pytest.mark.parametrize(
    "payload",
    [
        verification_evidence(),
        run_result(),
        {
            **run_result(outcome="invalid_input"),
            "identity": None,
            "repository": None,
        },
    ],
)
def test_self_digest_survives_validation_and_wire_round_trip(
    payload: dict[str, object],
) -> None:
    if payload["schema"] == "verification-evidence/v1":
        digest_field = "evidence_set_digest"
    else:
        digest_field = "result_digest"

    payload[digest_field] = contract_document_digest(payload)
    validated = validate_contract(payload)
    serialized = validated.model_dump(mode="json")
    revalidated = validate_contract(validated)

    verify_contract_digest(validated)
    verify_contract_digest(serialized)
    verify_contract_digest(revalidated)
    assert serialized == payload


@pytest.mark.parametrize(
    ("payload", "missing_path"),
    [
        (
            {
                **run_result(outcome="invalid_input"),
                "identity": None,
                "repository": None,
            },
            ("identity",),
        ),
        (
            {
                **run_result(outcome="invalid_input"),
                "identity": None,
                "repository": None,
            },
            ("repository",),
        ),
        (verification_evidence(), ("runner", "image")),
        (verification_evidence(), ("runner", "source_commit")),
        (run_result(), ("runner_build", "image")),
        (run_result(), ("runner_build", "source_commit")),
    ],
)
def test_nullable_wire_members_must_be_explicit(
    payload: dict[str, object],
    missing_path: tuple[str, ...],
) -> None:
    container = payload
    for member in missing_path[:-1]:
        nested = container[member]
        assert isinstance(nested, dict)
        container = nested
    del container[missing_path[-1]]

    with pytest.raises(ContractValidationError) as exc_info:
        validate_contract(payload)

    assert exc_info.value.code == "invalid_input"

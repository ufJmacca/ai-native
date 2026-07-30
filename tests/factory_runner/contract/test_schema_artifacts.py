from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.protocol import (
    load_contract_schema,
    schema_manifest_digest,
    schema_set_digest,
)
from ai_native.factory_runner.schema_generation import (
    render_schema_artifacts,
    schema_artifact_drift,
    write_schema_artifacts,
)
from tests.factory_runner.contract._schema_support import (
    CONTRACT_CASES,
    EXPECTED_SCHEMA_ARTIFACT_FILENAMES,
    EXPECTED_SCHEMA_FILENAMES,
    SCHEMA_DIRECTORY,
)
from tests.factory_runner.contract._support import (
    DIGEST_A,
    checkpoint,
    run_result,
    run_spec,
    runner_event,
    verification_evidence,
    verification_run_spec,
)


def _checked_in_filenames() -> tuple[str, ...]:
    if not SCHEMA_DIRECTORY.is_dir():
        return ()
    return tuple(sorted(path.name for path in SCHEMA_DIRECTORY.iterdir()))


def test_checked_in_v1_schema_artifact_set_is_exact() -> None:
    assert len(EXPECTED_SCHEMA_FILENAMES) == 7
    assert _checked_in_filenames() == EXPECTED_SCHEMA_ARTIFACT_FILENAMES


def test_all_checked_in_schemas_are_valid_draft_2020_12() -> None:
    for case in CONTRACT_CASES:
        schema = load_contract_schema(case.schema_name)
        assert schema["$schema"] == Draft202012Validator.META_SCHEMA["$id"]
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker())


def test_schema_generation_is_deterministic_and_has_no_drift(
    tmp_path: Path,
) -> None:
    first = render_schema_artifacts()
    second = render_schema_artifacts()

    assert first == second
    assert tuple(first) == EXPECTED_SCHEMA_ARTIFACT_FILENAMES
    assert schema_artifact_drift(SCHEMA_DIRECTORY, expected=first) == ()

    left = tmp_path / "left"
    right = tmp_path / "right"
    write_schema_artifacts(left)
    write_schema_artifacts(right)
    assert {path.name: path.read_bytes() for path in sorted(left.iterdir())} == {
        path.name: path.read_bytes() for path in sorted(right.iterdir())
    }


def test_manifest_binds_individual_canonical_and_raw_digests() -> None:
    manifest_path = SCHEMA_DIRECTORY / "schema-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest["schemas"]

    assert tuple(entry["path"] for entry in entries) == EXPECTED_SCHEMA_FILENAMES
    for entry, case in zip(entries, CONTRACT_CASES, strict=True):
        schema = json.loads(
            (SCHEMA_DIRECTORY / case.schema_filename).read_text(encoding="utf-8")
        )
        assert entry == {
            "digest": sha256_digest(canonical_json_bytes(schema)),
            "path": case.schema_filename,
            "schema": case.schema_name,
            "schema_id": schema["$id"],
        }

    canonical_manifest_digest = sha256_digest(canonical_json_bytes(manifest))
    raw_manifest_digest = sha256_digest(manifest_bytes)
    assert schema_set_digest() == canonical_manifest_digest
    assert (
        SCHEMA_DIRECTORY.joinpath("schema-set.sha256")
        .read_text(encoding="ascii")
        .strip()
        == canonical_manifest_digest
    )
    assert schema_manifest_digest() == raw_manifest_digest
    assert raw_manifest_digest != canonical_manifest_digest


def test_json_schemas_encode_cross_field_and_payload_safety_rules() -> None:
    verify_with_authoring_workspace = run_spec()
    verify_with_authoring_workspace["operation"] = "verify"
    run_spec_errors = list(
        Draft202012Validator(
            load_contract_schema("run-spec/v1"),
            format_checker=FormatChecker(),
        ).iter_errors(verify_with_authoring_workspace)
    )
    assert run_spec_errors

    event_with_secret_field = runner_event()
    event_with_secret_field["sanitised_payload"]["github_token"] = "secret-canary"
    event_errors = list(
        Draft202012Validator(
            load_contract_schema("runner-event/v1"),
            format_checker=FormatChecker(),
        ).iter_errors(event_with_secret_field)
    )
    assert event_errors


def test_json_schemas_encode_schema_freeze_safety_rules() -> None:
    author_with_change_set = run_spec()
    author_with_change_set["verification_input"] = {
        "change_set_path": "/factory/input/verification/change-set.json",
        "expected_digest": DIGEST_A,
    }
    verify_without_change_set = verification_run_spec()
    verify_without_change_set["verification_input"] = None
    author_evidence_with_future_digest = verification_evidence()
    author_evidence_with_future_digest["change_set_digest"] = DIGEST_A
    non_input_failure_without_identity = run_result(outcome="failed")
    non_input_failure_without_identity["identity"] = None
    event_with_nested_secret = runner_event()
    event_with_nested_secret["sanitised_payload"] = {
        "nested": {"github_token": "secret-canary"}
    }
    checkpoint_with_unsafe_integer = checkpoint()
    checkpoint_with_unsafe_integer["workflow_state"] = {"value": 10**30}

    invalid_cases = (
        ("run-spec/v1", author_with_change_set),
        ("run-spec/v1", verify_without_change_set),
        ("verification-evidence/v1", author_evidence_with_future_digest),
        ("run-result/v1", non_input_failure_without_identity),
        ("runner-event/v1", event_with_nested_secret),
        ("checkpoint/v1", checkpoint_with_unsafe_integer),
    )
    for schema_name, payload in invalid_cases:
        validator = Draft202012Validator(
            load_contract_schema(schema_name),
            format_checker=FormatChecker(),
        )
        assert list(validator.iter_errors(payload)), schema_name

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.factory_runner.contract._support import (
    BASE_COMMIT_SHA,
    BUILDERS,
    CREATED_AT,
    MODEL_NAMES,
    PROTOCOL,
    assert_invalid,
    dumped,
    run_spec,
    run_result,
    context_bundle,
    validate,
)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_complete_valid_document_is_accepted(model_name: str) -> None:
    validate(model_name, BUILDERS[model_name]())


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_unknown_top_level_fields_are_rejected(model_name: str) -> None:
    payload = BUILDERS[model_name]()
    payload["github_token"] = "secret-canary"

    assert_invalid(model_name, payload)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("protocol", "factory-runner-protocol/v2"),
        ("schema_version", 2),
        ("schema_version", "1"),
        ("schema_version", True),
    ],
)
def test_protocol_and_schema_versions_are_strict(
    model_name: str,
    field: str,
    invalid_value: object,
) -> None:
    payload = BUILDERS[model_name]()
    payload[field] = invalid_value

    assert_invalid(model_name, payload)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_contract_specific_schema_identifier_is_required(model_name: str) -> None:
    payload = BUILDERS[model_name]()
    payload["schema"] = "wrong-contract/v1"

    assert_invalid(model_name, payload)


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-07-30T10:00:00",
        "2026-07-30T20:00:00+10:00",
        "not-a-timestamp",
    ],
)
def test_timestamps_require_utc_rfc3339(invalid_timestamp: str) -> None:
    payload = run_spec()
    payload["created_at"] = invalid_timestamp
    assert_invalid("RunSpec", payload)

    event = BUILDERS["RunnerEvent"]()
    event["timestamp"] = invalid_timestamp
    assert_invalid("RunnerEvent", event)


def test_nested_identity_and_repository_fields_are_strict() -> None:
    for section in ("identity", "repository"):
        payload = run_spec()
        payload[section]["unexpected"] = "not-allowed"
        assert_invalid("RunSpec", payload)


def test_identity_values_round_trip_without_normalisation() -> None:
    payload = run_spec()
    exact_values = {
        "work_item_id": " Work-É\u0301-001 ",
        "work_item_revision_id": "Revision-0001",
        "delivery_phase_id": "PHASE-A",
        "run_id": "run-00001",
        "attempt_id": "Attempt-Case-Sensitive",
        "correlation_id": " correlation-with-spaces ",
    }
    payload["identity"] = exact_values

    assert dumped("RunSpec", payload)["identity"] == exact_values


@pytest.mark.parametrize("field", ["run_id", "attempt_id", "correlation_id"])
def test_identity_values_do_not_coerce_non_strings(field: str) -> None:
    payload = run_spec()
    payload["identity"][field] = 123

    assert_invalid("RunSpec", payload)


@pytest.mark.parametrize(
    "invalid_sha",
    [
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        BASE_COMMIT_SHA + " ",
    ],
)
def test_base_commit_sha_is_exact_lowercase_hex(invalid_sha: str) -> None:
    payload = run_spec()
    payload["repository"]["base_commit_sha"] = invalid_sha

    assert_invalid("RunSpec", payload)


def test_input_payload_is_not_mutated_during_validation() -> None:
    payload = run_spec()
    original = deepcopy(payload)

    validate("RunSpec", payload)

    assert payload == original
    assert payload["protocol"] == PROTOCOL
    assert payload["created_at"] == CREATED_AT


def test_repository_path_and_result_message_length_limits_match_schema() -> None:
    context = context_bundle()
    context["manifest_entries"][0]["logical_path"] = "a" * 4097
    assert_invalid("ContextBundle", context)

    result = run_result()
    result["message"] = "x" * 4097
    assert_invalid("RunResult", result)

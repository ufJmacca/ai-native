from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from ai_native.factory_runner.contracts.change_set import ChangedFile
from ai_native.factory_runner.contracts.run_spec import RunPolicy
from tests.factory_runner.contract._support import (
    DIGEST_A,
    DIGEST_B,
    PROTOCOL,
    assert_invalid,
    bind_self_digest,
    checkpoint,
    change_set,
    changed_file,
    clean_verification_evidence,
    protocol_api,
    resuming_run_spec,
    run_result,
    run_spec,
    runner_event,
    verification_evidence,
)


@pytest.mark.parametrize(
    ("required", "optional", "supported"),
    [
        (["author", "author"], [], ["author"]),
        (["author"], ["structured-events", "structured-events"], ["author"]),
        (["author"], [], ["author", "author"]),
        (["author"], ["author"], ["author"]),
    ],
    ids=[
        "duplicate-required",
        "duplicate-optional",
        "duplicate-supported",
        "required-optional-overlap",
    ],
)
def test_capability_duplicates_and_overlap_are_invalid_input(
    required: list[str],
    optional: list[str],
    supported: list[str],
) -> None:
    with pytest.raises(Exception) as exc_info:
        protocol_api().negotiate_protocol(
            protocol=PROTOCOL,
            required_capabilities=required,
            optional_capabilities=optional,
            supported_capabilities=supported,
        )

    assert getattr(exc_info.value, "code", None) == "invalid_input"


@pytest.mark.parametrize(
    "unordered",
    [
        {"author", "structured-events"},
        frozenset({"author", "structured-events"}),
    ],
)
def test_protocol_negotiation_rejects_unordered_capability_collections(
    unordered: set[str] | frozenset[str],
) -> None:
    with pytest.raises(Exception) as exc_info:
        protocol_api().negotiate_protocol(
            protocol=PROTOCOL,
            required_capabilities=unordered,
            optional_capabilities=[],
            supported_capabilities=["author", "structured-events"],
        )

    assert getattr(exc_info.value, "code", None) == "invalid_input"


@pytest.mark.parametrize(
    "invalid_name",
    [" bad ", "bad/name", "\n", "a" * 129],
)
def test_protocol_negotiation_enforces_capability_name_grammar(
    invalid_name: str,
) -> None:
    with pytest.raises(Exception) as exc_info:
        protocol_api().negotiate_protocol(
            protocol=PROTOCOL,
            required_capabilities=[invalid_name],
            optional_capabilities=[],
            supported_capabilities=[invalid_name],
        )

    assert getattr(exc_info.value, "code", None) == "invalid_input"


def test_checkpoint_resume_cannot_remove_a_prohibited_path() -> None:
    stored = checkpoint()
    resumed = resuming_run_spec()
    resumed["policy"]["prohibited_paths"] = []

    with pytest.raises(Exception) as exc_info:
        protocol_api().validate_checkpoint_compatibility(
            stored,
            resumed,
            supported_capabilities=["author", "structured-events"],
        )

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"


@pytest.mark.parametrize(
    ("limit_field", "consumed_field", "below_consumed"),
    [
        ("max_wall_seconds", "wall_seconds", 9),
        ("max_agent_turns", "agent_turns", 1),
        ("max_model_tokens", "model_tokens", 999),
    ],
)
def test_checkpoint_resume_budget_cannot_be_below_already_consumed_usage(
    limit_field: str,
    consumed_field: str,
    below_consumed: int,
) -> None:
    stored = checkpoint()
    resumed = resuming_run_spec()
    assert below_consumed < stored["budgets"]["consumed"][consumed_field]
    resumed["policy"][limit_field] = below_consumed

    with pytest.raises(Exception) as exc_info:
        protocol_api().validate_checkpoint_compatibility(
            stored,
            resumed,
            supported_capabilities=["author", "structured-events"],
        )

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"


def test_checkpoint_resume_budget_may_equal_already_consumed_usage() -> None:
    stored = checkpoint()
    resumed = deepcopy(resuming_run_spec())
    resumed["policy"]["max_wall_seconds"] = stored["budgets"]["consumed"][
        "wall_seconds"
    ]
    resumed["policy"]["max_agent_turns"] = stored["budgets"]["consumed"]["agent_turns"]
    resumed["policy"]["max_model_tokens"] = stored["budgets"]["consumed"][
        "model_tokens"
    ]

    protocol_api().validate_checkpoint_compatibility(
        stored,
        resumed,
        supported_capabilities=["author", "structured-events"],
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_resume_reference",
        "wrong_checkpoint_digest",
        "undeclared_required_capability",
        "unsupported_checkpoint_capability",
        "runner_version_too_old",
        "tampered_checkpoint_self_digest",
    ],
)
def test_checkpoint_resume_binds_compatibility_requirements(mutation: str) -> None:
    stored = checkpoint()
    resumed = resuming_run_spec()
    if mutation == "missing_resume_reference":
        resumed["resume"] = {"checkpoint_path": None, "expected_digest": None}
    elif mutation == "wrong_checkpoint_digest":
        resumed["resume"]["expected_digest"] = DIGEST_A
    elif mutation == "undeclared_required_capability":
        stored["compatibility"]["required_capabilities"] = ["unknown-required"]
        bind_self_digest(stored, "checkpoint_digest")
        resumed["resume"]["expected_digest"] = stored["checkpoint_digest"]
    elif mutation == "unsupported_checkpoint_capability":
        stored["compatibility"]["required_capabilities"] = ["structured-events"]
        resumed["capabilities"]["required"].append("structured-events")
        bind_self_digest(stored, "checkpoint_digest")
        resumed["resume"]["expected_digest"] = stored["checkpoint_digest"]
    elif mutation == "tampered_checkpoint_self_digest":
        stored["workflow_state"]["status"] = "tampered"
    else:
        stored["compatibility"]["minimum_runner_version"] = "999.0.0"
        bind_self_digest(stored, "checkpoint_digest")
        resumed["resume"]["expected_digest"] = stored["checkpoint_digest"]

    with pytest.raises(Exception) as exc_info:
        protocol_api().validate_checkpoint_compatibility(
            stored,
            resumed,
            supported_capabilities=["author"],
        )

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"


@pytest.mark.parametrize("runner_version", ["", 1, b"1.4.0", False])
def test_checkpoint_resume_rejects_an_explicit_invalid_runner_version(
    runner_version: object,
) -> None:
    with pytest.raises(Exception) as exc_info:
        protocol_api().validate_checkpoint_compatibility(
            checkpoint(),
            resuming_run_spec(),
            supported_capabilities=["author", "structured-events"],
            runner_version=runner_version,
        )

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"


def test_checkpoint_resume_rejects_huge_runner_version_with_stable_code() -> None:
    with pytest.raises(Exception) as exc_info:
        protocol_api().validate_checkpoint_compatibility(
            checkpoint(),
            resuming_run_spec(),
            supported_capabilities=["author", "structured-events"],
            runner_version=("9" * 5000) + ".0.0",
        )

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"


def test_contract_integers_stay_inside_the_rfc_8785_domain() -> None:
    payload = run_spec()
    payload["policy"]["max_model_tokens"] = 10**30

    assert_invalid("RunSpec", payload)


def test_runner_event_sanitised_payload_is_immutable_after_validation() -> None:
    event = protocol_api().RunnerEvent.model_validate(runner_event())

    with pytest.raises(TypeError):
        event.sanitised_payload["github_token"] = "secret-canary"
    with pytest.raises(Exception):
        event.sanitised_payload |= {"github_token": "secret-canary"}
    assert "github_token" not in event.sanitised_payload
    with pytest.raises(TypeError):
        dict.__setitem__(
            event.sanitised_payload,
            "github_token",
            "secret-canary",
        )


def test_all_durable_contract_collections_are_immutable_after_validation() -> None:
    api = protocol_api()
    stored = api.Checkpoint.model_validate(checkpoint())
    evidence = api.VerificationEvidence.model_validate(verification_evidence())
    changes = api.ChangeSet.model_validate(change_set())
    result = api.RunResult.model_validate(run_result())

    with pytest.raises(TypeError):
        stored.workflow_state["secret"] = "late mutation"
    with pytest.raises((AttributeError, TypeError)):
        evidence.items.append(evidence.items[0])
    with pytest.raises(TypeError):
        evidence.items[0].tool_versions["pytest"] = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        changes.changed_files.append(changes.changed_files[0])
    with pytest.raises((AttributeError, TypeError)):
        result.completed_stages.append("intake")


def test_checkpoint_artifact_manifest_paths_are_unique() -> None:
    stored = checkpoint()
    duplicate = deepcopy(stored["artifact_manifest"][0])
    duplicate["digest"] = DIGEST_B
    stored["artifact_manifest"].append(duplicate)

    assert_invalid("Checkpoint", stored)


def test_changed_file_digest_helper_revalidates_model_instances() -> None:
    api = protocol_api()
    valid = ChangedFile.model_validate(changed_file())
    bypassed = valid.model_copy(update={"path": ".git/config"})

    with pytest.raises(ValueError):
        api.changed_file_manifest_digest([bypassed])


@pytest.mark.parametrize(
    "unordered",
    [
        lambda values: set(values),
        lambda values: (value for value in values),
    ],
)
def test_changed_file_digest_helper_requires_an_ordered_sequence(
    unordered: Callable[[list[Any]], Any],
) -> None:
    api = protocol_api()
    values = [
        ChangedFile.model_validate(changed_file()),
        ChangedFile.model_validate(
            {
                **changed_file("add"),
                "path": "src/new.py",
            }
        ),
    ]

    with pytest.raises(ValueError):
        api.changed_file_manifest_digest(unordered(values))


def test_nested_contract_instances_are_always_revalidated() -> None:
    policy = RunPolicy.model_validate(run_spec()["policy"])
    bypassed = policy.model_copy(update={"allowed_stages": ("commit",)})
    payload = run_spec()
    payload["policy"] = bypassed

    assert_invalid("RunSpec", payload)


def test_nested_json_integers_stay_inside_the_rfc_8785_domain() -> None:
    stored = checkpoint()
    stored["workflow_state"] = {"nested": [10**30]}
    assert_invalid("Checkpoint", stored)

    event = runner_event()
    event["sanitised_payload"] = {"nested": [10**30]}
    assert_invalid("RunnerEvent", event)


@pytest.mark.parametrize(
    ("model_name", "builder", "field_name", "byte_limit"),
    [
        ("RunnerEvent", runner_event, "sanitised_payload", 16_384),
        ("Checkpoint", checkpoint, "workflow_state", 262_144),
    ],
)
def test_inline_json_byte_limits_use_rfc_8785_canonical_bytes(
    model_name: str,
    builder: Callable[[], dict[str, Any]],
    field_name: str,
    byte_limit: int,
) -> None:
    api = protocol_api()
    full_chunks = (byte_limit // 4096) - 1
    value = {
        "number": 0.000001,
        "padding": [*("x" * 4096 for _ in range(full_chunks)), ""],
    }
    padding_size = byte_limit - len(api.canonical_json_bytes(value))
    assert 0 <= padding_size <= 4096
    value["padding"][-1] = "x" * padding_size
    assert len(api.canonical_json_bytes(value)) == byte_limit

    payload = builder()
    payload[field_name] = value
    getattr(api, model_name).model_validate(payload)

    payload[field_name]["padding"][-1] += "x"
    assert_invalid(model_name, payload)


def test_every_valid_contract_can_be_canonicalised() -> None:
    api = protocol_api()
    for model_name, builder in (
        ("RunSpec", run_spec),
        ("Checkpoint", checkpoint),
        ("VerificationEvidence", verification_evidence),
        ("ChangeSet", change_set),
        ("RunResult", run_result),
        ("RunnerEvent", runner_event),
    ):
        model = getattr(api, model_name).model_validate(builder())
        assert api.canonical_json_bytes(model)


def test_validated_immutable_json_subdocuments_can_be_canonicalised() -> None:
    api = protocol_api()
    stored = api.Checkpoint.model_validate(checkpoint())
    event = api.RunnerEvent.model_validate(runner_event())

    assert api.canonical_json_bytes(stored.workflow_state) == (
        api.canonical_json_bytes({"status": "ready"})
    )
    assert api.canonical_json_bytes(event.sanitised_payload) == (
        api.canonical_json_bytes({"stage": "plan"})
    )


def test_nanosecond_timestamp_order_is_not_truncated_to_microseconds() -> None:
    evidence = verification_evidence()
    evidence["items"][0]["started_at"] = "2026-07-30T10:00:00.123456789Z"
    evidence["items"][0]["finished_at"] = "2026-07-30T10:00:00.123456780Z"

    assert_invalid("VerificationEvidence", evidence)


@pytest.mark.parametrize(
    ("environment_kind", "change_set_digest", "valid"),
    [
        ("authoring", None, True),
        ("authoring", DIGEST_A, False),
        ("clean_verification", None, False),
        ("clean_verification", DIGEST_A, True),
    ],
)
def test_evidence_digest_direction_is_one_way(
    environment_kind: str,
    change_set_digest: str | None,
    valid: bool,
) -> None:
    payload = (
        clean_verification_evidence()
        if environment_kind == "clean_verification"
        else verification_evidence()
    )
    payload["environment_kind"] = environment_kind
    payload["change_set_digest"] = change_set_digest

    if valid:
        protocol_api().VerificationEvidence.model_validate(payload)
    else:
        assert_invalid("VerificationEvidence", payload)


def test_invalid_input_result_accepts_explicitly_unknown_source_identity() -> None:
    payload = run_result(operation="author", outcome="invalid_input")
    payload["identity"] = None
    payload["repository"] = None

    protocol_api().RunResult.model_validate(payload)


def test_non_input_failure_result_requires_source_identity() -> None:
    payload = run_result(operation="author", outcome="failed")
    payload["identity"] = None

    assert_invalid("RunResult", payload)


def test_wire_string_scalars_do_not_coerce_bytes() -> None:
    evidence = verification_evidence()
    evidence["advisory_observations"] = [b"bytes"]
    assert_invalid("VerificationEvidence", evidence)

    result = run_result()
    result["reason_code"] = b"completed"
    assert_invalid("RunResult", result)

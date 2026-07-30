from __future__ import annotations

from copy import deepcopy

import pytest

from tests.factory_runner.contract._support import (
    DIGEST_A,
    PROTOCOL,
    assert_invalid,
    bind_self_digest,
    checkpoint,
    change_set,
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


def test_contract_integers_stay_inside_the_rfc_8785_domain() -> None:
    payload = run_spec()
    payload["policy"]["max_model_tokens"] = 10**30

    assert_invalid("RunSpec", payload)


def test_runner_event_sanitised_payload_is_immutable_after_validation() -> None:
    event = protocol_api().RunnerEvent.model_validate(runner_event())

    with pytest.raises(TypeError):
        event.sanitised_payload["github_token"] = "secret-canary"


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


def test_nested_json_integers_stay_inside_the_rfc_8785_domain() -> None:
    stored = checkpoint()
    stored["workflow_state"] = {"nested": [10**30]}
    assert_invalid("Checkpoint", stored)

    event = runner_event()
    event["sanitised_payload"] = {"nested": [10**30]}
    assert_invalid("RunnerEvent", event)


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
    payload = verification_evidence()
    payload["environment_kind"] = environment_kind
    payload["change_set_digest"] = change_set_digest

    if valid:
        protocol_api().VerificationEvidence.model_validate(payload)
    else:
        assert_invalid("VerificationEvidence", payload)


def test_invalid_input_result_can_omit_untrusted_source_identity() -> None:
    payload = run_result(operation="author", outcome="invalid_input")
    payload["identity"] = None
    payload["repository"] = None

    protocol_api().RunResult.model_validate(payload)


def test_non_input_failure_result_requires_source_identity() -> None:
    payload = run_result(operation="author", outcome="failed")
    payload["identity"] = None

    assert_invalid("RunResult", payload)

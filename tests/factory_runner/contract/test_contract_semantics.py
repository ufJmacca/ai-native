from __future__ import annotations

from copy import deepcopy

import pytest

from tests.factory_runner.contract._support import (
    DIGEST_A,
    assert_invalid,
    change_set,
    checkpoint,
    context_bundle,
    run_result,
    runner_event,
    validate,
    verification_evidence,
)


@pytest.mark.parametrize(
    "mutation",
    [
        "invalid_classification",
        "duplicate_logical_path",
        "traversal_path",
        "negative_byte_size",
        "malformed_digest",
    ],
)
def test_context_bundle_rejects_invalid_manifest_entries(mutation: str) -> None:
    payload = context_bundle()
    entry = payload["manifest_entries"][0]
    if mutation == "invalid_classification":
        entry["classification"] = "unreviewed_memory"
    elif mutation == "duplicate_logical_path":
        payload["manifest_entries"].append(deepcopy(entry))
    elif mutation == "traversal_path":
        entry["logical_path"] = "../other-repository/memory.md"
    elif mutation == "negative_byte_size":
        entry["byte_size"] = -1
    elif mutation == "malformed_digest":
        entry["digest"] = "sha256:not-a-digest"

    assert_invalid("ContextBundle", payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "zero_sequence",
        "producer_mismatch",
        "unknown_completed_stage",
        "publication_next_stage",
        "negative_consumed_budget",
        "remaining_exceeds_authority",
        "duplicate_object_digest",
    ],
)
def test_checkpoint_document_fails_closed_on_invalid_state(mutation: str) -> None:
    payload = checkpoint()
    if mutation == "zero_sequence":
        payload["sequence"] = 0
    elif mutation == "producer_mismatch":
        payload["producer_attempt_id"] = "different-producer-attempt"
    elif mutation == "unknown_completed_stage":
        payload["completed_stages"].append("unknown")
    elif mutation == "publication_next_stage":
        payload["next_permitted_stage"] = "commit"
    elif mutation == "negative_consumed_budget":
        payload["budgets"]["consumed"]["agent_turns"] = -1
    elif mutation == "remaining_exceeds_authority":
        payload["budgets"]["remaining"]["model_tokens"] = 50_001
    elif mutation == "duplicate_object_digest":
        payload["object_digests"].append(DIGEST_A)

    assert_invalid("Checkpoint", payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "red_exit_zero",
        "red_actual_passed",
        "red_expected_passed",
        "red_syntax_failure",
        "red_collection_failure",
        "red_infrastructure_failure",
        "negative_duration",
        "finish_before_start",
        "environment_contains_value",
        "shell_string_command",
    ],
)
def test_verification_evidence_rejects_false_red_and_malformed_items(
    mutation: str,
) -> None:
    payload = verification_evidence()
    item = payload["items"][0]
    if mutation == "red_exit_zero":
        item["exit_code"] = 0
    elif mutation == "red_actual_passed":
        item["actual_status"] = "passed"
    elif mutation == "red_expected_passed":
        item["expected_status"] = "passed"
    elif mutation == "red_syntax_failure":
        item["failure_classification"] = "syntax_error"
    elif mutation == "red_collection_failure":
        item["failure_classification"] = "collection_error"
    elif mutation == "red_infrastructure_failure":
        item["failure_classification"] = "infrastructure_error"
    elif mutation == "negative_duration":
        item["duration_seconds"] = -1
    elif mutation == "finish_before_start":
        item["started_at"], item["finished_at"] = (
            item["finished_at"],
            item["started_at"],
        )
    elif mutation == "environment_contains_value":
        item["environment_keys"] = ["TOKEN=secret-canary"]
    elif mutation == "shell_string_command":
        item["command"] = "pytest -q"

    assert_invalid("VerificationEvidence", payload)


@pytest.mark.parametrize("operation", ["add", "modify", "delete", "rename"])
def test_change_set_accepts_complete_file_operations(operation: str) -> None:
    validate("ChangeSet", change_set(operation))


@pytest.mark.parametrize(
    "mutation",
    [
        "rename_without_previous_path",
        "add_with_previous_blob",
        "delete_with_resulting_blob",
        "modify_without_previous_blob",
        "modify_without_resulting_blob",
        "symlink_mode",
        "submodule_mode",
        "denied_path",
        "git_internal",
        "traversal_path",
        "publication_metadata",
    ],
)
def test_change_set_rejects_inconsistent_or_unsafe_file_operations(
    mutation: str,
) -> None:
    payload = change_set("modify")
    entry = payload["changed_files"][0]
    if mutation == "rename_without_previous_path":
        payload = change_set("rename")
        payload["changed_files"][0]["previous_path"] = None
    elif mutation == "add_with_previous_blob":
        payload = change_set("add")
        payload["changed_files"][0]["previous_blob_digest"] = DIGEST_A
    elif mutation == "delete_with_resulting_blob":
        payload = change_set("delete")
        payload["changed_files"][0]["resulting_blob_digest"] = DIGEST_A
    elif mutation == "modify_without_previous_blob":
        entry["previous_blob_digest"] = None
    elif mutation == "modify_without_resulting_blob":
        entry["resulting_blob_digest"] = None
    elif mutation == "symlink_mode":
        entry["resulting_mode"] = "120000"
    elif mutation == "submodule_mode":
        entry["resulting_mode"] = "160000"
    elif mutation == "denied_path":
        entry["allowed_path_decision"] = "denied"
    elif mutation == "git_internal":
        entry["path"] = ".git/config"
    elif mutation == "traversal_path":
        entry["path"] = "../outside"
    elif mutation == "publication_metadata":
        payload["branch_name"] = "factory-published-branch"

    assert_invalid("ChangeSet", payload)


def test_no_change_is_a_result_without_a_change_set() -> None:
    validate("RunResult", run_result(operation="author", outcome="no_change"))


@pytest.mark.parametrize(
    "mutation",
    [
        "author_success_without_change_set",
        "verify_success_without_evidence",
        "no_change_with_change_set",
        "verify_with_change_set",
        "author_with_clean_verification_evidence",
        "finish_before_start",
    ],
)
def test_run_result_enforces_operation_and_outcome_references(mutation: str) -> None:
    if mutation == "author_success_without_change_set":
        payload = run_result()
        payload["change_set"] = None
    elif mutation == "verify_success_without_evidence":
        payload = run_result(operation="verify")
        payload["verification_evidence"] = None
    elif mutation == "no_change_with_change_set":
        payload = run_result(outcome="no_change")
        payload["change_set"] = {
            "path": "changeset/change-set.json",
            "media_type": "application/json",
            "byte_size": 3,
            "digest": DIGEST_A,
        }
    elif mutation == "verify_with_change_set":
        payload = run_result(operation="verify")
        payload["change_set"] = {
            "path": "changeset/change-set.json",
            "media_type": "application/json",
            "byte_size": 3,
            "digest": DIGEST_A,
        }
    elif mutation == "author_with_clean_verification_evidence":
        payload = run_result()
        payload["verification_evidence"] = {
            "path": "evidence/verification-evidence.json",
            "media_type": "application/json",
            "byte_size": 3,
            "digest": DIGEST_A,
        }
    else:
        payload = run_result()
        payload["started_at"], payload["finished_at"] = (
            payload["finished_at"],
            payload["started_at"],
        )

    assert_invalid("RunResult", payload)


@pytest.mark.parametrize(
    "event_type",
    [
        "RunQueued",
        "SandboxLeaseGranted",
        "PullRequestOpened",
        "PullRequestMerged",
        "DatabaseTransactionCommitted",
    ],
)
def test_runner_event_rejects_factory_control_plane_states(
    event_type: str,
) -> None:
    assert_invalid("RunnerEvent", runner_event(event_type))


@pytest.mark.parametrize(
    "mutation",
    ["zero_sequence", "negative_sequence", "created_at_alias", "secret_field"],
)
def test_runner_event_uses_the_explicit_strict_event_envelope(
    mutation: str,
) -> None:
    payload = runner_event()
    if mutation == "zero_sequence":
        payload["sequence"] = 0
    elif mutation == "negative_sequence":
        payload["sequence"] = -1
    elif mutation == "created_at_alias":
        payload["created_at"] = payload["timestamp"]
    elif mutation == "secret_field":
        payload["github_token"] = "secret-canary"

    assert_invalid("RunnerEvent", payload)

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.factory_runner.contract._support import (
    DIGEST_A,
    assert_invalid,
    bind_changed_file_manifest_digest,
    change_set,
    changed_file,
    clean_verification_evidence,
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
    ("operation", "verification_digest"),
    [
        ("author", DIGEST_A),
        ("verify", None),
    ],
)
def test_checkpoint_operation_binds_verification_input(
    operation: str,
    verification_digest: str | None,
) -> None:
    payload = checkpoint()
    payload["operation"] = operation
    payload["verification_change_set_digest"] = verification_digest

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


def _evidence_with_verification_status(
    actual_status: str,
    overall_status: str,
) -> dict[str, object]:
    payload = verification_evidence()
    item = payload["items"][0]
    item.update(
        {
            "phase": "verification",
            "expected_status": "passed",
            "actual_status": actual_status,
            "exit_code": 0,
            "termination_reason": "exited",
            "failure_classification": "none",
        }
    )
    if actual_status == "failed":
        item["exit_code"] = 1
        item["failure_classification"] = "test_failure"
    elif actual_status == "blocked":
        item["exit_code"] = None
        item["termination_reason"] = "not_started"
        item["failure_classification"] = "infrastructure_error"
    elif actual_status == "not_run":
        item["exit_code"] = None
        item["termination_reason"] = "not_started"
    payload["overall_status"] = overall_status
    return payload


@pytest.mark.parametrize(
    ("actual_status", "overall_status"),
    [
        ("passed", "passed"),
        ("failed", "failed"),
        ("blocked", "blocked"),
        ("not_run", "not_run"),
    ],
)
def test_verification_evidence_accepts_derived_overall_status(
    actual_status: str,
    overall_status: str,
) -> None:
    validate(
        "VerificationEvidence",
        _evidence_with_verification_status(actual_status, overall_status),
    )


def test_expected_red_failure_and_passing_green_aggregate_to_passed() -> None:
    payload = verification_evidence()
    green = _evidence_with_verification_status("passed", "passed")["items"][0]
    green["phase"] = "green"
    payload["items"].append(green)

    validate("VerificationEvidence", payload)


@pytest.mark.parametrize(
    ("actual_status", "contradictory_overall_status"),
    [
        ("passed", "failed"),
        ("failed", "passed"),
        ("blocked", "passed"),
        ("not_run", "blocked"),
    ],
)
def test_verification_evidence_rejects_contradictory_overall_status(
    actual_status: str,
    contradictory_overall_status: str,
) -> None:
    assert_invalid(
        "VerificationEvidence",
        _evidence_with_verification_status(
            actual_status,
            contradictory_overall_status,
        ),
    )


def test_non_red_evidence_items_expect_success() -> None:
    payload = _evidence_with_verification_status("passed", "passed")
    payload["items"][0]["expected_status"] = "failed"

    assert_invalid("VerificationEvidence", payload)


def test_clean_verification_contains_only_verification_phase_items() -> None:
    payload = verification_evidence()
    payload["environment_kind"] = "clean_verification"
    payload["change_set_digest"] = DIGEST_A

    assert_invalid("VerificationEvidence", payload)
    validate("VerificationEvidence", clean_verification_evidence())


@pytest.mark.parametrize(
    "mutation",
    [
        "passed_timeout",
        "passed_failure_classification",
        "passed_repository_mutation",
    ],
)
def test_clean_verification_cannot_claim_a_contradictory_pass(
    mutation: str,
) -> None:
    payload = clean_verification_evidence()
    item = payload["items"][0]
    if mutation == "passed_timeout":
        item["termination_reason"] = "timed_out"
        item["failure_classification"] = "timeout"
    elif mutation == "passed_failure_classification":
        item["failure_classification"] = "test_failure"
    else:
        item["repository_files_changed"] = True

    assert_invalid("VerificationEvidence", payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "timeout_without_timeout_classification",
        "timeout_without_failed_status",
        "timeout_classification_without_timeout",
        "not_run_after_process_exit",
    ],
)
def test_evidence_status_termination_and_classification_are_coherent(
    mutation: str,
) -> None:
    payload = _evidence_with_verification_status("failed", "failed")
    item = payload["items"][0]
    if mutation == "timeout_without_timeout_classification":
        item["termination_reason"] = "timed_out"
    elif mutation == "timeout_without_failed_status":
        item.update(
            {
                "termination_reason": "timed_out",
                "failure_classification": "timeout",
                "actual_status": "blocked",
            }
        )
        payload["overall_status"] = "blocked"
    elif mutation == "timeout_classification_without_timeout":
        item["failure_classification"] = "timeout"
    else:
        item.update(
            {
                "actual_status": "not_run",
                "exit_code": None,
                "failure_classification": "none",
            }
        )
        payload["overall_status"] = "not_run"

    assert_invalid("VerificationEvidence", payload)


def test_failed_clean_verification_can_report_repository_mutation() -> None:
    payload = clean_verification_evidence()
    payload["items"][0].update(
        {
            "actual_status": "failed",
            "exit_code": 1,
            "failure_classification": "test_failure",
            "repository_files_changed": True,
        }
    )
    payload["overall_status"] = "failed"

    validate("VerificationEvidence", payload)


@pytest.mark.parametrize("operation", ["add", "modify", "delete", "rename"])
def test_change_set_accepts_complete_file_operations(operation: str) -> None:
    validate("ChangeSet", change_set(operation))


def test_change_set_diff_digest_binds_ordered_changed_file_manifest() -> None:
    payload = change_set()
    added = changed_file("add")
    added["path"] = "src/new.py"
    payload["changed_files"].append(added)
    bind_changed_file_manifest_digest(payload)

    validate("ChangeSet", payload)

    reordered = deepcopy(payload)
    reordered["changed_files"].reverse()
    assert_invalid("ChangeSet", reordered)

    tampered = deepcopy(payload)
    tampered["changed_files"][0]["path"] = "src/renamed.py"
    assert_invalid("ChangeSet", tampered)


def test_change_set_rejects_duplicate_source_paths() -> None:
    first = changed_file("rename")
    second = changed_file("rename")
    second["path"] = "src/second-app.py"
    payload = change_set()
    payload["changed_files"] = [first, second]
    bind_changed_file_manifest_digest(payload)

    assert_invalid("ChangeSet", payload)


def test_change_set_patch_must_not_be_empty() -> None:
    payload = change_set()
    payload["patch"]["byte_size"] = 0

    assert_invalid("ChangeSet", payload)


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
    "completed_stages",
    [
        ["plan"],
        [],
    ],
)
def test_successful_verify_result_requires_only_completed_verify_stage(
    completed_stages: list[str],
) -> None:
    payload = run_result(operation="verify", outcome="succeeded")
    payload["completed_stages"] = completed_stages

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

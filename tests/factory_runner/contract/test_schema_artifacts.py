from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.protocol import (
    load_contract_schema,
    schema_manifest_digest,
    schema_set_digest,
    validate_contract,
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
    change_set,
    checkpoint,
    clean_verification_evidence,
    context_bundle,
    evidence_item,
    run_result,
    run_spec,
    runner_event,
    verification_evidence,
    verification_checkpoint,
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


def test_generated_schemas_do_not_contain_pydantic_constraint_keywords() -> None:
    prohibited = {"ge", "gt", "le", "lt"}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert prohibited.isdisjoint(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for case in CONTRACT_CASES:
        visit(load_contract_schema(case.schema_name))


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


def test_checkpoint_schema_binds_verify_only_stage_authority_and_history() -> None:
    schema = load_contract_schema("checkpoint/v1")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    authoring_authority = verification_checkpoint()
    authoring_authority["authority"]["allowed_stages"] = ["plan", "verify"]
    assert list(validator.iter_errors(authoring_authority))

    authoring_history = verification_checkpoint()
    authoring_history["authority"]["allowed_stages"] = ["plan", "verify"]
    authoring_history["completed_stages"] = ["plan"]
    assert list(validator.iter_errors(authoring_history))

    no_commands = verification_checkpoint()
    no_commands["authority"]["allowed_commands"] = []
    assert list(validator.iter_errors(no_commands))


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
    event_with_case_variant_secret = runner_event()
    event_with_case_variant_secret["sanitised_payload"] = {
        "nested": {"GitHub_Token": "secret-canary"}
    }
    checkpoint_with_unsafe_integer = checkpoint()
    checkpoint_with_unsafe_integer["workflow_state"] = {"value": 10**30}
    run_spec_with_secret_profile = run_spec()
    run_spec_with_secret_profile["policy"]["model_profile"] = "SECRET-model"
    checkpoint_with_secret_profile = checkpoint()
    checkpoint_with_secret_profile["authority"]["model_profile"] = "SECRET-model"
    run_spec_with_digest_line_break = run_spec()
    run_spec_with_digest_line_break["context"]["expected_digest"] += "\n"
    run_spec_with_commit_line_break = run_spec()
    run_spec_with_commit_line_break["repository"]["base_commit_sha"] += "\n"
    run_spec_with_timestamp_line_break = run_spec()
    run_spec_with_timestamp_line_break["created_at"] += "\n"
    run_spec_with_environment_line_break = run_spec()
    run_spec_with_environment_line_break["policy"]["allowed_environment_keys"] = [
        "PATH\n"
    ]
    run_spec_with_profile_line_break = run_spec()
    run_spec_with_profile_line_break["policy"]["network_profile"] = "network\n"
    run_spec_with_capability_line_break = run_spec()
    run_spec_with_capability_line_break["capabilities"]["required"] = ["author\n"]
    run_spec_with_identity_control = run_spec()
    run_spec_with_identity_control["identity"]["run_id"] = "run\nid"
    context_with_invalid_media_type = context_bundle()
    context_with_invalid_media_type["manifest_entries"][0]["media_type"] = "nonsense"
    early_invalid_result_without_identity = run_result(outcome="invalid_input")
    early_invalid_result_without_identity["repository"] = None
    early_invalid_result_without_identity.pop("identity")
    early_invalid_result_without_repository = run_result(outcome="invalid_input")
    early_invalid_result_without_repository["identity"] = None
    early_invalid_result_without_repository.pop("repository")
    evidence_without_runner_image = verification_evidence()
    evidence_without_runner_image["runner"].pop("image")
    evidence_without_runner_source_commit = verification_evidence()
    evidence_without_runner_source_commit["runner"].pop("source_commit")
    result_without_runner_image = run_result()
    result_without_runner_image["runner_build"].pop("image")
    result_without_runner_source_commit = run_result()
    result_without_runner_source_commit["runner_build"].pop("source_commit")
    evidence_with_contradictory_overall_status = verification_evidence()
    evidence_with_contradictory_overall_status["items"][0].update(
        {
            "phase": "verification",
            "expected_status": "passed",
            "actual_status": "failed",
            "exit_code": 1,
            "failure_classification": "test_failure",
        }
    )
    evidence_with_non_red_failure_expectation = verification_evidence()
    evidence_with_non_red_failure_expectation["items"][0].update(
        {
            "phase": "verification",
            "expected_status": "failed",
            "actual_status": "failed",
            "exit_code": 1,
            "failure_classification": "test_failure",
        }
    )
    evidence_with_passing_nonzero_exit = verification_evidence()
    evidence_with_passing_nonzero_exit["items"][0].update(
        {
            "phase": "verification",
            "expected_status": "passed",
            "actual_status": "passed",
            "exit_code": 1,
            "failure_classification": "none",
        }
    )
    clean_evidence_with_red_item = verification_evidence()
    clean_evidence_with_red_item["environment_kind"] = "clean_verification"
    clean_evidence_with_red_item["change_set_digest"] = DIGEST_A
    clean_evidence_with_timeout_pass = clean_verification_evidence()
    clean_evidence_with_timeout_pass["items"][0].update(
        {
            "termination_reason": "timed_out",
            "failure_classification": "timeout",
        }
    )
    clean_evidence_with_mutating_pass = clean_verification_evidence()
    clean_evidence_with_mutating_pass["items"][0]["repository_files_changed"] = True
    evidence_with_timeout_mismatch = clean_verification_evidence()
    evidence_with_timeout_mismatch["items"][0].update(
        {
            "actual_status": "failed",
            "exit_code": 1,
            "termination_reason": "timed_out",
            "failure_classification": "test_failure",
        }
    )
    evidence_with_timeout_mismatch["overall_status"] = "failed"
    evidence_with_not_run_exit = clean_verification_evidence()
    evidence_with_not_run_exit["items"][0].update(
        {
            "actual_status": "not_run",
            "exit_code": None,
            "failure_classification": "none",
        }
    )
    evidence_with_not_run_exit["overall_status"] = "not_run"
    verify_result_with_authoring_stage = run_result(operation="verify")
    verify_result_with_authoring_stage["completed_stages"] = ["plan"]
    verify_result_without_completed_verify = run_result(operation="verify")
    verify_result_without_completed_verify["completed_stages"] = []
    verify_without_commands = verification_run_spec()
    verify_without_commands["policy"]["allowed_commands"] = []
    run_spec_with_nul_command = run_spec()
    run_spec_with_nul_command["policy"]["allowed_commands"] = [["pytest", "\x00"]]
    evidence_with_nul_command = verification_evidence()
    evidence_with_nul_command["items"][0]["command"] = ["pytest", "\x00"]
    context_with_long_repository_path = context_bundle()
    context_with_long_repository_path["manifest_entries"][0]["logical_path"] = (
        "a" * 4097
    )
    run_spec_with_long_policy_path = run_spec()
    run_spec_with_long_policy_path["policy"]["allowed_paths"] = ["a" * 4097]
    run_spec_with_long_prohibited_path = run_spec()
    run_spec_with_long_prohibited_path["policy"]["prohibited_paths"] = ["a" * 4097]
    run_spec_with_long_absolute_path = run_spec()
    run_spec_with_long_absolute_path["workspace"]["path"] = "/" + ("a" * 4096)
    evidence_with_negative_duration = verification_evidence()
    evidence_with_negative_duration["items"][0]["duration_seconds"] = -1
    evidence_with_huge_duration = verification_evidence()
    evidence_with_huge_duration["items"][0]["duration_seconds"] = 10**30
    result_with_long_message = run_result()
    result_with_long_message["message"] = "x" * 4097
    change_set_with_empty_patch = change_set()
    change_set_with_empty_patch["patch"]["byte_size"] = 0
    author_checkpoint_with_verification_digest = checkpoint()
    author_checkpoint_with_verification_digest["verification_change_set_digest"] = (
        DIGEST_A
    )
    verify_checkpoint_without_verification_digest = checkpoint()
    verify_checkpoint_without_verification_digest["operation"] = "verify"
    verify_checkpoint_without_verification_digest["verification_change_set_digest"] = (
        None
    )

    invalid_cases = (
        ("run-spec/v1", author_with_change_set),
        ("run-spec/v1", verify_without_change_set),
        ("verification-evidence/v1", author_evidence_with_future_digest),
        ("run-result/v1", non_input_failure_without_identity),
        ("runner-event/v1", event_with_nested_secret),
        ("runner-event/v1", event_with_case_variant_secret),
        ("checkpoint/v1", checkpoint_with_unsafe_integer),
        ("run-spec/v1", run_spec_with_secret_profile),
        ("checkpoint/v1", checkpoint_with_secret_profile),
        ("run-spec/v1", run_spec_with_digest_line_break),
        ("run-spec/v1", run_spec_with_commit_line_break),
        ("run-spec/v1", run_spec_with_timestamp_line_break),
        ("run-spec/v1", run_spec_with_environment_line_break),
        ("run-spec/v1", run_spec_with_profile_line_break),
        ("run-spec/v1", run_spec_with_capability_line_break),
        ("run-spec/v1", run_spec_with_identity_control),
        ("context-bundle/v1", context_with_invalid_media_type),
        ("run-result/v1", early_invalid_result_without_identity),
        ("run-result/v1", early_invalid_result_without_repository),
        ("verification-evidence/v1", evidence_without_runner_image),
        ("verification-evidence/v1", evidence_without_runner_source_commit),
        ("run-result/v1", result_without_runner_image),
        ("run-result/v1", result_without_runner_source_commit),
        ("verification-evidence/v1", evidence_with_contradictory_overall_status),
        ("verification-evidence/v1", evidence_with_non_red_failure_expectation),
        ("verification-evidence/v1", evidence_with_passing_nonzero_exit),
        ("verification-evidence/v1", clean_evidence_with_red_item),
        ("verification-evidence/v1", clean_evidence_with_timeout_pass),
        ("verification-evidence/v1", clean_evidence_with_mutating_pass),
        ("verification-evidence/v1", evidence_with_timeout_mismatch),
        ("verification-evidence/v1", evidence_with_not_run_exit),
        ("run-result/v1", verify_result_with_authoring_stage),
        ("run-result/v1", verify_result_without_completed_verify),
        ("run-spec/v1", verify_without_commands),
        ("run-spec/v1", run_spec_with_nul_command),
        ("verification-evidence/v1", evidence_with_nul_command),
        ("context-bundle/v1", context_with_long_repository_path),
        ("run-spec/v1", run_spec_with_long_policy_path),
        ("run-spec/v1", run_spec_with_long_prohibited_path),
        ("run-spec/v1", run_spec_with_long_absolute_path),
        ("verification-evidence/v1", evidence_with_negative_duration),
        ("verification-evidence/v1", evidence_with_huge_duration),
        ("run-result/v1", result_with_long_message),
        ("change-set/v1", change_set_with_empty_patch),
        ("checkpoint/v1", author_checkpoint_with_verification_digest),
        ("checkpoint/v1", verify_checkpoint_without_verification_digest),
    )
    for schema_name, payload in invalid_cases:
        validator = Draft202012Validator(
            load_contract_schema(schema_name),
            format_checker=FormatChecker(),
        )
        assert list(validator.iter_errors(payload)), schema_name


def test_integral_json_numbers_have_model_schema_parity() -> None:
    payload = run_spec()
    payload["schema_version"] = 1.0
    payload["policy"]["max_agent_turns"] = 20.0

    validated = validate_contract(payload)
    assert validated.schema_version == 1
    assert validated.policy.max_agent_turns == 20

    validator = Draft202012Validator(
        load_contract_schema("run-spec/v1"),
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(payload)) == []


def test_evidence_schema_accepts_mixed_red_and_green_authoring_evidence() -> None:
    payload = verification_evidence()
    payload["items"].append(evidence_item("green"))

    validator = Draft202012Validator(
        load_contract_schema("verification-evidence/v1"),
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(payload)) == []

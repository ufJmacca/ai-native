from __future__ import annotations

from copy import deepcopy

import pytest

from tests.factory_runner.contract._support import (
    DIGEST_A,
    assert_invalid,
    run_spec,
    validate,
    verification_run_spec,
)


@pytest.mark.parametrize(
    "invalid_path",
    [
        "",
        ".",
        "../outside",
        "src/../outside",
        "/absolute/path",
        "src//app.py",
        "src\\app.py",
        "C:\\app.py",
        "src/app.py/",
        "src/\x00app.py",
    ],
)
def test_repository_paths_must_be_normalised_posix_relative(
    invalid_path: str,
) -> None:
    payload = run_spec()
    payload["policy"]["allowed_paths"] = [invalid_path]

    assert_invalid("RunSpec", payload)


@pytest.mark.parametrize(
    "invalid_digest",
    [
        "a" * 64,
        "sha256:" + ("a" * 63),
        "sha256:" + ("a" * 65),
        "sha256:" + ("A" * 64),
        "sha256:" + ("g" * 64),
        DIGEST_A + " ",
    ],
)
def test_digests_use_exact_lowercase_sha256_form(invalid_digest: str) -> None:
    payload = run_spec()
    payload["context"]["expected_digest"] = invalid_digest

    assert_invalid("RunSpec", payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "verify_with_clean_base",
        "author_with_prepared_verification",
        "commit_stage",
        "pr_stage",
        "missing_allowed_paths",
        "shell_string_command",
        "missing_environment_allowlist",
        "broad_credential_profile",
        "zero_wall_budget",
        "zero_turn_budget",
        "zero_token_budget",
        "resume_path_without_digest",
        "resume_digest_without_path",
        "relative_workspace",
        "relative_context_manifest",
        "relative_output",
        "allowed_and_prohibited_overlap",
    ],
)
def test_run_spec_rejects_conflicting_or_authority_widening_input(
    mutation: str,
) -> None:
    payload = run_spec()
    if mutation == "verify_with_clean_base":
        payload["operation"] = "verify"
    elif mutation == "author_with_prepared_verification":
        payload["workspace"]["initial_state"] = "prepared_verification"
    elif mutation == "commit_stage":
        payload["policy"]["allowed_stages"].append("commit")
    elif mutation == "pr_stage":
        payload["policy"]["allowed_stages"].append("pr")
    elif mutation == "missing_allowed_paths":
        del payload["policy"]["allowed_paths"]
    elif mutation == "shell_string_command":
        payload["policy"]["allowed_commands"] = ["pytest -q"]
    elif mutation == "missing_environment_allowlist":
        del payload["policy"]["allowed_environment_keys"]
    elif mutation == "broad_credential_profile":
        payload["policy"]["credential_profile"] = "host-credentials"
    elif mutation == "zero_wall_budget":
        payload["policy"]["max_wall_seconds"] = 0
    elif mutation == "zero_turn_budget":
        payload["policy"]["max_agent_turns"] = 0
    elif mutation == "zero_token_budget":
        payload["policy"]["max_model_tokens"] = 0
    elif mutation == "resume_path_without_digest":
        payload["resume"]["checkpoint_path"] = "/factory/input/resume/checkpoint.json"
    elif mutation == "resume_digest_without_path":
        payload["resume"]["expected_digest"] = DIGEST_A
    elif mutation == "relative_workspace":
        payload["workspace"]["path"] = "workspace/target"
    elif mutation == "relative_context_manifest":
        payload["context"]["manifest_path"] = "context/context-bundle.json"
    elif mutation == "relative_output":
        payload["outputs"]["output_dir"] = "factory/output"
    elif mutation == "allowed_and_prohibited_overlap":
        payload["policy"]["prohibited_paths"] = ["src/app.py"]

    assert_invalid("RunSpec", payload)


def test_verify_operation_accepts_only_prepared_verification_authority() -> None:
    validate("RunSpec", verification_run_spec())


@pytest.mark.parametrize(
    "mutation",
    [
        "author_with_verification_input",
        "verify_without_verification_input",
        "verify_with_unbound_change_set",
    ],
)
def test_verification_input_is_operation_conditional_and_digest_bound(
    mutation: str,
) -> None:
    if mutation == "author_with_verification_input":
        payload = run_spec()
        payload["verification_input"] = {
            "change_set_path": "/factory/input/verification/change-set.json",
            "expected_digest": DIGEST_A,
        }
    else:
        payload = verification_run_spec()
        if mutation == "verify_without_verification_input":
            payload["verification_input"] = None
        else:
            payload["verification_input"]["expected_digest"] = None

    assert_invalid("RunSpec", payload)


def test_model_profile_is_an_opaque_identifier_not_a_secret() -> None:
    payload = run_spec()
    payload["policy"]["model_profile"] = "https://user:secret@example.invalid/model"

    assert_invalid("RunSpec", payload)


def test_run_spec_validation_does_not_rewrite_authority_lists() -> None:
    payload = run_spec()
    original_policy = deepcopy(payload["policy"])

    validated = validate("RunSpec", payload).model_dump(mode="json")

    assert validated["policy"] == original_policy

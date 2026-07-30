from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ai_native.factory_runner.protocol import contract_document_digest
from tests.factory_runner.admission._fixtures import (
    AdmissionCase,
    PLACEHOLDER_DIGEST,
    assert_read_only,
)
from tests.factory_runner.admission.conftest import admit


def assert_admission_rejected(
    admission_api: ModuleType,
    case: AdmissionCase,
    reason_code: str,
    **admission_overrides: Any,
) -> None:
    def reject() -> None:
        with pytest.raises(admission_api.FactoryAdmissionError) as captured:
            admit(admission_api, case, **admission_overrides)
        assert captured.value.reason_code == reason_code

    assert_read_only(case.root, reject)


def test_valid_inputs_are_admitted_without_filesystem_mutation(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    validated = assert_read_only(
        admission_case.root,
        lambda: admit(admission_api, admission_case),
    )

    assert isinstance(validated, admission_api.ValidatedInputs)


@pytest.mark.parametrize(
    ("run_spec_operation", "cli_operation"),
    [
        ("author", "verify"),
        ("verify", "author"),
    ],
)
def test_cli_selected_operation_must_match_run_spec(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    run_spec_operation: str,
    cli_operation: str,
) -> None:
    if run_spec_operation == "verify":
        admission_case.prepare_verification()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
        expected_operation=cli_operation,
    )


def test_cli_output_dir_must_exactly_match_run_spec(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    different_output = admission_case.root / "different-output"
    different_output.mkdir()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
        output_dir=different_output,
    )


def test_output_dir_inside_workspace_is_denied(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.set_output_dir(admission_case.workspace / "factory-output")

    assert_admission_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_context_bundle_self_digest_is_verified(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.context_bundle["trusted_policy_summary"] = [
        "Content changed after the bundle was signed."
    ]
    admission_case.write_context_bundle()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "digest_mismatch",
    )


def test_context_bundle_expected_digest_is_verified(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["context"]["expected_digest"] = PLACEHOLDER_DIGEST
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "digest_mismatch",
    )


@pytest.mark.parametrize(
    "identity_field",
    [
        "work_item_id",
        "work_item_revision_id",
        "delivery_phase_id",
        "run_id",
        "attempt_id",
        "correlation_id",
    ],
)
def test_context_identity_must_exactly_match_run_spec(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    identity_field: str,
) -> None:
    admission_case.context_bundle["identity"][identity_field] = (
        f"different-{identity_field}"
    )
    admission_case.rebind_context_bundle()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


@pytest.mark.parametrize(
    ("repository_field", "different_value"),
    [
        ("repository_id", "different-repository"),
        ("display_name", "different/repository"),
        ("base_commit_sha", "b" * 40),
    ],
)
def test_context_repository_identity_must_exactly_match_run_spec(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    repository_field: str,
    different_value: str,
) -> None:
    admission_case.context_bundle["repository"][repository_field] = different_value
    admission_case.rebind_context_bundle()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


@pytest.mark.parametrize("mismatch", ["outcome", "acceptance_criteria"])
def test_normalised_work_item_must_match_run_spec_task(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    mismatch: str,
) -> None:
    if mismatch == "outcome":
        admission_case.context_bundle["work_item_revision"]["outcome"] = (
            "A conflicting outcome."
        )
    else:
        admission_case.context_bundle["work_item_revision"]["acceptance_criteria"] = [
            "A conflicting acceptance criterion."
        ]
    admission_case.rebind_context_bundle()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


@pytest.mark.parametrize("entry_index", [0, 1, 2])
@pytest.mark.parametrize(
    "object_failure",
    ["digest", "size", "missing"],
)
def test_every_context_manifest_object_is_verified(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    entry_index: int,
    object_failure: str,
) -> None:
    object_path = admission_case.context_object_path(entry_index)
    if object_failure == "digest":
        content = bytearray(object_path.read_bytes())
        content[-1] = (content[-1] + 1) % 256
        object_path.write_bytes(bytes(content))
    elif object_failure == "size":
        admission_case.context_entries[entry_index]["byte_size"] += 1
        admission_case.rebind_context_bundle()
    else:
        object_path.unlink()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "digest_mismatch",
    )


def test_unsupported_required_capability_is_rejected(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["capabilities"]["required"].append(
        "unsupported-required-capability"
    )
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "unsupported_capability",
    )


def test_required_structured_events_are_rejected_until_an03(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["capabilities"] = {
        "required": ["author", "structured-events"],
        "optional": [],
    }
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "unsupported_capability",
    )


def test_stdout_event_streaming_is_rejected_until_an03(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["outputs"]["stream_events_to_stdout"] = True
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "unsupported_capability",
    )


def test_author_requires_declared_deterministic_verification(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["policy"]["allowed_commands"] = []
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_prohibited_declared_command_is_rejected_before_authoring(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.run_spec["policy"]["allowed_commands"] = [
        ["git", "push", "origin", "HEAD"]
    ]
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


@pytest.mark.parametrize(
    "reserved_key",
    [
        "AINATIVE_FACTORY_AGENT_COMMAND_JSON",
        "ATTEMPT_GATEWAY_TOKEN_FILE",
    ],
)
def test_runner_bootstrap_keys_cannot_be_granted_to_project_commands(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    reserved_key: str,
) -> None:
    admission_case.run_spec["policy"]["allowed_environment_keys"].append(reserved_key)
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_gateway_token_file_path_must_be_absolute(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
        environment={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "ATTEMPT_GATEWAY_TOKEN_FILE": "relative/gateway-token",
        },
    )


@pytest.mark.parametrize(
    ("profile_field", "unsupported_profile"),
    [
        ("model_profile", "unsupported-model"),
        ("network_profile", "open-internet"),
    ],
)
def test_unsupported_runtime_profile_is_denied(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    profile_field: str,
    unsupported_profile: str,
) -> None:
    admission_case.run_spec["policy"][profile_field] = unsupported_profile
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_an02_rejects_resume_input_instead_of_silently_ignoring_it(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    checkpoint_path = admission_case.root / "input" / "resume" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("{}\n", encoding="utf-8")
    admission_case.run_spec["resume"] = {
        "checkpoint_path": str(checkpoint_path.resolve()),
        "expected_digest": PLACEHOLDER_DIGEST,
    }
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "checkpoint_incompatible",
    )


@pytest.mark.parametrize("reference_field", ["patch", "evidence_refs"])
def test_every_change_set_artifact_reference_is_digest_verified(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    reference_field: str,
) -> None:
    admission_case.prepare_verification()
    change_set_path = Path(
        admission_case.run_spec["verification_input"]["change_set_path"]
    )
    change_set = json.loads(change_set_path.read_text(encoding="utf-8"))
    reference = (
        change_set["patch"]
        if reference_field == "patch"
        else change_set["evidence_refs"][0]
    )
    referenced_path = admission_case.run_spec_path.parent / reference["path"]
    original = referenced_path.read_bytes()
    referenced_path.write_bytes(b"x" * len(original))

    assert_admission_rejected(
        admission_api,
        admission_case,
        "digest_mismatch",
    )


@pytest.mark.parametrize(
    "tamper",
    ["criterion", "evidence-set-digest"],
)
def test_change_set_claims_must_bind_the_run_and_authoring_evidence(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    tamper: str,
) -> None:
    admission_case.prepare_verification()
    change_set_path = Path(
        admission_case.run_spec["verification_input"]["change_set_path"]
    )
    change_set = json.loads(change_set_path.read_text(encoding="utf-8"))
    if tamper == "criterion":
        change_set["acceptance_criteria_results"][0]["criterion"] = (
            "An unrelated criterion."
        )
    else:
        change_set["evidence_set_digest"] = "sha256:" + ("0" * 64)
    change_set["change_set_digest"] = "sha256:" + ("0" * 64)
    change_set["change_set_digest"] = contract_document_digest(change_set)
    change_set_path.write_text(
        json.dumps(change_set, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    admission_case.run_spec["verification_input"]["expected_digest"] = change_set[
        "change_set_digest"
    ]
    admission_case.write_run_spec()

    assert_admission_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )

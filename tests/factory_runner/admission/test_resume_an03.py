from __future__ import annotations

from copy import deepcopy
from types import ModuleType

import pytest

from ai_native.factory_runner.checkpoints import CheckpointManager
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.protocol import contract_document_digest, sha256_digest
from tests.factory_runner.admission._fixtures import (
    AdmissionCase,
    PLACEHOLDER_DIGEST,
    assert_read_only,
)
from tests.factory_runner.admission.conftest import admit
from tests.factory_runner.contract._support import (
    bind_self_digest,
    checkpoint as checkpoint_fixture,
)


STATE = "checkpoints/1/objects/workflow-state.json"


def _enable_resume(
    case: AdmissionCase,
    *,
    producer_attempt_id: str | None = None,
    resumed_attempt_id: str = "attempt-admission-resumed",
) -> Checkpoint:
    state = b'{"stage":"plan","portable":true}\n'
    producer_run_spec = deepcopy(case.run_spec)
    if producer_attempt_id is not None:
        producer_run_spec["identity"]["attempt_id"] = producer_attempt_id
    payload = checkpoint_fixture()
    payload["identity"] = deepcopy(producer_run_spec["identity"])
    payload["repository"] = deepcopy(producer_run_spec["repository"])
    payload["producer_attempt_id"] = producer_run_spec["identity"]["attempt_id"]
    payload["context_bundle_digest"] = case.context_bundle["bundle_digest"]
    payload["run_spec_digest"] = contract_document_digest(producer_run_spec)
    payload["compatibility"]["minimum_runner_version"] = "0.0.0"
    payload["workspace_patch_digest"] = None
    payload["artifact_manifest"] = [
        {
            "path": STATE,
            "media_type": "application/json",
            "byte_size": len(state),
            "digest": sha256_digest(state),
        }
    ]
    payload["object_digests"] = [sha256_digest(state)]
    payload["authority"] = deepcopy(case.run_spec["policy"])
    payload["budgets"]["remaining"] = {
        "wall_seconds": 290,
        "agent_turns": 8,
        "model_tokens": 19_000,
    }
    payload["checkpoint_digest"] = PLACEHOLDER_DIGEST
    checkpoint = Checkpoint.model_validate(
        bind_self_digest(payload, "checkpoint_digest")
    )
    root = case.root / "input" / "resume"
    root.mkdir()
    reference = CheckpointManager(root).write_safe_boundary(
        checkpoint=checkpoint,
        objects={STATE: state},
    )
    case.run_spec["identity"] = deepcopy(producer_run_spec["identity"])
    case.run_spec["identity"]["attempt_id"] = resumed_attempt_id
    case.run_spec["repository"] = deepcopy(producer_run_spec["repository"])
    case.run_spec["resume"] = {
        "checkpoint_path": str((root / reference.path).resolve()),
        "expected_digest": checkpoint.checkpoint_digest,
    }
    case.write_run_spec()
    return checkpoint


def test_admission_allows_new_attempt_with_original_digest_bound_context(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    producer_attempt = admission_case.context_bundle["identity"]["attempt_id"]
    checkpoint = _enable_resume(admission_case)

    inputs = assert_read_only(
        admission_case.root,
        lambda: admit(admission_api, admission_case),
    )

    assert inputs.run_spec.identity.attempt_id == "attempt-admission-resumed"
    assert inputs.context_bundle.identity.attempt_id == producer_attempt
    assert inputs.checkpoint.checkpoint == checkpoint
    assert checkpoint.producer_attempt_id == producer_attempt


def test_admission_allows_third_attempt_to_resume_second_attempt_checkpoint_with_original_context(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    original_context_attempt = admission_case.context_bundle["identity"]["attempt_id"]
    checkpoint = _enable_resume(
        admission_case,
        producer_attempt_id="attempt-admission-second",
        resumed_attempt_id="attempt-admission-third",
    )

    inputs = assert_read_only(
        admission_case.root,
        lambda: admit(admission_api, admission_case),
    )

    assert inputs.context_bundle.identity.attempt_id == original_context_attempt
    assert checkpoint.producer_attempt_id == "attempt-admission-second"
    assert inputs.run_spec.identity.attempt_id == "attempt-admission-third"
    assert (
        inputs.context_bundle.repository
        == checkpoint.repository
        == inputs.run_spec.repository
    )
    assert (
        inputs.context_bundle.bundle_digest
        == checkpoint.context_bundle_digest
        == inputs.run_spec.context.expected_digest
    )
    assert (
        inputs.context_bundle.identity.model_dump(
            mode="json",
            exclude={"attempt_id"},
        )
        == checkpoint.identity.model_dump(
            mode="json",
            exclude={"attempt_id"},
        )
        == inputs.run_spec.identity.model_dump(
            mode="json",
            exclude={"attempt_id"},
        )
    )


@pytest.mark.parametrize(
    ("boundary", "reason_code"),
    [
        ("run", "invalid_input"),
        ("revision", "invalid_input"),
        ("repository", "invalid_input"),
        ("context", "checkpoint_incompatible"),
    ],
)
def test_chained_resume_rejects_cross_lineage_inputs(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    boundary: str,
    reason_code: str,
) -> None:
    if boundary == "run":
        admission_case.run_spec["identity"]["run_id"] = "run-other"
    elif boundary == "revision":
        admission_case.run_spec["identity"]["work_item_revision_id"] = "revision-other"
    elif boundary == "repository":
        admission_case.run_spec["repository"]["repository_id"] = "repository-other"

    _enable_resume(
        admission_case,
        producer_attempt_id="attempt-admission-second",
        resumed_attempt_id="attempt-admission-third",
    )
    if boundary == "context":
        admission_case.context_bundle["context_bundle_id"] = "context-bundle-other"
        admission_case.rebind_context_bundle()

    def reject() -> None:
        with pytest.raises(admission_api.FactoryAdmissionError) as captured:
            admit(admission_api, admission_case)
        assert captured.value.reason_code == reason_code

    assert_read_only(admission_case.root, reject)

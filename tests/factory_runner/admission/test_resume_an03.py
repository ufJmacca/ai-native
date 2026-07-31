from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import ModuleType

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


def _enable_resume(case: AdmissionCase) -> Checkpoint:
    state = b'{"stage":"plan","portable":true}\n'
    payload = checkpoint_fixture()
    payload["identity"] = deepcopy(case.run_spec["identity"])
    payload["repository"] = deepcopy(case.run_spec["repository"])
    payload["producer_attempt_id"] = case.run_spec["identity"]["attempt_id"]
    payload["context_bundle_digest"] = case.context_bundle["bundle_digest"]
    payload["run_spec_digest"] = contract_document_digest(case.run_spec)
    payload["compatibility"]["minimum_runner_version"] = "0.0.0"
    payload["workspace_patch_digest"] = None
    payload["artifact_manifest"] = [{
        "path": STATE,
        "media_type": "application/json",
        "byte_size": len(state),
        "digest": sha256_digest(state),
    }]
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
    case.run_spec["identity"]["attempt_id"] = "attempt-admission-resumed"
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

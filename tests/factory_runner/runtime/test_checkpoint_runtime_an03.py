from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.checkpoint_runtime import (
    CheckpointRuntimeError,
    WrittenCheckpoint,
    write_checkpoint_boundary,
)
from ai_native.factory_runner.checkpoints import CheckpointManager
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.checkpoint import ResourceBudget
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.protocol import (
    contract_document_digest,
    verify_contract_digest,
)
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner
from tests.factory_runner.contract._support import (
    DIGEST_A,
    DIGEST_B,
    run_spec as run_spec_fixture,
    verification_run_spec,
)


CREATED_AT = "2026-07-31T00:00:00Z"


def _spec(*, operation: str = "author") -> RunSpec:
    payload = run_spec_fixture() if operation == "author" else verification_run_spec()
    return RunSpec.model_validate(payload)


def _evidence_ref() -> ArtifactReference:
    return ArtifactReference(
        path="evidence/verification-evidence.json",
        media_type="application/json",
        byte_size=3,
        digest=sha256_digest(b"red"),
    )


def _read_objects(
    root: Path,
    written: WrittenCheckpoint,
) -> Mapping[str, bytes]:
    return {
        reference.path: (root / reference.path).read_bytes()
        for reference in written.checkpoint.artifact_manifest
    }


def test_author_checkpoint_is_self_digested_content_addressed_and_atomic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    spec = _spec()
    state = {
        "stage": "loop",
        "portable": {"state_path": "scratch/loop-state.json"},
    }
    patch = b"diff --git a/src/app.py b/src/app.py\n"

    written = write_checkpoint_boundary(
        CheckpointManager(root),
        run_spec=spec,
        context_bundle_digest=spec.context.expected_digest,
        sequence=7,
        created_at=CREATED_AT,
        completed_stages=("plan", "loop"),
        next_permitted_stage="verify",
        workflow_state=state,
        consumed=ResourceBudget(
            wall_seconds=12,
            agent_turns=3,
            model_tokens=1_234,
        ),
        workspace_patch=patch,
        evidence_refs=(_evidence_ref(),),
        decisions=("Retain only portable state.",),
    )

    checkpoint = written.checkpoint
    verify_contract_digest(checkpoint)
    assert written.reference.path == "checkpoints/7/checkpoint.json"
    assert checkpoint.identity == spec.identity
    assert checkpoint.repository == spec.repository
    assert checkpoint.producer_attempt_id == spec.identity.attempt_id
    assert checkpoint.context_bundle_digest == spec.context.expected_digest
    assert checkpoint.run_spec_digest == contract_document_digest(spec)
    assert checkpoint.operation == "author"
    assert checkpoint.verification_change_set_digest is None
    assert checkpoint.authority == spec.policy
    assert checkpoint.completed_stages == ("plan", "loop")
    assert checkpoint.next_permitted_stage == "verify"
    assert checkpoint.evidence_refs == (_evidence_ref(),)
    assert checkpoint.budgets.consumed == ResourceBudget(
        wall_seconds=12,
        agent_turns=3,
        model_tokens=1_234,
    )
    assert checkpoint.budgets.remaining == ResourceBudget(
        wall_seconds=588,
        agent_turns=17,
        model_tokens=48_766,
    )

    objects = _read_objects(root, written)
    expected_state = canonical_json_bytes(state)
    assert set(objects.values()) == {expected_state, patch}
    for reference in checkpoint.artifact_manifest:
        object_hash = reference.digest.removeprefix("sha256:")
        assert reference.path == f"checkpoints/7/objects/{object_hash}"
        assert reference.digest == sha256_digest(objects[reference.path])
    assert checkpoint.workspace_patch_digest == sha256_digest(patch)
    assert set(checkpoint.object_digests) == {
        sha256_digest(expected_state),
        sha256_digest(patch),
    }
    assert (root / written.reference.path).is_file()
    assert not tuple(root.rglob("*.tmp"))


def test_verify_checkpoint_derives_verify_only_lineage_and_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    spec = _spec(operation="verify")

    written = write_checkpoint_boundary(
        CheckpointManager(root),
        run_spec=spec,
        context_bundle_digest=spec.context.expected_digest,
        sequence=1,
        created_at=CREATED_AT,
        completed_stages=("verify",),
        next_permitted_stage=None,
        workflow_state={"stage": "verify", "status": "complete"},
        consumed=ResourceBudget(
            wall_seconds=600,
            agent_turns=20,
            model_tokens=50_000,
        ),
    )

    checkpoint = written.checkpoint
    verify_contract_digest(checkpoint)
    assert checkpoint.operation == "verify"
    assert checkpoint.verification_change_set_digest == DIGEST_B
    assert checkpoint.authority.allowed_stages == ("verify",)
    assert checkpoint.authority.allowed_commands == (("pytest", "-q"),)
    assert checkpoint.workspace_patch_digest is None
    assert checkpoint.budgets.remaining == ResourceBudget(
        wall_seconds=0,
        agent_turns=0,
        model_tokens=0,
    )


@pytest.mark.parametrize(
    ("state", "decisions"),
    [
        ({"scratch": "/private/tmp/factory-state"}, ()),
        ({"credential": "opaque-value"}, ()),
        ({"note": "authorization: opaque-value"}, ()),
        ({"status": "ready"}, ("Read /Users/operator/private.txt.",)),
    ],
)
def test_checkpoint_rejects_nonportable_or_credential_state_before_writing(
    tmp_path: Path,
    state: dict[str, str],
    decisions: tuple[str, ...],
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    spec = _spec()

    with pytest.raises(
        CheckpointRuntimeError,
        match="portable|credential",
    ):
        write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=spec.context.expected_digest,
            sequence=1,
            created_at=CREATED_AT,
            completed_stages=("plan",),
            next_permitted_stage="loop",
            workflow_state=state,
            consumed=ResourceBudget(
                wall_seconds=0,
                agent_turns=0,
                model_tokens=0,
            ),
            decisions=decisions,
        )

    assert list(root.iterdir()) == []


def test_checkpoint_scans_every_durable_byte_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    spec = _spec()
    canary = b"credential-material-known-to-this-attempt"
    scanner = SecretScanner(SecretPolicy((("attempt-token", canary),)))

    with pytest.raises(FactoryPolicyViolation) as caught:
        write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=spec.context.expected_digest,
            sequence=1,
            created_at=CREATED_AT,
            completed_stages=("plan",),
            next_permitted_stage="loop",
            workflow_state={"stage": "plan"},
            consumed=ResourceBudget(
                wall_seconds=1,
                agent_turns=1,
                model_tokens=1,
            ),
            workspace_patch=b"diff --git a/a b/a\n+" + canary + b"\n",
            secret_scanner=scanner,
        )

    assert canary.decode() not in str(caught.value)
    assert list(root.iterdir()) == []


def test_checkpoint_rejects_digest_budget_and_authority_mismatch_without_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    spec = _spec()

    with pytest.raises(CheckpointRuntimeError, match="context"):
        write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=sha256_digest(b"different"),
            sequence=1,
            created_at=CREATED_AT,
            completed_stages=("plan",),
            next_permitted_stage="loop",
            workflow_state={"stage": "plan"},
            consumed=ResourceBudget(
                wall_seconds=0,
                agent_turns=0,
                model_tokens=0,
            ),
        )
    with pytest.raises(CheckpointRuntimeError, match="budget"):
        write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=spec.context.expected_digest,
            sequence=1,
            created_at=CREATED_AT,
            completed_stages=("plan",),
            next_permitted_stage="loop",
            workflow_state={"stage": "plan"},
            consumed=ResourceBudget(
                wall_seconds=601,
                agent_turns=0,
                model_tokens=0,
            ),
        )
    with pytest.raises(CheckpointRuntimeError, match="authority"):
        write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=spec.context.expected_digest,
            sequence=1,
            created_at=CREATED_AT,
            completed_stages=("intake",),
            next_permitted_stage="loop",
            workflow_state={"stage": "intake"},
            consumed=ResourceBudget(
                wall_seconds=0,
                agent_turns=0,
                model_tokens=0,
            ),
        )

    assert list(root.iterdir()) == []


def test_checkpoint_identity_and_object_addresses_are_deterministic(
    tmp_path: Path,
) -> None:
    spec_payload = run_spec_fixture()
    spec_payload["capabilities"]["required"] = ["structured-events", "author"]
    spec_payload["capabilities"]["optional"] = []
    spec = RunSpec.model_validate(deepcopy(spec_payload))

    def write(root: Path) -> WrittenCheckpoint:
        root.mkdir()
        return write_checkpoint_boundary(
            CheckpointManager(root),
            run_spec=spec,
            context_bundle_digest=DIGEST_A,
            sequence=3,
            created_at=CREATED_AT,
            completed_stages=("plan",),
            next_permitted_stage="loop",
            workflow_state={"stage": "plan", "values": [1, 2, 3]},
            consumed=ResourceBudget(
                wall_seconds=4,
                agent_turns=1,
                model_tokens=9,
            ),
            workspace_patch=b"deterministic patch bytes\n",
        )

    first = write(tmp_path / "first")
    second = write(tmp_path / "second")

    assert first.checkpoint == second.checkpoint
    assert first.checkpoint.checkpoint_digest == second.checkpoint.checkpoint_digest
    assert first.checkpoint.artifact_manifest == second.checkpoint.artifact_manifest
    assert first.reference.digest == second.reference.digest

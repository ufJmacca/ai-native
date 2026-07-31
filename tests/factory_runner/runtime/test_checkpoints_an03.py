from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ai_native.factory_runner.checkpoints import CheckpointError, CheckpointManager
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.protocol import sha256_digest
from tests.factory_runner.admission._fixtures import filesystem_snapshot
from tests.factory_runner.contract._support import (
    bind_self_digest,
    checkpoint as checkpoint_fixture,
    resuming_run_spec,
)


PATCH = "checkpoints/1/objects/workspace.patch"
STATE = "checkpoints/1/objects/workflow-state.json"
CAPABILITIES = ("author", "structured-events")


def _ref(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "media_type": "application/octet-stream",
        "byte_size": len(content),
        "digest": sha256_digest(content),
    }


def _bundle(
    root: Path,
    patch: bytes = b"not a valid patch\n",
) -> tuple[Checkpoint, RunSpec, dict[str, bytes]]:
    state = b'{"stage":"loop","portable":true}\n'
    payload = checkpoint_fixture()
    payload["artifact_manifest"] = [_ref(PATCH, patch), _ref(STATE, state)]
    payload["workspace_patch_digest"] = sha256_digest(patch)
    payload["object_digests"] = [sha256_digest(patch), sha256_digest(state)]
    checkpoint = Checkpoint.model_validate(
        bind_self_digest(payload, "checkpoint_digest")
    )
    run_payload = resuming_run_spec()
    run_payload["resume"] = {
        "checkpoint_path": str((root / "checkpoints/1/checkpoint.json").resolve()),
        "expected_digest": checkpoint.checkpoint_digest,
    }
    return (
        checkpoint,
        RunSpec.model_validate(run_payload),
        {
            PATCH: patch,
            STATE: state,
        },
    )


def _load(
    manager: CheckpointManager,
    path: str,
    checkpoint: Checkpoint,
    run_spec: RunSpec,
):
    return manager.load_for_resume(
        path,
        expected_digest=checkpoint.checkpoint_digest,
        run_spec=run_spec,
        supported_capabilities=CAPABILITIES,
        runner_version="1.4.0",
    )


def test_safe_boundary_write_is_atomic_digest_bound_and_immutable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkpoints"
    root.mkdir()
    checkpoint, run_spec, objects = _bundle(root)
    manager = CheckpointManager(root)

    with pytest.raises(CheckpointError, match="missing|manifest"):
        manager.write_safe_boundary(
            checkpoint=checkpoint,
            objects={PATCH: objects[PATCH]},
        )
    assert list(root.iterdir()) == []

    reference = manager.write_safe_boundary(
        checkpoint=checkpoint,
        objects=objects,
    )
    loaded = _load(manager, reference.path, checkpoint, run_spec)
    assert loaded.checkpoint == checkpoint
    assert dict(loaded.objects) == objects
    assert reference.digest == sha256_digest((root / reference.path).read_bytes())
    assert not tuple(root.rglob("*.tmp"))

    before = filesystem_snapshot(root)
    with pytest.raises(CheckpointError, match="immutable|already exists"):
        manager.write_safe_boundary(checkpoint=checkpoint, objects=objects)
    assert filesystem_snapshot(root) == before


def test_resume_load_accepts_a_new_attempt_in_the_same_run(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    root.mkdir()
    checkpoint, run_spec, objects = _bundle(root)
    manager = CheckpointManager(root)
    reference = manager.write_safe_boundary(checkpoint=checkpoint, objects=objects)

    loaded = _load(manager, reference.path, checkpoint, run_spec)

    assert run_spec.identity.attempt_id != checkpoint.producer_attempt_id
    assert loaded.checkpoint == checkpoint


@pytest.mark.parametrize("damage", ["corrupt", "missing", "symlink"])
def test_resume_load_rejects_untrusted_objects(
    tmp_path: Path,
    damage: str,
) -> None:
    root = tmp_path / "checkpoints"
    root.mkdir()
    checkpoint, run_spec, objects = _bundle(root)
    manager = CheckpointManager(root)
    reference = manager.write_safe_boundary(checkpoint=checkpoint, objects=objects)
    target = root / STATE
    if damage == "corrupt":
        target.write_bytes(b"x" * len(objects[STATE]))
    else:
        target.unlink()
        if damage == "symlink":
            outside = tmp_path / "outside"
            outside.write_bytes(objects[STATE])
            target.symlink_to(outside)

    with pytest.raises(CheckpointError, match="digest|missing|symbolic link"):
        _load(manager, reference.path, checkpoint, run_spec)


def test_failed_restore_rolls_back_the_entire_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(workspace)],
        check=True,
        capture_output=True,
    )
    source = workspace / "app.py"
    source.write_text("before\n", encoding="utf-8")
    root = tmp_path / "checkpoints"
    root.mkdir()
    checkpoint, run_spec, objects = _bundle(root)
    manager = CheckpointManager(root)
    reference = manager.write_safe_boundary(checkpoint=checkpoint, objects=objects)
    loaded = _load(manager, reference.path, checkpoint, run_spec)
    before = filesystem_snapshot(workspace)

    with pytest.raises(CheckpointError, match="restore|patch"):
        manager.restore_transactionally(loaded, workspace=workspace)

    assert filesystem_snapshot(workspace) == before

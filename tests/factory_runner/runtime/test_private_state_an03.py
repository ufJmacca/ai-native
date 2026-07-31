from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat

import pytest

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.checkpoint_runtime import (
    CheckpointStateObject,
    build_checkpoint_bundle,
)
from ai_native.factory_runner.contracts.checkpoint import ResourceBudget
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.private_state import (
    PRIVATE_ROOT_TOKEN,
    PRIVATE_RUN_TOKEN,
    PRIVATE_STATE_WORKFLOW_KEY,
    WORKSPACE_ROOT_TOKEN,
    PrivateStateError,
    PrivateStateLimits,
    restore_private_run_directory,
    snapshot_private_run_directory,
)
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner
from tests.factory_runner.contract._support import run_spec as run_spec_fixture


CREATED_AT = "2026-07-31T00:00:00Z"


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700, parents=True)
    run_dir = private_root / "state" / "old-attempt"
    run_dir.mkdir(mode=0o700, parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    return private_root, run_dir, workspace


def _write_state_fixture(
    private_root: Path,
    run_dir: Path,
    workspace: Path,
) -> dict[str, bytes]:
    state = (
        json.dumps(
            {
                "run_dir": str(run_dir),
                "spec_path": str(run_dir / "spec.md"),
                "workspace_root": str(workspace),
                "private_state_root": str(private_root / "state"),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    spec = (f"workspace={workspace}\nrun={run_dir}\nprivate={private_root}\n").encode()
    nested = run_dir / "nested"
    nested.mkdir(mode=0o700)
    empty = run_dir / "empty"
    empty.mkdir(mode=0o755)
    state_path = run_dir / "state.json"
    state_path.write_bytes(state)
    state_path.chmod(0o600)
    spec_path = nested / "spec.md"
    spec_path.write_bytes(spec)
    spec_path.chmod(0o644)
    return {
        "state.json": state,
        "nested/spec.md": spec,
    }


def _spec() -> RunSpec:
    return RunSpec.model_validate(run_spec_fixture())


def test_snapshot_is_deterministic_tokenised_and_checkpoint_addressable(
    tmp_path: Path,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    original = _write_state_fixture(private_root, run_dir, workspace)

    first = snapshot_private_run_directory(
        run_dir,
        private_root=private_root,
        workspace_root=workspace,
    )
    second = snapshot_private_run_directory(
        run_dir,
        private_root=private_root,
        workspace_root=workspace,
    )

    assert canonical_json_bytes(first.descriptor) == canonical_json_bytes(
        second.descriptor
    )
    assert first.objects == second.objects
    assert first.descriptor["schema"] == "private-author-state/v1"
    assert [entry["path"] for entry in first.descriptor["files"]] == [
        "nested/spec.md",
        "state.json",
    ]
    assert [entry["path"] for entry in first.descriptor["directories"]] == [
        "empty",
        "nested",
    ]
    assert all(
        not Path(entry["path"]).is_absolute() and ".." not in Path(entry["path"]).parts
        for entry in first.descriptor["files"]
    )
    object_bytes = b"\n".join(item.content for item in first.objects)
    assert str(private_root).encode() not in object_bytes
    assert str(run_dir).encode() not in object_bytes
    assert str(workspace).encode() not in object_bytes
    assert PRIVATE_ROOT_TOKEN in object_bytes
    assert PRIVATE_RUN_TOKEN in object_bytes
    assert WORKSPACE_ROOT_TOKEN in object_bytes
    assert sum(item.byte_size for item in first.objects) <= sum(
        len(value) for value in original.values()
    )

    spec = _spec()
    bundle = build_checkpoint_bundle(
        run_spec=spec,
        context_bundle_digest=spec.context.expected_digest,
        sequence=2,
        created_at=CREATED_AT,
        completed_stages=("plan", "loop"),
        next_permitted_stage="verify",
        workflow_state={
            "stage": "loop",
            PRIVATE_STATE_WORKFLOW_KEY: first.descriptor,
        },
        consumed=ResourceBudget(
            wall_seconds=1,
            agent_turns=1,
            model_tokens=1,
        ),
        state_objects=first.objects,
    )

    private_digests = {item.digest for item in first.objects}
    assert private_digests.issubset(set(bundle.checkpoint.object_digests))
    assert all(
        reference.path
        == ("checkpoints/2/objects/" + reference.digest.removeprefix("sha256:"))
        for reference in bundle.checkpoint.artifact_manifest
    )


def test_restore_rebinds_tokens_beneath_a_fresh_private_root(
    tmp_path: Path,
) -> None:
    old_private, old_run, old_workspace = _roots(tmp_path / "old")
    original = _write_state_fixture(old_private, old_run, old_workspace)
    snapshot = snapshot_private_run_directory(
        old_run,
        private_root=old_private,
        workspace_root=old_workspace,
    )
    objects = {
        f"checkpoints/4/objects/{item.digest.removeprefix('sha256:')}": (item.content)
        for item in snapshot.objects
    }
    new_private = tmp_path / "new" / "private"
    new_private.mkdir(mode=0o700, parents=True)
    new_workspace = tmp_path / "new" / "workspace"
    new_workspace.mkdir(mode=0o700)
    new_run = new_private / "state" / "replacement-attempt"

    restored = restore_private_run_directory(
        descriptor=snapshot.descriptor,
        objects=objects,
        destination_run_dir=new_run,
        private_root=new_private,
        workspace_root=new_workspace,
    )

    assert restored == new_run.resolve()
    assert sorted(
        path.relative_to(new_run).as_posix() for path in new_run.rglob("*")
    ) == ["empty", "nested", "nested/spec.md", "state.json"]
    assert stat.S_IMODE((new_run / "state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((new_run / "nested/spec.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((new_run / "empty").stat().st_mode) == 0o755
    restored_state = (new_run / "state.json").read_bytes()
    restored_spec = (new_run / "nested/spec.md").read_bytes()
    assert str(new_run).encode() in restored_state
    assert str(new_workspace).encode() in restored_state
    assert str(new_private / "state").encode() in restored_state
    assert str(new_run).encode() in restored_spec
    assert str(new_workspace).encode() in restored_spec
    assert str(new_private).encode() in restored_spec
    assert str(old_private).encode() not in restored_state + restored_spec
    assert str(old_workspace).encode() not in restored_state + restored_spec
    assert original["state.json"] != restored_state


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "mode", "hardlink"])
def test_snapshot_rejects_unsafe_filesystem_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    target = run_dir / "unsafe"
    if unsafe_kind == "symlink":
        target.symlink_to(outside)
    elif unsafe_kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"unsafe")
        if unsafe_kind == "mode":
            target.chmod(0o666)
        else:
            os.link(target, run_dir / "second-link")

    with pytest.raises(PrivateStateError, match="link|regular|mode|unsafe"):
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
        )


def test_snapshot_rejects_secret_or_size_overrun_without_echoing_content(
    tmp_path: Path,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    canary = b"known-private-state-credential"
    sensitive = run_dir / "state.json"
    sensitive.write_bytes(b'{"value":"' + canary + b'"}')
    sensitive.chmod(0o600)
    scanner = SecretScanner(SecretPolicy((("private-state-token", canary),)))

    with pytest.raises(FactoryPolicyViolation) as caught:
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
            secret_scanner=scanner,
        )
    assert canary.decode() not in str(caught.value)

    sensitive.write_bytes(b"123456789")
    with pytest.raises(PrivateStateError, match="size|limit"):
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
            limits=PrivateStateLimits(
                max_files=10,
                max_file_bytes=8,
                max_total_bytes=8,
            ),
        )


def test_snapshot_rejects_unsafe_private_root_mode(
    tmp_path: Path,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    private_root.chmod(0o777)

    with pytest.raises(PrivateStateError, match="mode|unsafe"):
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
        )


def test_snapshot_bounds_directory_only_state(
    tmp_path: Path,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    for name in ("one", "two", "three"):
        (run_dir / name).mkdir(mode=0o700)

    with pytest.raises(PrivateStateError, match="count|limit"):
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
            limits=PrivateStateLimits(
                max_files=2,
                max_file_bytes=8,
                max_total_bytes=8,
            ),
        )


@pytest.mark.parametrize("damage", ["traversal", "digest", "unresolved-token"])
def test_restore_rejects_untrusted_state_before_mutating_destination(
    tmp_path: Path,
    damage: str,
) -> None:
    old_private, old_run, old_workspace = _roots(tmp_path / "old")
    _write_state_fixture(old_private, old_run, old_workspace)
    snapshot = snapshot_private_run_directory(
        old_run,
        private_root=old_private,
        workspace_root=old_workspace,
    )
    descriptor = json.loads(canonical_json_bytes(snapshot.descriptor))
    objects = {
        f"objects/{item.digest.removeprefix('sha256:')}": item.content
        for item in snapshot.objects
    }
    if damage == "traversal":
        descriptor["files"][0]["path"] = "../escape"
    elif damage == "digest":
        first = next(iter(objects))
        objects[first] = b"x" * len(objects[first])
    else:
        entry = descriptor["files"][0]
        first = next(
            path
            for path, content in objects.items()
            if sha256_digest(content) == entry["object_digest"]
        )
        suffix = b"@{FACTORY_UNKNOWN_ROOT}@"
        objects[first] += suffix
        entry["object_digest"] = sha256_digest(objects[first])
        entry["byte_size"] = len(objects[first])
        descriptor["total_bytes"] += len(suffix)
    new_private = tmp_path / "new" / "private"
    new_private.mkdir(mode=0o700, parents=True)
    new_workspace = tmp_path / "new" / "workspace"
    new_workspace.mkdir(mode=0o700)
    destination = new_private / "state" / "replacement"

    with pytest.raises(PrivateStateError, match="path|digest|token"):
        restore_private_run_directory(
            descriptor=descriptor,
            objects=objects,
            destination_run_dir=destination,
            private_root=new_private,
            workspace_root=new_workspace,
        )

    assert not destination.exists()
    assert not (tmp_path / "new" / "escape").exists()
    assert not tuple(new_private.glob(".*.tmp"))


def test_snapshot_rejects_preexisting_portability_tokens(
    tmp_path: Path,
) -> None:
    private_root, run_dir, workspace = _roots(tmp_path)
    path = run_dir / "state.json"
    path.write_bytes(b'{"collision":"' + PRIVATE_RUN_TOKEN + b'"}')
    path.chmod(0o600)

    with pytest.raises(PrivateStateError, match="token"):
        snapshot_private_run_directory(
            run_dir,
            private_root=private_root,
            workspace_root=workspace,
        )


def test_checkpoint_state_object_rejects_nonbytes() -> None:
    with pytest.raises(TypeError, match="bytes"):
        CheckpointStateObject(content="not-bytes")  # type: ignore[arg-type]


def test_restore_rejects_existing_destination(
    tmp_path: Path,
) -> None:
    old_private, old_run, old_workspace = _roots(tmp_path / "old")
    _write_state_fixture(old_private, old_run, old_workspace)
    snapshot = snapshot_private_run_directory(
        old_run,
        private_root=old_private,
        workspace_root=old_workspace,
    )
    objects = {
        f"objects/{item.digest.removeprefix('sha256:')}": item.content
        for item in snapshot.objects
    }
    new_private = tmp_path / "new" / "private"
    new_private.mkdir(mode=0o700, parents=True)
    new_workspace = tmp_path / "new" / "workspace"
    new_workspace.mkdir(mode=0o700)
    destination = new_private / "state" / "replacement"
    destination.mkdir(mode=0o700, parents=True)
    before = deepcopy(sorted(path.as_posix() for path in new_private.rglob("*")))

    with pytest.raises(PrivateStateError, match="exists"):
        restore_private_run_directory(
            descriptor=snapshot.descriptor,
            objects=objects,
            destination_run_dir=destination,
            private_root=new_private,
            workspace_root=new_workspace,
        )

    assert sorted(path.as_posix() for path in new_private.rglob("*")) == before

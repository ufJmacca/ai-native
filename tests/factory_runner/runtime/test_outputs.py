from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.outputs import (
    OutputLimitError,
    OutputWriter,
    validate_output_root,
)
from ai_native.factory_runner.redaction import (
    SecretDetectedError,
    SecretPolicy,
    SecretScanner,
)


def test_output_root_must_be_empty(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        validate_output_root(output)

    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"


def test_output_root_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    output = linked_parent / "output"

    with pytest.raises(ValueError, match="symbolic link"):
        validate_output_root(output)

    assert not (outside / "output").exists()


def test_output_root_creation_fsyncs_its_containing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / "output"
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    fsynced_directories: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            fsynced_directories.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    assert validate_output_root(output) == output
    assert parent_identity in fsynced_directories


def test_writer_does_not_create_through_a_symlinked_child(tmp_path: Path) -> None:
    output = validate_output_root(tmp_path / "output")
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "escape").symlink_to(outside, target_is_directory=True)
    writer = OutputWriter(output)

    with pytest.raises(ValueError, match="symbolic link"):
        writer.write_bytes("escape/created/artifact.txt", b"must stay contained")

    assert not (outside / "created").exists()


def test_writer_fsyncs_each_parent_that_gains_an_artifact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    first = output / "first"
    containing_identities = {
        (output.stat().st_dev, output.stat().st_ino),
    }
    fsynced_directories: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            fsynced_directories.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    writer.write_bytes("first/second/artifact.bin", b"durable")
    containing_identities.add((first.stat().st_dev, first.stat().st_ino))

    assert containing_identities.issubset(fsynced_directories)


def test_writer_is_poisoned_when_artifact_parent_creation_is_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    output_identity = (output.stat().st_dev, output.stat().st_ino)
    real_fsync = os.fsync

    def fail_new_directory_parent_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == output_identity and (
            output / "uncertain"
        ).exists():
            raise OSError("injected artifact-parent fsync failure")
        real_fsync(descriptor)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_new_directory_parent_fsync)
        with pytest.raises(OSError, match="artifact-parent"):
            writer.write_bytes("uncertain/artifact.bin", b"content")

    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("later.bin", b"x")
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()


def _external_reference(path: str, content: bytes) -> ArtifactReference:
    return ArtifactReference(
        path=path,
        media_type="application/json",
        byte_size=len(content),
        digest=sha256_digest(content),
    )


def test_writer_registers_verified_external_checkpoint_artifacts(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    content = b'{"checkpoint":"complete"}'
    checkpoint = output / "checkpoints/1/checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(content)
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )
    writer = OutputWriter(output)

    writer.register_existing_artifact(reference)
    events = writer.write_events_placeholder()
    manifest_reference = writer.write_protocol_manifest(event_stream=events)

    manifest = json.loads((output / manifest_reference.path).read_bytes())
    assert [item["path"] for item in manifest["artifacts"]] == [
        "checkpoints/1/checkpoint.json",
        "events.ndjson",
    ]


@pytest.mark.parametrize("mutation", ["digest", "size", "symlink"])
def test_writer_rejects_unverified_external_artifacts_without_recording(
    tmp_path: Path,
    mutation: str,
) -> None:
    output = validate_output_root(tmp_path / "output")
    content = b'{"checkpoint":"complete"}'
    checkpoint = output / "checkpoints/1/checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(content)
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )
    if mutation == "digest":
        reference = reference.model_copy(update={"digest": sha256_digest(b"different")})
    elif mutation == "size":
        reference = reference.model_copy(update={"byte_size": len(content) + 1})
    else:
        outside = tmp_path / "outside.json"
        outside.write_bytes(content)
        checkpoint.unlink()
        checkpoint.symlink_to(outside)
    writer = OutputWriter(output)

    with pytest.raises(ValueError, match="artifact|digest|size|symbolic"):
        writer.register_existing_artifact(reference)

    assert writer.manifest_digest == sha256_digest(b"[]")


def test_writer_applies_secret_and_total_limits_to_external_artifacts(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    canary = b"factory-secret-canary"
    checkpoint = output / "checkpoints/1/checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(canary)
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        canary,
    )
    scanner = SecretScanner(SecretPolicy((("checkpoint-canary", canary),)))

    with pytest.raises(SecretDetectedError):
        OutputWriter(
            output,
            secret_scanner=scanner,
        ).register_existing_artifact(reference)

    clean_output = validate_output_root(tmp_path / "clean-output")
    clean_checkpoint = clean_output / "checkpoints/1/checkpoint.json"
    clean_checkpoint.parent.mkdir(parents=True)
    clean_checkpoint.write_bytes(b"1234")
    clean_reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        b"1234",
    )
    with pytest.raises(ValueError, match="total"):
        OutputWriter(
            clean_output,
            max_total_bytes=3,
        ).register_existing_artifact(clean_reference)


def test_writer_reserves_capacity_for_terminal_finalization(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(
        output,
        max_artifact_bytes=10,
        max_total_bytes=10,
        finalization_reserve_bytes=4,
    )
    writer.write_bytes("producer.bin", b"123456")

    with pytest.raises(OutputLimitError, match="total"):
        writer.write_bytes("overflow.bin", b"x")

    writer.begin_finalization()
    writer.write_bytes("terminal.bin", b"7890", record=False)
    assert sum(path.stat().st_size for path in output.iterdir()) == 10


def test_staged_artifact_reserves_its_terminal_append_capacity(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output, max_artifact_bytes=10, max_total_bytes=20)
    staged = writer.begin_staged_artifact(
        "events.ndjson",
        finalization_reserve_bytes=4,
    )
    staged.append(b"123456")

    with pytest.raises(OutputLimitError, match="artifact"):
        staged.append(b"x")

    writer.begin_finalization()
    staged.append(b"7890")
    reference = staged.finalize()
    assert reference.byte_size == 10


def test_writer_reserves_one_protocol_manifest_entry_for_events(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(
        output,
        max_artifact_bytes=1,
        max_total_bytes=1,
        finalization_reserve_bytes=1,
    )
    references = tuple(
        _external_reference(f"bundle/object-{index:05d}", b"")
        for index in range(20_000)
    )

    assert writer.preflight_external_artifacts(references[:-1]) == references[:-1]
    with pytest.raises(OutputLimitError, match="manifest"):
        writer.preflight_external_artifacts(references)


def test_staged_bytes_count_against_the_total_output_budget(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output, max_total_bytes=6)
    staged = writer.begin_staged_artifact("events.ndjson")
    staged.append(b"1234")

    with pytest.raises(OutputLimitError, match="total"):
        writer.write_bytes("too-large.bin", b"abc")

    writer.write_bytes("fits.bin", b"ab")
    staged.abort()
    writer.write_bytes("released.bin", b"cdef")


def test_staged_creation_cleanup_uncertainty_poisoned_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    real_fsync = os.fsync
    real_unlink = os.unlink

    def fail_parent_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode) and any(output.iterdir()):
            raise OSError("injected staged parent fsync failure")
        real_fsync(descriptor)

    def fail_staging_cleanup(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path.startswith(".events.ndjson.") and path.endswith(".staging"):
            raise OSError("injected staged cleanup failure")
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_parent_fsync)
        context.setattr(os, "unlink", fail_staging_cleanup)
        with pytest.raises(OSError, match="parent fsync"):
            writer.begin_staged_artifact("events.ndjson")

    hidden = tuple(output.iterdir())
    assert len(hidden) == 1
    assert hidden[0].name.startswith(".events.ndjson.")
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")


def test_partial_staged_append_poisoned_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    staged = writer.begin_staged_artifact("events.ndjson")
    real_fsync = os.fsync

    def fail_appended_file_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size:
            raise OSError("injected staged append fsync failure")
        real_fsync(descriptor)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_appended_file_fsync)
        with pytest.raises(OSError, match="append fsync"):
            staged.append(b"partial")

    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")
    staged.abort()


def test_failed_staged_abort_poisoned_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    staged = writer.begin_staged_artifact("events.ndjson")
    staged.append(b"content")
    real_unlink = os.unlink

    def fail_staging_cleanup(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path.startswith(".events.ndjson.") and path.endswith(".staging"):
            raise OSError("injected staged abort failure")
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(os, "unlink", fail_staging_cleanup)
        with pytest.raises(OSError, match="abort"):
            staged.abort()

    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")
    staged.abort()


def test_ambiguous_staged_publish_poisoned_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    staged = writer.begin_staged_artifact("events.ndjson")
    staged.append(b"content")
    real_replace = os.replace

    def publish_then_fail(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        raise OSError("injected ambiguous staged publish failure")

    with monkeypatch.context() as context:
        context.setattr(os, "replace", publish_then_fail)
        with pytest.raises(OSError, match="ambiguous"):
            staged.finalize()

    assert (output / "events.ndjson").read_bytes() == b"content"
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")
    assert not (output / "completion.json").exists()


def test_external_bundle_is_preflighted_as_one_total(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output, max_total_bytes=5)
    references = (
        _external_reference("checkpoints/1/object-a", b"123"),
        _external_reference("checkpoints/1/object-b", b"456"),
    )

    with pytest.raises(OutputLimitError, match="total"):
        writer.preflight_external_artifacts(references)

    assert tuple(output.iterdir()) == ()


@pytest.mark.parametrize(
    ("total", "reserve"),
    ((None, 1), (3, 4), (3, -1)),
)
def test_finalization_reserve_must_fit_the_total_limit(
    tmp_path: Path,
    total: int | None,
    reserve: int,
) -> None:
    output = validate_output_root(tmp_path / f"output-{total}-{reserve}")

    with pytest.raises(ValueError, match="reserve"):
        OutputWriter(
            output,
            max_total_bytes=total,
            finalization_reserve_bytes=reserve,
        )


def _bundle_manifest_digest(
    references: tuple[ArtifactReference, ...],
) -> str:
    return sha256_digest(
        canonical_json_bytes(
            [
                reference.model_dump(mode="json")
                for reference in sorted(references, key=lambda item: item.path)
            ]
        )
    )


def test_writer_publishes_one_external_bundle_and_accounts_it_atomically(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    contents = {
        "checkpoints/1/checkpoint.json": b"{}",
        "checkpoints/1/objects/state": b"abc",
    }
    references = tuple(
        _external_reference(path, content) for path, content in contents.items()
    )
    writer = OutputWriter(output, max_total_bytes=6)

    published = writer.publish_external_bundle(references, contents)

    assert published == references
    assert writer.manifest_digest == _bundle_manifest_digest(references)
    assert {path: (output / path).read_bytes() for path in sorted(contents)} == contents
    writer.write_bytes("remaining.bin", b"x")
    with pytest.raises(OutputLimitError, match="total"):
        writer.write_bytes("overflow.bin", b"x")


@pytest.mark.parametrize(
    "damage",
    ("missing", "extra", "size", "digest", "secret", "total"),
)
def test_external_bundle_validation_is_all_or_nothing(
    tmp_path: Path,
    damage: str,
) -> None:
    output = validate_output_root(tmp_path / f"output-{damage}")
    canary = b"bundle-attempt-secret"
    first = b"abc"
    second = canary if damage == "secret" else b"de"
    references = (
        _external_reference("checkpoints/1/objects/first", first),
        _external_reference("checkpoints/1/checkpoint.json", second),
    )
    contents = {
        references[0].path: first,
        references[1].path: second,
    }
    if damage == "missing":
        contents.pop(references[1].path)
    elif damage == "extra":
        contents["checkpoints/1/unexpected"] = b"x"
    elif damage == "size":
        references = (
            references[0],
            references[1].model_copy(update={"byte_size": references[1].byte_size + 1}),
        )
    elif damage == "digest":
        references = (
            references[0],
            references[1].model_copy(update={"digest": sha256_digest(b"different")}),
        )
    scanner = SecretScanner(SecretPolicy((("bundle-canary", canary),)))
    writer = OutputWriter(
        output,
        secret_scanner=scanner,
        max_total_bytes=4 if damage == "total" else 64,
    )

    with pytest.raises((OutputLimitError, SecretDetectedError, ValueError)):
        writer.publish_external_bundle(references, contents)

    assert tuple(output.iterdir()) == ()
    assert writer.manifest_digest == sha256_digest(b"[]")


def test_external_bundle_cleans_hidden_staging_on_prepublish_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    content = b"12345"
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )
    writer = OutputWriter(output, max_total_bytes=len(content))

    def fail_before_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr(os, "rename", fail_before_publish)
    with pytest.raises(OSError, match="injected"):
        writer.publish_external_bundle(
            (reference,),
            {reference.path: content},
        )

    assert not (output / "checkpoints/1").exists()
    assert not any(path.name.startswith(".1.") for path in output.rglob("*"))
    assert writer.manifest_digest == sha256_digest(b"[]")
    writer.write_bytes("full-budget.bin", content)


def test_external_bundle_poisoned_when_prepublish_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    content = b"12345"
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )
    writer = OutputWriter(output, max_total_bytes=len(content) + 1)

    def fail_before_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected rename failure")

    def fail_staging_cleanup(
        _cls: type[OutputWriter],
        _parent_fd: int,
        _target_name: str,
    ) -> None:
        raise OSError("injected staging cleanup failure")

    monkeypatch.setattr(os, "rename", fail_before_publish)
    monkeypatch.setattr(
        OutputWriter,
        "_remove_bundle_directory",
        classmethod(fail_staging_cleanup),
    )
    with pytest.raises(OSError, match="rename"):
        writer.publish_external_bundle(
            (reference,),
            {reference.path: content},
        )

    hidden = tuple((output / "checkpoints").iterdir())
    assert len(hidden) == 1
    assert hidden[0].name.startswith(".1.")
    assert (hidden[0] / "checkpoint.json").read_bytes() == content
    assert writer.manifest_digest == sha256_digest(b"[]")
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")
    assert not (output / "completion.json").exists()


def test_atomic_write_poisoned_when_prepublish_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output, max_total_bytes=32)
    real_unlink = os.unlink

    def fail_before_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected replace failure")

    def fail_temporary_cleanup(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path.startswith(".artifact.bin.") and path.endswith(".tmp"):
            raise OSError("injected temporary cleanup failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "replace", fail_before_publish)
    monkeypatch.setattr(os, "unlink", fail_temporary_cleanup)
    with pytest.raises(OSError, match="replace"):
        writer.write_bytes("artifact.bin", b"content")

    hidden = tuple(output.iterdir())
    assert len(hidden) == 1
    assert hidden[0].name.startswith(".artifact.bin.")
    assert hidden[0].read_bytes() == b"content"
    assert writer.manifest_digest == sha256_digest(b"[]")
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")
    assert not (output / "completion.json").exists()


def test_external_bundle_remains_fully_accounted_after_postrename_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    content = b"12345"
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )
    writer = OutputWriter(output, max_total_bytes=len(content) + 1)
    real_fsync = os.fsync

    def fail_after_publish(descriptor: int) -> None:
        if (output / "checkpoints/1").exists():
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_after_publish)
        with pytest.raises(OSError, match="injected"):
            writer.publish_external_bundle(
                (reference,),
                {reference.path: content},
            )

    assert (output / reference.path).read_bytes() == content
    assert writer.manifest_digest == _bundle_manifest_digest((reference,))
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.begin_finalization()
    with pytest.raises(RuntimeError, match="poisoned"):
        writer.write_bytes("remaining.bin", b"x")


def test_staged_artifact_rejects_a_hard_link_alias_before_writing(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    writer = OutputWriter(output)
    staged = writer.begin_staged_artifact("events.ndjson")
    hidden = next(output.iterdir())
    alias = tmp_path / "staging-alias"
    os.link(hidden, alias)

    with pytest.raises(ValueError, match="hard link"):
        staged.append(b"must not reach the alias")

    assert alias.read_bytes() == b""
    staged.abort()


@pytest.mark.parametrize("target_kind", ("symlink", "fifo"))
def test_external_bundle_rejects_link_or_special_common_target(
    tmp_path: Path,
    target_kind: str,
) -> None:
    output = validate_output_root(tmp_path / f"output-{target_kind}")
    writer = OutputWriter(output)
    bundle_parent = output / "checkpoints"
    bundle_parent.mkdir()
    target = bundle_parent / "1"
    outside = tmp_path / f"outside-{target_kind}"
    if target_kind == "symlink":
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(target)
    content = b"checkpoint"
    reference = _external_reference(
        "checkpoints/1/checkpoint.json",
        content,
    )

    with pytest.raises(ValueError, match="symbolic|exists"):
        writer.publish_external_bundle(
            (reference,),
            {reference.path: content},
        )

    assert not outside.exists() or tuple(outside.iterdir()) == ()
    assert writer.manifest_digest == sha256_digest(b"[]")


def test_external_bundle_requires_one_non_root_common_directory(
    tmp_path: Path,
) -> None:
    output = validate_output_root(tmp_path / "output")
    contents = {
        "first/artifact": b"a",
        "second/artifact": b"b",
    }
    references = tuple(
        _external_reference(path, content) for path, content in contents.items()
    )
    writer = OutputWriter(output)

    with pytest.raises(ValueError, match="common directory"):
        writer.publish_external_bundle(references, contents)

    assert tuple(output.iterdir()) == ()

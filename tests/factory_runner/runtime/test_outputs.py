from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root
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


def test_writer_does_not_create_through_a_symlinked_child(tmp_path: Path) -> None:
    output = validate_output_root(tmp_path / "output")
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "escape").symlink_to(outside, target_is_directory=True)
    writer = OutputWriter(output)

    with pytest.raises(ValueError, match="symbolic link"):
        writer.write_bytes("escape/created/artifact.txt", b"must stay contained")

    assert not (outside / "created").exists()


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
        reference = reference.model_copy(
            update={"digest": sha256_digest(b"different")}
        )
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

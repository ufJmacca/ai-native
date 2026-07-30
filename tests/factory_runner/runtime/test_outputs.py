from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.factory_runner.outputs import OutputWriter, validate_output_root


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

from __future__ import annotations

import re
from pathlib import PurePath

from ai_native.stages.slicing import _slice_artifact_filename


def test_slice_artifact_filename_is_one_portable_path_component() -> None:
    filename = _slice_artifact_filename("S001", "Add GET /health")

    assert filename == "S001-add-get-health.md"
    assert PurePath(filename).name == filename
    assert "/" not in filename
    assert "\\" not in filename


def test_slice_artifact_filename_contains_untrusted_id_and_name() -> None:
    filename = _slice_artifact_filename("../S001", "../GET /health\\status")

    assert filename == "s001-get-health-status.md"
    assert PurePath(filename).name == filename


def test_slice_artifact_filename_bounds_long_names_with_stable_digest() -> None:
    long_name = "A" * 400

    filename = _slice_artifact_filename("S001", long_name)

    assert filename == _slice_artifact_filename("S001", long_name)
    assert filename != _slice_artifact_filename("S001", f"{long_name}B")
    assert filename.isascii()
    assert len(filename.encode("ascii")) == 240
    assert re.fullmatch(r"S001-a+-[0-9a-f]{16}\.md", filename)
    assert PurePath(filename).name == filename

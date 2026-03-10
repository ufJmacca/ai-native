from __future__ import annotations

import shutil
from pathlib import Path

from ai_native.models import RunState
from ai_native.utils import ensure_dir

WORKSPACE_ARTIFACT_FILES = ("red.log", "green.log", "refactor-notes.md")


def workspace_run_dir(state: RunState) -> Path:
    return ensure_dir(Path(state.run_dir))


def workspace_slice_dir(state: RunState, slice_id: str) -> Path:
    return ensure_dir(workspace_run_dir(state) / "slices" / slice_id)


def mirror_files(source_dir: Path, target_dir: Path, filenames: tuple[str, ...] = WORKSPACE_ARTIFACT_FILES) -> list[Path]:
    copied: list[Path] = []
    ensure_dir(target_dir)
    for name in filenames:
        source = source_dir / name
        target = target_dir / name
        if not source.exists():
            continue
        if source.resolve() == target.resolve():
            continue
        shutil.copyfile(source, target)
        copied.append(target)
    return copied

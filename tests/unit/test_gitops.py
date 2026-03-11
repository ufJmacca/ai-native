from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_native.gitops import ensure_repo


def test_ensure_repo_initializes_standalone_directory(tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()

    ensure_repo(workspace_root, "main")

    assert (workspace_root / ".git").exists()


def test_ensure_repo_allows_existing_repo_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=workspace_root, check=True, capture_output=True, text=True)

    ensure_repo(workspace_root, "main")

    assert (workspace_root / ".git").exists()


def test_ensure_repo_rejects_nested_directory_inside_existing_repo(tmp_path: Path) -> None:
    parent_repo = tmp_path / "parent-repo"
    nested_workspace = parent_repo / "app"
    nested_workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=parent_repo, check=True, capture_output=True, text=True)

    with pytest.raises(RuntimeError, match="nested inside existing git repository"):
        ensure_repo(nested_workspace, "main")

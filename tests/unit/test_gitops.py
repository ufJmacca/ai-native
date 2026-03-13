from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_native import gitops
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


def test_git_commands_mark_explicit_directory_safe(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    gitops._run_optional(["git", "status", "--short"], tmp_path)

    assert recorded == [["git", "-c", "safe.directory=*", "status", "--short"]]


def test_create_worktree_disables_auto_tracking_config(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []
    worktree_path = tmp_path / "worktrees" / "S001"

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)
    monkeypatch.setattr(gitops, "_ensure_local_ignore", lambda cwd: None)

    gitops.create_worktree(tmp_path, "origin/main", "codex/example-S001", worktree_path)

    assert recorded == [
        [
            "git",
            "-c",
            "safe.directory=*",
            "-c",
            "branch.autoSetupMerge=false",
            "worktree",
            "add",
            "-b",
            "codex/example-S001",
            str(worktree_path),
            "origin/main",
        ]
    ]


def test_push_branch_avoids_setting_upstream_tracking(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    gitops.push_branch(tmp_path, "codex/example-S001")

    assert recorded == [["git", "-c", "safe.directory=*", "push", "origin", "codex/example-S001"]]

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_native import gitops
from ai_native.gitops import discover_repo_root, ensure_repo


def _init_repo(cwd: Path) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    (cwd / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


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


def test_discover_repo_root_returns_none_outside_repo(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    assert discover_repo_root(workspace_root) is None


def test_discover_repo_root_returns_top_level_for_nested_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    nested_workspace = repo_root / "apps" / "web"
    nested_workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True, text=True)

    assert discover_repo_root(nested_workspace) == repo_root.resolve()


def test_discover_repo_root_returns_none_when_git_is_missing(monkeypatch, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    assert discover_repo_root(workspace_root) is None


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


def test_status_porcelain_reports_tracked_and_untracked_changes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    (tmp_path / "README.md").write_text("# Changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    status = gitops.status_porcelain(tmp_path)

    assert " M README.md" in status
    assert "?? new.txt" in status


def test_head_diff_is_clean_checks_head_tracked_diff_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    assert gitops.head_diff_is_clean(tmp_path) is True

    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    assert gitops.head_diff_is_clean(tmp_path) is True

    (tmp_path / "README.md").write_text("# Changed\n", encoding="utf-8")
    assert gitops.head_diff_is_clean(tmp_path) is False


def test_worktree_is_clean_requires_no_tracked_or_untracked_changes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    assert gitops.worktree_is_clean(tmp_path) is True

    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    assert gitops.worktree_is_clean(tmp_path) is False


def test_amend_all_stages_tracked_and_untracked_repairs_cleanly(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    sha = gitops.amend_all(tmp_path)

    assert sha
    assert gitops.worktree_is_clean(tmp_path) is True
    committed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "README.md" in committed_files
    assert "new.txt" in committed_files


def test_amend_all_stages_amends_and_returns_new_head(
    monkeypatch, tmp_path: Path
) -> None:
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        stdout = "newsha\n" if command[-2:] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    sha = gitops.amend_all(tmp_path)

    assert sha == "newsha"
    assert recorded == [
        ["git", "-c", "safe.directory=*", "add", "-A"],
        ["git", "-c", "safe.directory=*", "commit", "--amend", "--no-edit"],
        ["git", "-c", "safe.directory=*", "rev-parse", "HEAD"],
    ]


def test_merge_commit_aborts_and_reports_conflicted_files(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        if "merge" in command and "--abort" not in command:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="Auto-merging src/app.py\nCONFLICT (content): Merge conflict in src/app.py\n",
                stderr="Automatic merge failed; fix conflicts and then commit the result.\n",
            )
        if command[-3:] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/app.py\nsrc/lib.py\n", stderr="")
        if command[-2:] == ["merge", "--abort"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    with pytest.raises(gitops.MergeConflictError) as excinfo:
        gitops.merge_commit(tmp_path, "deadbeefcafebabe")

    assert excinfo.value.commit_sha == "deadbeefcafebabe"
    assert excinfo.value.conflicted_files == ["src/app.py", "src/lib.py"]
    assert excinfo.value.merge_aborted is True
    assert "worktree merge was aborted" in str(excinfo.value)
    assert recorded == [
        [
            "git",
            "-c",
            "safe.directory=*",
            "-c",
            "user.name=ai-native",
            "-c",
            "user.email=ai-native@example.invalid",
            "merge",
            "--no-edit",
            "deadbeefcafebabe",
        ],
        ["git", "-c", "safe.directory=*", "diff", "--name-only", "--diff-filter=U"],
        ["git", "-c", "safe.directory=*", "merge", "--abort"],
    ]


def test_merge_commit_for_repair_leaves_conflicted_merge(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        if "merge" in command:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="Auto-merging src/app.py\nCONFLICT (content): Merge conflict in src/app.py\n",
                stderr="Automatic merge failed; fix conflicts and then commit the result.\n",
            )
        if command[-3:] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/app.py\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    with pytest.raises(gitops.MergeConflictError) as excinfo:
        gitops.merge_commit_for_repair(tmp_path, "deadbeefcafebabe")

    assert excinfo.value.conflicted_files == ["src/app.py"]
    assert excinfo.value.merge_aborted is False
    assert "left in the worktree for repair" in str(excinfo.value)
    assert ["git", "-c", "safe.directory=*", "merge", "--abort"] not in recorded


def test_continue_merge_commits_merge_and_returns_head(monkeypatch, tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    recorded: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(command))
        if command[-2:] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(command, 0, stdout=".git\n", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="merge-sha\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)

    assert gitops.continue_merge(tmp_path) == "merge-sha"
    assert recorded == [
        ["git", "-c", "safe.directory=*", "rev-parse", "--git-dir"],
        ["git", "-c", "safe.directory=*", "add", "-A"],
        [
            "git",
            "-c",
            "safe.directory=*",
            "-c",
            "user.name=ai-native",
            "-c",
            "user.email=ai-native@example.invalid",
            "commit",
            "--no-edit",
        ],
        ["git", "-c", "safe.directory=*", "rev-parse", "HEAD"],
    ]

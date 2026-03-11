from __future__ import annotations

import subprocess
from pathlib import Path


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "command failed")
    return completed.stdout.strip()


def _run_optional(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _git_dir(cwd: Path) -> Path:
    git_dir = _run(["git", "rev-parse", "--git-dir"], cwd)
    path = Path(git_dir)
    if path.is_absolute():
        return path
    return (cwd / path).resolve()


def _ensure_local_ignore(cwd: Path) -> None:
    info_dir = _git_dir(cwd) / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if ".ai-native/" in existing.splitlines():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(f"{existing}{prefix}.ai-native/\n", encoding="utf-8")


def ensure_repo(cwd: Path, default_branch: str = "main") -> None:
    cwd = cwd.resolve()
    probe = _run_optional(["git", "rev-parse", "--show-toplevel"], cwd)
    if probe.returncode == 0:
        top_level = Path(probe.stdout.strip()).resolve()
        if top_level != cwd:
            raise RuntimeError(
                f"Workspace root {cwd} is nested inside existing git repository {top_level}. "
                "Use a standalone directory or the repository root as TARGET_DIR."
            )
        _ensure_local_ignore(cwd)
        return

    init = _run_optional(["git", "init", "-b", default_branch], cwd)
    if init.returncode == 0:
        _ensure_local_ignore(cwd)
        return

    _run(["git", "init"], cwd)
    current_branch = _run_optional(["git", "symbolic-ref", "--short", "HEAD"], cwd)
    if current_branch.returncode != 0 or current_branch.stdout.strip() != default_branch:
        _run(["git", "checkout", "-b", default_branch], cwd)
    _ensure_local_ignore(cwd)


def ensure_branch(cwd: Path, branch_name: str) -> str:
    branches = _run(["git", "branch", "--list", branch_name], cwd)
    if branches.strip():
        _run(["git", "checkout", branch_name], cwd)
    else:
        _run(["git", "checkout", "-b", branch_name], cwd)
    return branch_name


def ensure_base_commit(cwd: Path, base_branch: str) -> None:
    if _run_optional(["git", "rev-parse", "--verify", "HEAD"], cwd).returncode == 0:
        return
    current_branch = _run_optional(["git", "symbolic-ref", "--short", "HEAD"], cwd)
    if current_branch.returncode != 0 or current_branch.stdout.strip() != base_branch:
        _run_optional(["git", "checkout", "-B", base_branch], cwd)
    _run(
        [
            "git",
            "-c",
            "user.name=ai-native",
            "-c",
            "user.email=ai-native@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "chore: initialize repository for ai-native workflow",
        ],
        cwd,
    )


def has_changes(cwd: Path) -> bool:
    return bool(_run(["git", "status", "--porcelain"], cwd).strip())


def commit_all(cwd: Path, subject: str, body: str | None = None) -> str:
    _run(["git", "add", "-A"], cwd)
    command = ["git", "commit", "-m", subject]
    if body:
        command.extend(["-m", body])
    _run(command, cwd)
    return _run(["git", "rev-parse", "HEAD"], cwd)


def push_branch(cwd: Path, branch_name: str) -> None:
    _run(["git", "push", "-u", "origin", branch_name], cwd)


def create_pull_request(cwd: Path, title: str, body_file: Path, draft: bool, base_branch: str | None = None) -> str:
    command = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file)]
    if base_branch:
        command.extend(["--base", base_branch])
    if draft:
        command.append("--draft")
    return _run(command, cwd)


def resolve_base_ref(cwd: Path, base_branch: str) -> str:
    remote = _run_optional(["git", "remote", "get-url", "origin"], cwd)
    if remote.returncode == 0:
        fetch = _run_optional(["git", "fetch", "origin", base_branch], cwd)
        if fetch.returncode == 0:
            return f"origin/{base_branch}"
    remote_ref = f"refs/remotes/origin/{base_branch}"
    if _run_optional(["git", "show-ref", "--verify", "--quiet", remote_ref], cwd).returncode == 0:
        return f"origin/{base_branch}"
    return base_branch


def is_ancestor(cwd: Path, commit_sha: str, base_ref: str) -> bool:
    return _run_optional(["git", "merge-base", "--is-ancestor", commit_sha, base_ref], cwd).returncode == 0


def _parse_worktree_list(cwd: Path) -> dict[Path, str | None]:
    worktrees: dict[Path, str | None] = {}
    current_path: Path | None = None
    for raw_line in _run(["git", "worktree", "list", "--porcelain"], cwd).splitlines():
        if raw_line.startswith("worktree "):
            current_path = Path(raw_line.split(" ", 1)[1]).resolve()
            worktrees[current_path] = None
        elif raw_line.startswith("branch ") and current_path is not None:
            worktrees[current_path] = raw_line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif not raw_line:
            current_path = None
    return worktrees


def create_worktree(cwd: Path, base_ref: str, branch_name: str, worktree_path: Path) -> Path:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_ref], cwd)
    _ensure_local_ignore(worktree_path)
    return worktree_path.resolve()


def ensure_worktree(cwd: Path, branch_name: str, worktree_path: Path, base_ref: str) -> Path:
    target_path = worktree_path.resolve()
    known_worktrees = _parse_worktree_list(cwd)
    for existing_path, existing_branch in known_worktrees.items():
        if existing_branch == branch_name and existing_path.exists():
            _ensure_local_ignore(existing_path)
            return existing_path
    if target_path.exists():
        current_branch = _run_optional(["git", "branch", "--show-current"], target_path)
        if current_branch.returncode == 0 and current_branch.stdout.strip() == branch_name:
            _ensure_local_ignore(target_path)
            return target_path
    branch_exists = bool(_run(["git", "branch", "--list", branch_name], cwd).strip())
    if branch_exists:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "worktree", "add", str(worktree_path), branch_name], cwd)
    else:
        create_worktree(cwd, base_ref, branch_name, worktree_path)
    _ensure_local_ignore(worktree_path)
    return worktree_path.resolve()


def merge_commit(cwd: Path, commit_sha: str) -> None:
    _run(
        [
            "git",
            "-c",
            "user.name=ai-native",
            "-c",
            "user.email=ai-native@example.invalid",
            "merge",
            "--no-edit",
            commit_sha,
        ],
        cwd,
    )


def remove_worktree(worktree_path: Path) -> None:
    _run(["git", "worktree", "remove", str(worktree_path)], worktree_path.parent)


def non_ai_native_changes(cwd: Path) -> list[str]:
    changes = _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd).splitlines()
    relevant: list[str] = []
    for entry in changes:
        path = entry[3:] if len(entry) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.strip()
        if not normalized:
            continue
        if normalized == ".ai-native" or normalized.startswith(".ai-native/"):
            continue
        relevant.append(normalized)
    return relevant

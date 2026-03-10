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


def _ensure_local_ignore(cwd: Path) -> None:
    info_dir = cwd / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if ".ai-native/" in existing.splitlines():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(f"{existing}{prefix}.ai-native/\n", encoding="utf-8")


def ensure_repo(cwd: Path, default_branch: str = "main") -> None:
    probe = _run_optional(["git", "rev-parse", "--show-toplevel"], cwd)
    if probe.returncode == 0:
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


def create_pull_request(cwd: Path, title: str, body_file: Path, draft: bool) -> str:
    command = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file)]
    if draft:
        command.append("--draft")
    return _run(command, cwd)

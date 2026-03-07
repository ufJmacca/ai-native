from __future__ import annotations

import subprocess
from pathlib import Path


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "command failed")
    return completed.stdout.strip()


def ensure_branch(cwd: Path, branch_name: str) -> str:
    branches = _run(["git", "branch", "--list", branch_name], cwd)
    if branches.strip():
        _run(["git", "checkout", branch_name], cwd)
    else:
        _run(["git", "checkout", "-b", branch_name], cwd)
    return branch_name


def commit_all(cwd: Path, message: str) -> str:
    _run(["git", "add", "-A"], cwd)
    _run(["git", "commit", "-m", message], cwd)
    return _run(["git", "rev-parse", "HEAD"], cwd)


def push_branch(cwd: Path, branch_name: str) -> None:
    _run(["git", "push", "-u", "origin", branch_name], cwd)


def create_pull_request(cwd: Path, title: str, body_file: Path, draft: bool) -> str:
    command = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file)]
    if draft:
        command.append("--draft")
    return _run(command, cwd)


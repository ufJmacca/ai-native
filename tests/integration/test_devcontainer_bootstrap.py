from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _run_post_create(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / ".devcontainer" / "scripts" / "post-create.sh"
    home = tmp_path / "home"
    host_config = tmp_path / "host-config"
    host_copilot = tmp_path / "host-copilot"
    host_codex = tmp_path / "host-codex"
    env = os.environ.copy()
    env.update(
        {
            "AINATIVE_DEVCONTAINER_HOME": str(home),
            "AINATIVE_DEVCONTAINER_HOST_CONFIG": str(host_config),
            "AINATIVE_DEVCONTAINER_HOST_COPILOT": str(host_copilot),
            "AINATIVE_DEVCONTAINER_HOST_CODEX": str(host_codex),
        }
    )
    return subprocess.run(
        ["bash", str(script_path), "--verify-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_post_create_verify_only_succeeds_without_codex_or_copilot(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    completed = _run_post_create(tmp_path)

    assert completed.returncode == 0
    assert "[ok]" in completed.stdout
    assert "[optional-missing]" in completed.stdout


def test_post_create_verify_only_fails_when_git_or_ssh_is_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    completed = _run_post_create(tmp_path)

    assert completed.returncode == 1
    assert "Required host credentials were not mounted" in completed.stderr
    assert "~/.ssh and ~/.gitconfig" in completed.stderr

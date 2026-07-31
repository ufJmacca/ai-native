from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from ai_native.factory_runner.process_policy import (
    FactoryPolicyViolation,
    audit_host_environment,
    build_child_environment,
    resolve_trusted_command,
    validate_declared_command,
)
from ai_native.factory_runner.runner import _git_environment


@pytest.mark.parametrize(
    "credential_key",
    [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "AWS_ACCESS_KEY_ID",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AINATIVE_RUN_REGISTRY_AUTH_TOKEN",
        "AINATIVE_TELEMETRY_TOKEN",
        "DOCKER_AUTH_CONFIG",
        "KUBECONFIG",
    ],
)
def test_host_environment_audit_rejects_broad_credentials_without_echoing_values(
    credential_key: str,
) -> None:
    secret_canary = f"secret-canary-for-{credential_key.lower()}"

    with pytest.raises(FactoryPolicyViolation) as exc_info:
        audit_host_environment({credential_key: secret_canary})

    assert credential_key in str(exc_info.value)
    assert secret_canary not in str(exc_info.value)


def test_host_environment_audit_allows_noncredential_runtime_keys() -> None:
    audit_host_environment(
        {
            "PATH": "/factory/bin:/usr/bin",
            "LANG": "C.UTF-8",
            "CI": "true",
        }
    )


def test_declared_command_must_match_an_entire_argument_vector() -> None:
    allowed_commands = (
        ("pytest", "-q"),
        ("ruff", "check", "."),
    )

    validate_declared_command(("pytest", "-q"), allowed_commands)

    for command in (
        ("pytest",),
        ("pytest", "-q", "--last-failed"),
        ("pytest", "tests", "-q"),
        ("python", "-m", "pytest", "-q"),
        ("sh", "-c", "pytest -q"),
    ):
        with pytest.raises(FactoryPolicyViolation):
            validate_declared_command(command, allowed_commands)


@pytest.mark.parametrize(
    "command",
    [
        ("gh", "pr", "create"),
        ("/usr/bin/gh", "api", "repos/example/project"),
        ("git", "push", "origin", "HEAD"),
        ("/usr/bin/git", "commit", "-m", "publish"),
        ("git", "-c", "credential.helper=", "push", "origin", "HEAD"),
        (
            "git",
            "-c",
            "alias.publish=!git push origin HEAD",
            "publish",
        ),
        ("git", "merge", "topic"),
        ("git", "update-ref", "refs/tags/escaped", "HEAD"),
        ("git", "diff", "--ext-diff"),
        ("git", "grep", "--open-files-in-pager=cat", "needle"),
        ("sh", "-c", "pytest -q"),
        ("/bin/bash", "-lc", "git diff --check"),
    ],
)
def test_publication_commands_are_denied_even_when_declared(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(FactoryPolicyViolation):
        validate_declared_command(command, (command,))


def test_exact_declared_read_only_git_command_is_allowed() -> None:
    command = ("git", "diff", "--check")

    validate_declared_command(command, (command,))


def _git_config_overrides(environment: dict[str, str]) -> dict[str, str]:
    count = int(environment["GIT_CONFIG_COUNT"])
    return {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
    }


def test_runner_git_environment_trusts_only_the_exact_admitted_workspace(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    sterile_home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for directory in (workspace, sterile_home, temp_dir):
        directory.mkdir()

    environment = _git_environment(
        {},
        sterile_home=sterile_home,
        temp_dir=temp_dir,
        workspace=workspace,
    )

    overrides = _git_config_overrides(environment)
    assert overrides["safe.directory"] == str(workspace)
    assert overrides["safe.directory"] != "*"


def test_runner_git_environment_rejects_a_workspace_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    sterile_home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    sterile_home.mkdir()
    temp_dir.mkdir()

    with pytest.raises(FactoryPolicyViolation, match="canonical directory"):
        _git_environment(
            {},
            sterile_home=sterile_home,
            temp_dir=temp_dir,
            workspace=alias,
        )


def test_child_environment_is_filtered_and_forces_noninteractive_git(
    tmp_path: Path,
) -> None:
    sterile_home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    sterile_home.mkdir()
    temp_dir.mkdir()
    source_environment = {
        "PATH": "/factory/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "UNDECLARED_VALUE": "must-not-leak",
        "HOME": "/host/home",
        "TMPDIR": "/host/tmp",
        "GIT_TERMINAL_PROMPT": "1",
        "GIT_CONFIG_GLOBAL": "/host/.gitconfig",
        "GIT_ASKPASS": "/host/bin/askpass",
        "SSH_ASKPASS": "/host/bin/ssh-askpass",
        "GCM_INTERACTIVE": "Always",
    }

    environment = build_child_environment(
        allowed_keys=("PATH", "LANG"),
        source_env=source_environment,
        sterile_home=sterile_home,
        temp_dir=temp_dir,
    )

    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert environment["LANG"] == source_environment["LANG"]
    assert "UNDECLARED_VALUE" not in environment
    assert environment["HOME"] == str(sterile_home)
    assert environment["TMPDIR"] == str(temp_dir)
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ALLOW_PROTOCOL"] == ""
    assert environment["GIT_PROTOCOL_FROM_USER"] == "0"
    assert environment["GIT_EXTERNAL_DIFF"] == ""
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_PAGER"] == "cat"
    assert environment["GCM_INTERACTIVE"].casefold() in {"never", "false", "0"}

    overrides = _git_config_overrides(environment)
    assert overrides["credential.helper"] == ""
    assert overrides["core.fsmonitor"] == "false"
    assert "safe.directory" not in overrides
    hooks_path = Path(overrides["core.hooksPath"])
    assert hooks_path.is_absolute()
    assert hooks_path.is_dir()
    assert not any(hooks_path.iterdir())

    for key in ("GIT_ASKPASS", "SSH_ASKPASS"):
        helper = Path(environment[key])
        assert helper.is_absolute()
        completed = subprocess.run(
            [str(helper), "credential prompt"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert completed.returncode != 0


def test_environment_denylist_overrides_the_declared_allowlist(
    tmp_path: Path,
) -> None:
    sterile_home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    sterile_home.mkdir()
    temp_dir.mkdir()

    with pytest.raises(FactoryPolicyViolation):
        build_child_environment(
            allowed_keys=("PATH", "GITHUB_TOKEN"),
            source_env={
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "secret-canary",
            },
            sterile_home=sterile_home,
            temp_dir=temp_dir,
        )


def test_executable_resolution_rejects_mutable_workspace_binaries(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    workspace.mkdir()
    output.mkdir()
    executable = workspace / "pytest"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(FactoryPolicyViolation):
        resolve_trusted_command(
            (str(executable), "-q"),
            environment={"PATH": str(workspace)},
            prohibited_roots=(workspace, output),
        )


def test_executable_resolution_ignores_caller_controlled_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_git = workspace / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    environment = build_child_environment(
        allowed_keys=("PATH",),
        source_env={"PATH": str(workspace)},
        sterile_home=tmp_path / "home",
        temp_dir=tmp_path / "tmp",
    )

    resolved = resolve_trusted_command(
        ("git", "diff", "--check"),
        environment=environment,
        prohibited_roots=(workspace,),
    )

    assert resolved[0] != str(fake_git)
    assert Path(resolved[0]).is_absolute()

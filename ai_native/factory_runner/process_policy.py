from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
import stat


class FactoryPolicyViolation(ValueError):
    """A factory-mode request would exceed the runner's authority."""


_TRUSTED_EXECUTABLE_PATH = "/usr/local/bin:/usr/bin:/bin"

_PROHIBITED_CREDENTIAL_KEYS = frozenset(
    {
        "AINATIVE_RUN_REGISTRY_AUTH_TOKEN",
        "AINATIVE_TELEMETRY_API_KEY",
        "AINATIVE_TELEMETRY_PASSWORD",
        "AINATIVE_TELEMETRY_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_OPENAI_API_KEY",
        "AZURE_CONFIG_DIR",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_TENANT_ID",
        "ARM_CLIENT_CERTIFICATE_PATH",
        "ARM_CLIENT_ID",
        "ARM_CLIENT_SECRET",
        "ARM_OIDC_TOKEN",
        "ARM_OIDC_TOKEN_FILE_PATH",
        "ARM_TENANT_ID",
        "COPILOT_GITHUB_TOKEN",
        "DOCKER_AUTH_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "GCP_ACCESS_TOKEN",
        "GCP_CREDENTIALS",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        "GOOGLE_API_KEY",
        "GOOGLE_CREDENTIALS",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "KUBECONFIG",
        "KUBE_TOKEN",
        "NETRC",
        "NPM_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PYPI_TOKEN",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
        "TWINE_PASSWORD",
    }
)
_PROHIBITED_PREFIX_MARKERS = (
    (
        ("AINATIVE_RUN_REGISTRY_", "AINATIVE_TELEMETRY_"),
        ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN"),
    ),
    (
        ("AWS_", "AZURE_", "ARM_", "GCP_", "GOOGLE_"),
        (
            "ACCESS_KEY",
            "AUTH",
            "CERTIFICATE",
            "CREDENTIAL",
            "OIDC",
            "PASSWORD",
            "PRIVATE_KEY",
            "SECRET",
            "TOKEN",
        ),
    ),
    (
        ("GH_", "GITHUB_"),
        ("APP_KEY", "AUTH", "CREDENTIAL", "PRIVATE_KEY", "SECRET", "TOKEN"),
    ),
    (
        ("DOCKER_", "KUBE"),
        ("AUTH", "CONFIG", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN"),
    ),
)
_RUNNER_CONTROLLED_ENVIRONMENT_KEYS = frozenset(
    {
        "GCM_INTERACTIVE",
        "GIT_ASKPASS",
        "GIT_ALLOW_PROTOCOL",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PAGER",
        "GIT_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_PROXY_COMMAND",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_TERMINAL_PROMPT",
        "GIT_WORK_TREE",
        "HOME",
        "PYTHONNOUSERSITE",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)
_RUNNER_CONTROLLED_ENVIRONMENT_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "check-attr",
        "check-ignore",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "name-rev",
        "rev-parse",
        "shortlog",
        "show",
        "status",
        "version",
    }
)
_UNSAFE_READ_ONLY_GIT_ARGUMENTS = frozenset(
    {
        "--ext-diff",
        "--open-files-in-pager",
        "--textconv",
    }
)
_SHELL_EXECUTABLES = frozenset(
    {
        "ash",
        "bash",
        "cmd",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }
)


def _is_prohibited_credential_key(key: str) -> bool:
    normalized = key.upper()
    if normalized in _PROHIBITED_CREDENTIAL_KEYS:
        return True
    return any(
        normalized.startswith(prefixes)
        and any(marker in normalized for marker in markers)
        for prefixes, markers in _PROHIBITED_PREFIX_MARKERS
    )


def audit_host_environment(env: Mapping[str, str]) -> None:
    """Reject ambient credentials before factory mode performs any work."""

    for key in env:
        if _is_prohibited_credential_key(key):
            raise FactoryPolicyViolation(
                f"prohibited credential environment key: {key}"
            )


def _failing_helper(temp_dir: Path) -> Path:
    installed = shutil.which("false", path=os.defpath)
    if installed is not None:
        return Path(installed).resolve()

    helper = temp_dir / "deny-interactive-auth"
    helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    helper.chmod(0o700)
    return helper.resolve()


def _git_config_environment(overrides: Sequence[tuple[str, str]]) -> dict[str, str]:
    environment = {"GIT_CONFIG_COUNT": str(len(overrides))}
    for index, (key, value) in enumerate(overrides):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _is_runner_controlled_environment_key(key: str) -> bool:
    return key in _RUNNER_CONTROLLED_ENVIRONMENT_KEYS or key.startswith(
        _RUNNER_CONTROLLED_ENVIRONMENT_PREFIXES
    )


def build_child_environment(
    allowed_keys: Sequence[str],
    source_env: Mapping[str, str],
    sterile_home: Path,
    temp_dir: Path,
) -> dict[str, str]:
    """Build a minimal child environment with non-bypassable safety settings."""

    audit_host_environment(source_env)

    resolved_home = sterile_home.resolve()
    resolved_temp = temp_dir.resolve()
    resolved_home.mkdir(parents=True, exist_ok=True)
    resolved_temp.mkdir(parents=True, exist_ok=True)

    hooks_path = resolved_temp / "git-hooks"
    hooks_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(hooks_path.iterdir()):
        raise FactoryPolicyViolation("factory Git hooks directory must be empty")

    failing_helper = _failing_helper(resolved_temp)
    allowed = set(allowed_keys)
    environment = {
        key: value
        for key, value in source_env.items()
        if key in allowed
        and key != "PATH"
        and not _is_runner_controlled_environment_key(key)
    }

    xdg_config = resolved_home / ".config"
    xdg_cache = resolved_home / ".cache"
    xdg_data = resolved_home / ".local" / "share"
    for directory in (xdg_config, xdg_cache, xdg_data):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_ASKPASS": str(failing_helper),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_EXTERNAL_DIFF": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_SSH_COMMAND": str(failing_helper),
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(resolved_home),
            "PATH": _TRUSTED_EXECUTABLE_PATH,
            "PYTHONNOUSERSITE": "1",
            "SSH_ASKPASS": str(failing_helper),
            "SSH_ASKPASS_REQUIRE": "force",
            "TEMP": str(resolved_temp),
            "TMP": str(resolved_temp),
            "TMPDIR": str(resolved_temp),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
        }
    )
    environment.update(
        _git_config_environment(
            (
                ("credential.helper", ""),
                ("credential.interactive", "false"),
                ("core.askPass", str(failing_helper)),
                ("core.fsmonitor", "false"),
                ("core.hooksPath", str(hooks_path)),
            )
        )
    )
    return environment


def _argv(value: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise FactoryPolicyViolation(f"{field_name} must be an argument vector")
    command = tuple(value)
    if not command or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in command
    ):
        raise FactoryPolicyViolation(
            f"{field_name} must be a non-empty argument vector"
        )
    return command


def _executable_name(value: str) -> str:
    name = Path(value).name.casefold()
    return name.removesuffix(".exe")


def _reject_publication(command: tuple[str, ...]) -> None:
    executable = _executable_name(command[0])
    if executable == "gh":
        raise FactoryPolicyViolation("GitHub commands are prohibited in factory mode")
    if executable in _SHELL_EXECUTABLES:
        raise FactoryPolicyViolation(
            "shell interpreter commands are prohibited in factory mode"
        )
    if executable == "git":
        subcommand = command[1].casefold() if len(command) > 1 else ""
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS or any(
            argument.split("=", 1)[0] in _UNSAFE_READ_ONLY_GIT_ARGUMENTS
            for argument in command
        ):
            raise FactoryPolicyViolation(
                "only allowlisted read-only Git commands are permitted in factory mode"
            )


def validate_declared_command(
    command: Sequence[str],
    allowed_commands: Sequence[Sequence[str]],
) -> None:
    """Require an exact argv match after applying immutable publication denial."""

    requested = _argv(command, "command")
    _reject_publication(requested)
    declared = {_argv(item, "allowed command") for item in allowed_commands}
    if requested not in declared:
        raise FactoryPolicyViolation("command is not declared by the run policy")


def resolve_trusted_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    prohibited_roots: Sequence[Path],
) -> tuple[str, ...]:
    """Resolve argv[0] through the runner-owned PATH and bind its real location."""

    requested = _argv(command, "command")
    executable = requested[0]
    if Path(executable).is_absolute():
        candidate = Path(executable)
    else:
        located = shutil.which(
            executable,
            path=environment.get("PATH", _TRUSTED_EXECUTABLE_PATH),
        )
        if located is None:
            raise FactoryPolicyViolation("declared command executable is unavailable")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise FactoryPolicyViolation(
            "declared command executable is unavailable"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise FactoryPolicyViolation("declared command executable is not executable")
    for root in prohibited_roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        raise FactoryPolicyViolation(
            "declared command executable is inside an untrusted mutable root"
        )
    return (str(resolved), *requested[1:])


__all__ = [
    "FactoryPolicyViolation",
    "audit_host_environment",
    "build_child_environment",
    "resolve_trusted_command",
    "validate_declared_command",
]

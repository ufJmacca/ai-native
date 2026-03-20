from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

_TELEMETRY_AUTH_TYPES = {"api_key", "bearer", "basic", "none"}
_COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


class WorkspaceConfig(BaseModel):
    artifacts_dir: Path = Path(".ai-native/runs")
    specs_dir: Path = Path("specs")
    base_branch: str = "main"
    parallel_mode: Literal["independent_only"] = "independent_only"
    parallel_workers: int = 4
    worktrees_dir: Path = Path(".ai-native/worktrees")
    dependency_policy: Literal["wait_for_base_merge", "assume_committed"] = "wait_for_base_merge"
    parallel_overlap_policy: Literal["path_prefix_block"] = "path_prefix_block"
    question_budget_per_stage: int = 1
    question_budget_per_run: int = 3
    plan_max_attempts: int = 3
    architecture_max_attempts: int = 3
    prd_max_attempts: int = 3
    loop_max_attempts: int = 3
    verification_max_attempts: int = 3
    mermaid_validate_command: list[str] = Field(default_factory=lambda: ["mmdc"])
    mermaid_validate_args: list[str] = Field(default_factory=lambda: ["--quiet"])


class AgentProfile(BaseModel):
    type: Literal["codex-exec", "codex-review", "copilot-cli", "external-command"]
    model: str | None = None
    sandbox: str | None = None
    base_branch: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    search: bool = False
    autopilot: bool | None = None
    allow_all_permissions: bool | None = None
    silent: bool | None = None
    no_ask_user: bool | None = None
    max_autopilot_continues: int | None = Field(default=None, ge=1)
    allow_tools: list[str] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)
    allow_urls: list[str] = Field(default_factory=list)
    deny_urls: list[str] = Field(default_factory=list)


class GitConfig(BaseModel):
    branch_prefix: str = "codex"
    conventional_prefix: str = "feat"
    pr_draft: bool = True


class QualityGates(BaseModel):
    require_plan_approval: bool = True
    require_diagram_approval: bool = True
    require_prd_approval: bool = True
    require_test_critique: bool = True
    require_red_green_refactor: bool = True


class RegistryConfig(BaseModel):
    heartbeat_interval_seconds: int = 15
    liveness_ttl_seconds: int = 60
    liveness_grace_period_seconds: int = 120
    remote_url: str | None = None
    auth_token: str | None = None
    timeout_seconds: float = 5.0


class TelemetryDestination(BaseModel):
    url: str
    auth_type: Literal["none", "bearer", "basic", "api_key"] = "none"
    credentials_ref: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class TelemetryConfig(BaseModel):
    enabled: bool = False
    profile: str | None = None
    destinations: dict[str, TelemetryDestination] = Field(default_factory=dict)
    url: str | None = None
    auth_type: Literal["api_key", "bearer", "basic", "none"] = "none"
    api_key: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    tenant: str | None = None


def codex_home() -> Path:
    return (Path.home() / ".codex").resolve()


def copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".copilot").resolve()


def provider_runtime_checks(which: Callable[[str], str | None] | None = None) -> dict[str, str | None]:
    resolver = shutil.which if which is None else which
    codex_root = codex_home()
    copilot_root = copilot_home()
    return {
        "codex": resolver("codex"),
        "copilot": resolver("copilot"),
        "codex_auth": str(codex_root / "auth.json"),
        "codex_config": str(codex_root / "config.toml"),
        "copilot_dir": str(copilot_root),
        "copilot_config": str(copilot_root / "config.json"),
    }


def provider_readiness(checks: Mapping[str, str | None]) -> dict[str, bool]:
    return {
        "codex": bool(checks.get("codex"))
        and Path(str(checks["codex_auth"])).exists()
        and Path(str(checks["codex_config"])).exists(),
        # Copilot auth can come from env vars, keychain, gh auth, or local config.
        "copilot": bool(checks.get("copilot")),
    }


def copilot_has_auth_signal(which: Callable[[str], str | None] | None = None) -> bool:
    resolver = shutil.which if which is None else which
    if any(_read_env(name) for name in _COPILOT_TOKEN_ENV_VARS):
        return True
    if Path(copilot_home() / "config.json").exists():
        return True
    gh_hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    return bool(resolver("gh")) and gh_hosts.exists()


def default_agents() -> dict[str, AgentProfile]:
    return {
        "builder": AgentProfile(
            type="codex-exec",
            model="gpt-5.4",
            sandbox="workspace-write",
            extra_args=["--full-auto", "-c", 'model_reasoning_effort="xhigh"'],
        ),
        "critic": AgentProfile(
            type="codex-exec",
            model="gpt-5.4",
            sandbox="workspace-write",
            extra_args=["--full-auto", "-c", 'model_reasoning_effort="xhigh"'],
        ),
        "verifier": AgentProfile(
            type="codex-exec",
            model="gpt-5.4",
            sandbox="workspace-write",
            extra_args=["--full-auto", "-c", 'model_reasoning_effort="xhigh"'],
        ),
        "pr_reviewer": AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            base_branch="main",
            extra_args=["-c", 'model_reasoning_effort="xhigh"'],
        ),
    }


def copilot_default_agents() -> dict[str, AgentProfile]:
    return {
        "builder": AgentProfile(
            type="copilot-cli",
            autopilot=True,
            allow_all_permissions=True,
            silent=True,
            no_ask_user=True,
            max_autopilot_continues=10,
        ),
        "critic": AgentProfile(
            type="copilot-cli",
            autopilot=True,
            allow_all_permissions=True,
            silent=True,
            no_ask_user=True,
            max_autopilot_continues=10,
        ),
        "verifier": AgentProfile(
            type="copilot-cli",
            autopilot=True,
            allow_all_permissions=True,
            silent=True,
            no_ask_user=True,
            max_autopilot_continues=10,
        ),
        "pr_reviewer": AgentProfile(
            type="copilot-cli",
            autopilot=False,
            allow_all_permissions=False,
            silent=True,
            no_ask_user=True,
            allow_tools=["read", "shell(git:*)"],
        ),
    }


def default_agents_for_missing_config(
    which: Callable[[str], str | None] | None = None,
) -> dict[str, AgentProfile]:
    readiness = provider_readiness(provider_runtime_checks(which))
    if readiness["copilot"] and not readiness["codex"] and copilot_has_auth_signal(which):
        return copilot_default_agents()
    return default_agents()


class AppConfig(BaseModel):
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    agents: dict[str, AgentProfile] = Field(default_factory=default_agents)
    git: GitConfig = Field(default_factory=GitConfig)
    quality_gates: QualityGates = Field(default_factory=QualityGates)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    config_path: Path = Field(default=Path("ainative.yaml"), exclude=True)
    repo_root: Path = Field(default=Path.cwd(), exclude=True)
    package_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent, exclude=True)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        resolved_path = path.resolve()
        raw = {}
        config_exists = resolved_path.exists()
        if config_exists:
            raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(raw)
        if not config_exists:
            config.agents = default_agents_for_missing_config()
        config.config_path = resolved_path
        config.repo_root = resolved_path.parent.resolve()
        config.package_root = Path(__file__).resolve().parent
        config.workspace.specs_dir = (config.repo_root / config.workspace.specs_dir).resolve()
        config._apply_environment_overrides()
        return config

    def _apply_environment_overrides(self) -> None:
        if env_registry_url := _read_env("AINATIVE_RUN_REGISTRY_URL"):
            self.registry.remote_url = env_registry_url

        if env_registry_auth_token := _read_env("AINATIVE_RUN_REGISTRY_AUTH_TOKEN"):
            self.registry.auth_token = env_registry_auth_token

        if env_registry_timeout := _read_env("AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS"):
            try:
                self.registry.timeout_seconds = float(env_registry_timeout)
            except ValueError as exc:
                raise ValueError(
                    "Invalid AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS="
                    f"{env_registry_timeout!r}. Expected a numeric timeout in seconds."
                ) from exc

        if env_url := _read_env("AINATIVE_TELEMETRY_URL"):
            self.telemetry.url = env_url
            self.telemetry.enabled = True

        if env_auth_type := _read_env("AINATIVE_TELEMETRY_AUTH_TYPE"):
            normalized_auth_type = env_auth_type.lower()
            if normalized_auth_type not in _TELEMETRY_AUTH_TYPES:
                allowed = ", ".join(sorted(_TELEMETRY_AUTH_TYPES))
                raise ValueError(
                    f"Invalid AINATIVE_TELEMETRY_AUTH_TYPE={env_auth_type!r}. Expected one of: {allowed}."
                )
            self.telemetry.auth_type = normalized_auth_type

        if env_api_key := _read_env("AINATIVE_TELEMETRY_API_KEY"):
            self.telemetry.api_key = env_api_key
            self.telemetry.enabled = True

        if env_token := _read_env("AINATIVE_TELEMETRY_TOKEN"):
            self.telemetry.token = env_token
            self.telemetry.enabled = True

        if env_username := _read_env("AINATIVE_TELEMETRY_USERNAME"):
            self.telemetry.username = env_username
            self.telemetry.enabled = True

        if env_password := _read_env("AINATIVE_TELEMETRY_PASSWORD"):
            self.telemetry.password = env_password
            self.telemetry.enabled = True

        if env_tenant := _read_env("AINATIVE_TELEMETRY_TENANT"):
            self.telemetry.tenant = env_tenant
            self.telemetry.enabled = True

        if env_enabled := _read_env("AINATIVE_TELEMETRY_ENABLED"):
            self.telemetry.enabled = env_enabled.lower() in {"1", "true", "yes", "on"}

    def resolve_artifacts_dir(self, workspace_root: Path) -> Path:
        root = self.workspace.artifacts_dir
        if root.is_absolute():
            return root.resolve()
        return (workspace_root.resolve() / root).resolve()

    def resolve_worktrees_dir(self, workspace_root: Path) -> Path:
        root = self.workspace.worktrees_dir
        if root.is_absolute():
            return root.resolve()
        return (workspace_root.resolve() / root).resolve()


def _read_env(name: str) -> str | None:
    from os import environ

    value = environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None

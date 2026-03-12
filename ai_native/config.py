from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


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
    type: Literal["codex-exec", "codex-review", "external-command"]
    model: str | None = None
    sandbox: str | None = None
    base_branch: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    search: bool = False


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


class AppConfig(BaseModel):
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    agents: dict[str, AgentProfile] = Field(default_factory=default_agents)
    git: GitConfig = Field(default_factory=GitConfig)
    quality_gates: QualityGates = Field(default_factory=QualityGates)
    config_path: Path = Field(default=Path("ainative.yaml"), exclude=True)
    repo_root: Path = Field(default=Path.cwd(), exclude=True)
    package_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent, exclude=True)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        resolved_path = path.resolve()
        raw = {}
        if resolved_path.exists():
            raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(raw)
        config.config_path = resolved_path
        config.repo_root = resolved_path.parent.resolve()
        config.package_root = Path(__file__).resolve().parent
        config.workspace.specs_dir = (config.repo_root / config.workspace.specs_dir).resolve()
        return config

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

from __future__ import annotations

from ai_native.adapters.base import AdapterError, AgentAdapter, ReviewAdapter
from ai_native.adapters.codex import CodexExecAdapter, CodexReviewAdapter
from ai_native.adapters.external import ExternalCommandAdapter
from ai_native.config import AppConfig, AgentProfile


def build_adapter(profile: AgentProfile) -> AgentAdapter:
    if profile.type == "codex-exec":
        return CodexExecAdapter(profile)
    if profile.type == "codex-review":
        return CodexReviewAdapter(profile)
    if profile.type == "external-command":
        return ExternalCommandAdapter(profile)
    raise AdapterError(f"Unsupported adapter type: {profile.type}")


def build_role_adapters(config: AppConfig) -> dict[str, AgentAdapter | ReviewAdapter]:
    return {role: build_adapter(profile) for role, profile in config.agents.items()}


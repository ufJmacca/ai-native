from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ai_native.workflow_stages import FACTORY_ELIGIBLE_STAGES


class ExecutionMode(StrEnum):
    """Execution profiles with deliberately separate authority."""

    LEGACY = "legacy"
    FACTORY = "factory"


class ExecutionCapability(StrEnum):
    """Security-relevant actions that an execution profile may perform."""

    AUTHOR = "author"
    VERIFY = "verify"
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    PUSH = "push"
    MERGE = "merge"
    INTERACTIVE_INPUT = "interactive_input"


_FACTORY_EXECUTION_CAPABILITIES = frozenset(
    {
        ExecutionCapability.AUTHOR,
        ExecutionCapability.VERIFY,
    }
)


@dataclass(frozen=True, slots=True)
class FactoryModeCapabilities:
    """Immutable authority ceiling for all future factory-runner work."""

    mode: ExecutionMode = field(default=ExecutionMode.FACTORY, init=False)
    allowed: frozenset[ExecutionCapability] = field(
        default_factory=lambda: _FACTORY_EXECUTION_CAPABILITIES,
        init=False,
    )
    allowed_stages: frozenset[str] = field(
        default_factory=lambda: FACTORY_ELIGIBLE_STAGES,
        init=False,
    )

    def permits(self, capability: ExecutionCapability) -> bool:
        return capability in self.allowed

    def permits_stage(self, stage: str) -> bool:
        return stage in self.allowed_stages


DEFAULT_FACTORY_MODE_CAPABILITIES = FactoryModeCapabilities()

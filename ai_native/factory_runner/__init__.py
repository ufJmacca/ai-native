"""Public boundary for AI Native's non-interactive factory mode.

AN-00 intentionally exposes policy only. Protocol contracts and executable
runner commands are introduced by later phases.
"""

from ai_native.factory_runner.policy import (
    DEFAULT_FACTORY_MODE_CAPABILITIES,
    ExecutionCapability,
    ExecutionMode,
    FactoryModeCapabilities,
)

__all__ = [
    "DEFAULT_FACTORY_MODE_CAPABILITIES",
    "ExecutionCapability",
    "ExecutionMode",
    "FactoryModeCapabilities",
]


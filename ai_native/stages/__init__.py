"""Lazy compatibility exports for legacy stage orchestration."""

from __future__ import annotations

import importlib
from typing import Any

from ai_native.workflow_stages import LEGACY_ORDERED_STAGES


ORDERED_STAGES = list(LEGACY_ORDERED_STAGES)
_LAZY_EXPORTS = {
    "commit_run": ("ai_native.stages.git_pr", "commit_run"),
    "create_prs": ("ai_native.stages.git_pr", "create_prs"),
    "run_architecture": ("ai_native.stages.architecture", "run"),
    "run_intake": ("ai_native.stages.intake", "run"),
    "run_loop": ("ai_native.stages.loop", "run"),
    "run_plan": ("ai_native.stages.planning", "run"),
    "run_prd": ("ai_native.stages.prd", "run"),
    "run_recon": ("ai_native.stages.recon", "run"),
    "run_slice": ("ai_native.stages.slicing", "run"),
    "run_verify": ("ai_native.stages.verify", "run"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(importlib.import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "ORDERED_STAGES",
    "commit_run",
    "create_prs",
    "run_architecture",
    "run_intake",
    "run_loop",
    "run_plan",
    "run_prd",
    "run_recon",
    "run_slice",
    "run_verify",
]

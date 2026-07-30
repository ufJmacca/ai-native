from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    FactoryStage,
    NonEmptyString,
    OpaqueId,
    Sha256Digest,
    StrictContractModel,
    require_unique,
)
from ai_native.factory_runner.contracts.run_spec import (
    CapabilityName,
    RunPolicy,
)


class CheckpointCompatibility(StrictContractModel):
    protocol: Literal["factory-runner-protocol/v1"]
    required_capabilities: tuple[CapabilityName, ...]
    minimum_runner_version: NonEmptyString

    @field_validator("required_capabilities")
    @classmethod
    def capabilities_are_unique(
        cls,
        value: tuple[CapabilityName, ...],
    ) -> tuple[CapabilityName, ...]:
        return require_unique(value, "required_capabilities")


class ResourceBudget(StrictContractModel):
    wall_seconds: StrictInt = Field(ge=0)
    agent_turns: StrictInt = Field(ge=0)
    model_tokens: StrictInt = Field(ge=0)


class CheckpointBudgets(StrictContractModel):
    consumed: ResourceBudget
    remaining: ResourceBudget


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("workflow_state exceeds the nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("workflow_state numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("workflow_state keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("workflow_state must contain JSON values only")


class Checkpoint(DocumentEnvelope):
    schema_: Literal["checkpoint/v1"] = Field(alias="schema")
    checkpoint_id: OpaqueId
    sequence: StrictInt = Field(gt=0)
    producer_attempt_id: OpaqueId
    compatibility: CheckpointCompatibility
    context_bundle_digest: Sha256Digest
    run_spec_digest: Sha256Digest
    workspace_patch_digest: Sha256Digest | None
    completed_stages: tuple[FactoryStage, ...]
    next_permitted_stage: FactoryStage | None
    workflow_state: dict[str, Any]
    evidence_refs: tuple[ArtifactReference, ...]
    artifact_manifest: tuple[ArtifactReference, ...]
    authority: RunPolicy
    budgets: CheckpointBudgets
    decisions: tuple[NonEmptyString, ...]
    assumptions: tuple[NonEmptyString, ...]
    open_questions: tuple[NonEmptyString, ...]
    object_digests: tuple[Sha256Digest, ...]
    checkpoint_digest: Sha256Digest

    @field_validator("workflow_state")
    @classmethod
    def workflow_state_is_bounded_json(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 262_144:
            raise ValueError("workflow_state exceeds the inline byte limit")
        return value

    @model_validator(mode="after")
    def checkpoint_is_internally_consistent(self) -> Checkpoint:
        if self.producer_attempt_id != self.identity.attempt_id:
            raise ValueError(
                "producer_attempt_id must equal the checkpoint attempt identity"
            )
        require_unique(self.completed_stages, "completed_stages")
        require_unique(self.object_digests, "object_digests")
        if not set(self.completed_stages).issubset(self.authority.allowed_stages):
            raise ValueError("completed stages exceed checkpoint authority")
        if (
            self.next_permitted_stage is not None
            and self.next_permitted_stage not in self.authority.allowed_stages
        ):
            raise ValueError("next_permitted_stage exceeds checkpoint authority")

        limits = {
            "wall_seconds": self.authority.max_wall_seconds,
            "agent_turns": self.authority.max_agent_turns,
            "model_tokens": self.authority.max_model_tokens,
        }
        for field_name, limit in limits.items():
            consumed = getattr(self.budgets.consumed, field_name)
            remaining = getattr(self.budgets.remaining, field_name)
            if consumed + remaining != limit:
                raise ValueError(
                    f"checkpoint {field_name} consumed plus remaining "
                    "must equal original authority"
                )
        return self


__all__ = [
    "Checkpoint",
    "CheckpointBudgets",
    "CheckpointCompatibility",
    "ResourceBudget",
]

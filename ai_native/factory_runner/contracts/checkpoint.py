from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from ai_native.factory_runner.canonical import canonical_json_bytes
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    FactoryStage,
    JsonInteger,
    MAX_SAFE_INTEGER,
    NonEmptyString,
    OpaqueId,
    PositiveSequence,
    SemanticVersion,
    Sha256Digest,
    StrictContractModel,
    bounded_json_object_schema,
    freeze_mapping,
    require_unique,
    thaw_json_value,
)
from ai_native.factory_runner.contracts.run_spec import (
    CapabilityName,
    RunPolicy,
    RunnerOperation,
)


class CheckpointCompatibility(StrictContractModel):
    protocol: Literal["factory-runner-protocol/v1"]
    required_capabilities: tuple[CapabilityName, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    minimum_runner_version: SemanticVersion

    @field_validator("required_capabilities")
    @classmethod
    def capabilities_are_unique(
        cls,
        value: tuple[CapabilityName, ...],
    ) -> tuple[CapabilityName, ...]:
        return require_unique(value, "required_capabilities")


class ResourceBudget(StrictContractModel):
    wall_seconds: JsonInteger = Field(ge=0, le=MAX_SAFE_INTEGER)
    agent_turns: JsonInteger = Field(ge=0, le=MAX_SAFE_INTEGER)
    model_tokens: JsonInteger = Field(ge=0, le=MAX_SAFE_INTEGER)


class CheckpointBudgets(StrictContractModel):
    consumed: ResourceBudget
    remaining: ResourceBudget


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("workflow_state exceeds the nesting limit")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("workflow_state integer exceeds the RFC 8785 domain")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("workflow_state numbers must be finite")
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("workflow_state number exceeds the RFC 8785 domain")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("workflow_state keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("workflow_state must contain JSON values only")


class Checkpoint(DocumentEnvelope):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"const": "author"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "verification_change_set_digest": {"type": "null"}
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"operation": {"const": "verify"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "verification_change_set_digest": {"not": {"type": "null"}},
                            "authority": {
                                "properties": {
                                    "allowed_stages": {"const": ["verify"]},
                                    "allowed_commands": {"minItems": 1},
                                }
                            },
                            "completed_stages": {
                                "items": {"const": "verify"},
                            },
                            "next_permitted_stage": {
                                "enum": ["verify", None],
                            },
                        }
                    },
                },
            ]
        }
    )

    schema_: Literal["checkpoint/v1"] = Field(alias="schema")
    checkpoint_id: OpaqueId
    sequence: PositiveSequence
    producer_attempt_id: OpaqueId
    compatibility: CheckpointCompatibility
    context_bundle_digest: Sha256Digest
    run_spec_digest: Sha256Digest
    operation: RunnerOperation
    verification_change_set_digest: Sha256Digest | None
    workspace_patch_digest: Sha256Digest | None
    completed_stages: tuple[FactoryStage, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    next_permitted_stage: FactoryStage | None
    workflow_state: Mapping[StrictStr, Any]
    evidence_refs: tuple[ArtifactReference, ...]
    artifact_manifest: tuple[ArtifactReference, ...]
    authority: RunPolicy
    budgets: CheckpointBudgets
    decisions: tuple[NonEmptyString, ...]
    assumptions: tuple[NonEmptyString, ...]
    open_questions: tuple[NonEmptyString, ...]
    object_digests: tuple[Sha256Digest, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    checkpoint_digest: Sha256Digest

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        return bounded_json_object_schema(
            schema,
            field_name="workflow_state",
            definition_prefix="WorkflowJsonDepth",
            max_depth=16,
        )

    @field_validator("workflow_state")
    @classmethod
    def workflow_state_is_bounded_json(
        cls,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _validate_json_value(value)
        encoded = canonical_json_bytes(value)
        if len(encoded) > 262_144:
            raise ValueError("workflow_state exceeds the inline byte limit")
        return freeze_mapping(value)

    @field_serializer("workflow_state")
    def serialize_workflow_state(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @field_validator("artifact_manifest")
    @classmethod
    def artifact_manifest_paths_are_unique(
        cls,
        value: tuple[ArtifactReference, ...],
    ) -> tuple[ArtifactReference, ...]:
        require_unique(
            tuple(artifact.path for artifact in value),
            "artifact_manifest paths",
        )
        return value

    @model_validator(mode="after")
    def checkpoint_is_internally_consistent(self) -> Checkpoint:
        if self.producer_attempt_id != self.identity.attempt_id:
            raise ValueError(
                "producer_attempt_id must equal the checkpoint attempt identity"
            )
        if (
            self.operation == "author"
            and self.verification_change_set_digest is not None
        ):
            raise ValueError(
                "author checkpoints must not bind a verification change set"
            )
        if self.operation == "verify" and self.verification_change_set_digest is None:
            raise ValueError(
                "verify checkpoints require a verification change-set digest"
            )
        if self.operation == "verify":
            if self.authority.allowed_stages != ("verify",):
                raise ValueError(
                    "verify checkpoints require verify-only stage authority"
                )
            if not self.authority.allowed_commands:
                raise ValueError(
                    "verify checkpoints require at least one deterministic command"
                )
            if any(stage != "verify" for stage in self.completed_stages):
                raise ValueError(
                    "verify checkpoints may complete only the verify stage"
                )
            if self.next_permitted_stage not in {None, "verify"}:
                raise ValueError("verify checkpoints may permit only the verify stage")
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

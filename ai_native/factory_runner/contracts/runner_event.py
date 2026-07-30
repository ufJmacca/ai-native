from __future__ import annotations

import math
from collections.abc import Mapping
import re
from typing import Any, Literal

from pydantic import Field, StrictStr, field_serializer, field_validator

from ai_native.factory_runner.canonical import canonical_json_bytes
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    MAX_SAFE_INTEGER,
    OpaqueId,
    PositiveSequence,
    SchemaVersion,
    StrictContractModel,
    UtcTimestamp,
    ascii_case_insensitive_pattern,
    bounded_json_object_schema,
    freeze_mapping,
    thaw_json_value,
)


RunnerEventType = Literal[
    "RunnerStarted",
    "InputValidated",
    "CheckpointRestored",
    "StageStarted",
    "StageCompleted",
    "ToolStarted",
    "ToolCompleted",
    "TestStarted",
    "TestCompleted",
    "FileManifestChanged",
    "CheckpointWritten",
    "ChangeSetWritten",
    "VerificationEvidenceWritten",
    "PolicyDenied",
    "RunnerCancellationRequested",
    "RunnerCompleted",
    "RunnerFailed",
]

MAX_SANITISED_PAYLOAD_BYTES = 16_384
MAX_SANITISED_PAYLOAD_DEPTH = 8
MAX_INLINE_STRING_LENGTH = 4096
_PROHIBITED_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "branch_name",
        "credential",
        "github_token",
        "hidden_reasoning",
        "merge",
        "password",
        "pr_body",
        "pr_title",
        "publication_token",
        "pull_request",
        "raw_reasoning",
        "secret",
        "token",
    }
)
_PROHIBITED_PAYLOAD_KEY_PATTERN = re.compile(
    ascii_case_insensitive_pattern(_PROHIBITED_PAYLOAD_KEYS)
)


def _validate_sanitised_json(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_SANITISED_PAYLOAD_DEPTH:
        raise ValueError("sanitised payload exceeds the nesting limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("sanitised payload integer exceeds the RFC 8785 domain")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("sanitised payload numbers must be finite")
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("sanitised payload number exceeds the RFC 8785 domain")
        return
    if isinstance(value, str):
        if len(value) > MAX_INLINE_STRING_LENGTH:
            raise ValueError("sanitised payload string exceeds the inline limit")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_sanitised_json(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("sanitised payload object keys must be strings")
            if _PROHIBITED_PAYLOAD_KEY_PATTERN.fullmatch(key):
                raise ValueError("sanitised payload contains a prohibited key")
            _validate_sanitised_json(item, depth=depth + 1)
        return
    raise ValueError("sanitised payload must contain JSON values only")


class RunnerEvent(StrictContractModel):
    protocol: Literal["factory-runner-protocol/v1"]
    schema_: Literal["runner-event/v1"] = Field(alias="schema")
    schema_version: SchemaVersion
    run_id: OpaqueId
    attempt_id: OpaqueId
    sequence: PositiveSequence
    timestamp: UtcTimestamp
    event_type: RunnerEventType
    correlation_id: OpaqueId
    causation_id: OpaqueId | None
    sanitised_payload: Mapping[StrictStr, Any] = Field(
        json_schema_extra={
            "propertyNames": {
                "not": {
                    "pattern": ascii_case_insensitive_pattern(_PROHIBITED_PAYLOAD_KEYS)
                }
            }
        }
    )
    artifact_refs: tuple[ArtifactReference, ...]

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        return bounded_json_object_schema(
            schema,
            field_name="sanitised_payload",
            definition_prefix="SanitisedJsonDepth",
            max_depth=MAX_SANITISED_PAYLOAD_DEPTH,
            max_string_length=MAX_INLINE_STRING_LENGTH,
            prohibited_keys=_PROHIBITED_PAYLOAD_KEYS,
        )

    @field_validator("sanitised_payload")
    @classmethod
    def payload_is_bounded_sanitised_json(
        cls,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _validate_sanitised_json(value)
        encoded = canonical_json_bytes(value)
        if len(encoded) > MAX_SANITISED_PAYLOAD_BYTES:
            raise ValueError("sanitised payload exceeds the byte limit")
        return freeze_mapping(value)

    @field_serializer("sanitised_payload")
    def serialize_sanitised_payload(
        self,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        return thaw_json_value(value)

from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    OpaqueId,
    SchemaVersion,
    StrictContractModel,
    UtcTimestamp,
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


def _validate_sanitised_json(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_SANITISED_PAYLOAD_DEPTH:
        raise ValueError("sanitised payload exceeds the nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("sanitised payload numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_INLINE_STRING_LENGTH:
            raise ValueError("sanitised payload string exceeds the inline limit")
        return
    if isinstance(value, list):
        for item in value:
            _validate_sanitised_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("sanitised payload object keys must be strings")
            if key.casefold() in _PROHIBITED_PAYLOAD_KEYS:
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
    sequence: StrictInt = Field(gt=0)
    timestamp: UtcTimestamp
    event_type: RunnerEventType
    correlation_id: OpaqueId
    causation_id: OpaqueId | None
    sanitised_payload: dict[str, Any]
    artifact_refs: list[ArtifactReference]

    @field_validator("sanitised_payload")
    @classmethod
    def payload_is_bounded_sanitised_json(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_sanitised_json(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_SANITISED_PAYLOAD_BYTES:
            raise ValueError("sanitised payload exceeds the byte limit")
        return value

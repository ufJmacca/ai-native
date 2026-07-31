from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    SchemaVersion,
    Sha256Digest,
    StrictContractModel,
    UtcTimestamp,
)
from ai_native.factory_runner.contracts.run_result import RunOutcome


class EventStreamReference(ArtifactReference):
    path: Literal["events.ndjson"]
    media_type: Literal["application/x-ndjson"]


class ProtocolManifestReference(ArtifactReference):
    path: Literal["protocol-manifest.json"]
    media_type: Literal["application/json"]


class RunResultReference(ArtifactReference):
    path: Literal["result/run-result.json"]
    media_type: Literal["application/json"]


class ProtocolManifest(StrictContractModel):
    """Acyclic content-addressed inventory published before terminal output."""

    protocol: Literal["factory-runner-protocol/v1"]
    schema_: Literal["protocol-manifest/v1"] = Field(alias="schema")
    schema_version: SchemaVersion
    event_stream: EventStreamReference
    artifacts: tuple[ArtifactReference, ...] = Field(
        min_length=1,
        max_length=20_000,
        json_schema_extra={"uniqueItems": True},
    )

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        artifact_schema = schema["properties"]["artifacts"]
        artifact_schema.update(
            {
                "contains": {
                    "type": "object",
                    "properties": {
                        "path": {"const": "events.ndjson"},
                        "media_type": {
                            "const": "application/x-ndjson",
                        },
                    },
                    "required": ["path", "media_type"],
                },
                "minContains": 1,
                "maxContains": 1,
                "not": {
                    "contains": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "enum": [
                                    "completion.json",
                                    "protocol-manifest.json",
                                    "result/run-result.json",
                                ]
                            }
                        },
                        "required": ["path"],
                    }
                },
            }
        )
        return schema

    @model_validator(mode="after")
    def validate_artifact_inventory(self) -> ProtocolManifest:
        paths = tuple(reference.path for reference in self.artifacts)
        if paths != tuple(sorted(paths)):
            raise ValueError("protocol manifest artifacts must be sorted by path")
        if len(paths) != len(set(paths)):
            raise ValueError("protocol manifest artifact paths must be unique")
        event_payload = self.event_stream.model_dump(mode="json")
        matching_events = tuple(
            reference
            for reference in self.artifacts
            if reference.path == self.event_stream.path
        )
        if (
            len(matching_events) != 1
            or matching_events[0].model_dump(mode="json") != event_payload
        ):
            raise ValueError(
                "protocol manifest must contain its exact event stream reference"
            )
        terminal_paths = {
            "completion.json",
            "protocol-manifest.json",
            "result/run-result.json",
        }
        if terminal_paths.intersection(paths):
            raise ValueError(
                "protocol manifest may not create a cyclic terminal reference"
            )
        return self


class CompletionManifest(StrictContractModel):
    """Last-write marker binding one result to its immutable output manifest."""

    protocol: Literal["factory-runner-protocol/v1"]
    schema_: Literal["completion/v1"] = Field(alias="schema")
    schema_version: SchemaVersion
    completed_at: UtcTimestamp
    outcome: RunOutcome
    output_manifest_digest: Sha256Digest
    protocol_manifest: ProtocolManifestReference | None
    run_result: RunResultReference

    @model_validator(mode="after")
    def validate_manifest_binding(self) -> CompletionManifest:
        if (
            self.protocol_manifest is not None
            and self.protocol_manifest.digest != self.output_manifest_digest
        ):
            raise ValueError(
                "completion output manifest digest must match its manifest reference"
            )
        return self


__all__ = [
    "CompletionManifest",
    "EventStreamReference",
    "ProtocolManifest",
    "ProtocolManifestReference",
    "RunResultReference",
]

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    FactoryStage,
    NonEmptyString,
    RunnerBuildIdentity,
    Sha256Digest,
    UtcTimestamp,
    ensure_started_before_finished,
    require_unique,
)


RunnerOperation = Literal["author", "verify"]
RunOutcome = Literal[
    "succeeded",
    "no_change",
    "blocked",
    "failed",
    "cancelled",
    "timed_out",
    "invalid_input",
    "checkpoint_incompatible",
    "policy_denied",
]


class RunResult(DocumentEnvelope):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"const": "author"}},
                        "required": ["operation"],
                    },
                    "then": {"properties": {"verification_evidence": {"type": "null"}}},
                },
                {
                    "if": {
                        "properties": {"operation": {"const": "verify"}},
                        "required": ["operation"],
                    },
                    "then": {"properties": {"change_set": {"type": "null"}}},
                },
                {
                    "if": {
                        "properties": {
                            "operation": {"const": "author"},
                            "outcome": {"const": "succeeded"},
                        },
                        "required": ["operation", "outcome"],
                    },
                    "then": {"properties": {"change_set": {"not": {"type": "null"}}}},
                },
                {
                    "if": {
                        "properties": {
                            "operation": {"const": "verify"},
                            "outcome": {"const": "succeeded"},
                        },
                        "required": ["operation", "outcome"],
                    },
                    "then": {
                        "properties": {
                            "verification_evidence": {"not": {"type": "null"}}
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"outcome": {"const": "no_change"}},
                        "required": ["outcome"],
                    },
                    "then": {
                        "properties": {
                            "operation": {"const": "author"},
                            "change_set": {"type": "null"},
                            "verification_evidence": {"type": "null"},
                        }
                    },
                },
            ]
        }
    )

    schema_: Literal["run-result/v1"] = Field(alias="schema")
    operation: RunnerOperation
    outcome: RunOutcome
    reason_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    message: NonEmptyString = Field(max_length=4096)
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    completed_stages: list[FactoryStage] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    latest_checkpoint: ArtifactReference | None
    change_set: ArtifactReference | None
    verification_evidence: ArtifactReference | None
    event_stream_digest: Sha256Digest
    output_manifest_digest: Sha256Digest
    runner_build: RunnerBuildIdentity
    result_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_timing_and_result_references(self) -> RunResult:
        ensure_started_before_finished(self.started_at, self.finished_at)
        require_unique(tuple(self.completed_stages), "completed_stages")
        if self.operation == "author" and self.verification_evidence is not None:
            raise ValueError("author results cannot claim clean-verification evidence")
        if self.operation == "verify" and self.change_set is not None:
            raise ValueError("verify results cannot contain a change set")
        if self.outcome == "succeeded":
            if self.operation == "author" and self.change_set is None:
                raise ValueError("successful author results require a change set")
            if self.operation == "verify" and self.verification_evidence is None:
                raise ValueError(
                    "successful verify results require verification evidence"
                )
        if self.outcome == "no_change":
            if self.operation != "author":
                raise ValueError("no_change is valid only for author operations")
            if self.change_set is not None or self.verification_evidence is not None:
                raise ValueError("no_change cannot reference change or evidence output")
        return self

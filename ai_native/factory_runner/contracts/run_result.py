from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictStr, WithJsonSchema, model_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    FactoryStage,
    RepositoryIdentity,
    RunIdentity,
    RunnerBuildIdentity,
    SchemaVersion,
    Sha256Digest,
    StrictContractModel,
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
ReasonCode = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[a-z][a-z0-9_]*$",
            "not": {"pattern": r"[\u0000-\u001f\u007f]"},
        }
    ),
]
ResultMessage = Annotated[
    StrictStr,
    Field(min_length=1, max_length=4096),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
        }
    ),
]


class RunResult(StrictContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"outcome": {"not": {"const": "invalid_input"}}},
                        "required": ["outcome"],
                    },
                    "then": {
                        "properties": {
                            "identity": {"not": {"type": "null"}},
                            "repository": {"not": {"type": "null"}},
                        },
                        "required": ["identity", "repository"],
                    },
                },
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
                    "then": {
                        "properties": {
                            "change_set": {"type": "null"},
                            "completed_stages": {
                                "items": {"const": "verify"},
                            },
                        }
                    },
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
                            "verification_evidence": {"not": {"type": "null"}},
                            "completed_stages": {"const": ["verify"]},
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

    protocol: Literal["factory-runner-protocol/v1"]
    schema_: Literal["run-result/v1"] = Field(alias="schema")
    schema_version: SchemaVersion
    created_at: UtcTimestamp
    identity: RunIdentity | None
    repository: RepositoryIdentity | None
    operation: RunnerOperation
    outcome: RunOutcome
    reason_code: ReasonCode
    message: ResultMessage
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    completed_stages: tuple[FactoryStage, ...] = Field(
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
        require_unique(self.completed_stages, "completed_stages")
        if self.operation == "verify":
            if any(stage != "verify" for stage in self.completed_stages):
                raise ValueError("verify results may complete only the verify stage")
            if self.outcome == "succeeded" and self.completed_stages != ("verify",):
                raise ValueError(
                    "successful verify results require exactly the verify stage"
                )
        if self.outcome != "invalid_input" and (
            self.identity is None or self.repository is None
        ):
            raise ValueError(
                "source identity is required after input validation succeeds"
            )
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

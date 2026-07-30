from __future__ import annotations

from typing import Literal

import math

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    EnvironmentKey,
    NonEmptyString,
    NonNegativeSeconds,
    RepositoryPath,
    RunnerBuildIdentity,
    SafeInteger,
    Sha256Digest,
    StrictContractModel,
    UtcTimestamp,
    ensure_started_before_finished,
    freeze_mapping,
)


EvidencePhase = Literal["red", "green", "refactor", "verification"]
EvidenceStatus = Literal["passed", "failed", "blocked", "not_run"]
TerminationReason = Literal[
    "exited",
    "signalled",
    "timed_out",
    "cancelled",
    "not_started",
]
FailureClassification = Literal[
    "none",
    "expected_behavioral_failure",
    "assertion_failure",
    "test_failure",
    "syntax_error",
    "collection_error",
    "dependency_error",
    "credential_error",
    "infrastructure_error",
    "timeout",
    "unrelated_failure",
]


class EvidenceItem(StrictContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"phase": {"const": "red"}},
                        "required": ["phase"],
                    },
                    "then": {
                        "properties": {
                            "exit_code": {
                                "not": {
                                    "anyOf": [
                                        {"type": "null"},
                                        {"const": 0},
                                    ]
                                }
                            },
                            "expected_status": {"const": "failed"},
                            "actual_status": {"const": "failed"},
                            "failure_classification": {
                                "const": "expected_behavioral_failure"
                            },
                            "termination_reason": {"const": "exited"},
                        }
                    },
                }
            ]
        }
    )

    phase: EvidencePhase
    command: tuple[NonEmptyString, ...] = Field(min_length=1)
    working_directory: RepositoryPath | Literal["."]
    environment_keys: tuple[EnvironmentKey, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    duration_seconds: NonNegativeSeconds
    exit_code: SafeInteger | None
    termination_reason: TerminationReason
    expected_status: EvidenceStatus
    actual_status: EvidenceStatus
    failure_classification: FailureClassification
    stdout: ArtifactReference
    stderr: ArtifactReference
    test_reports: tuple[ArtifactReference, ...]
    tool_versions: dict[NonEmptyString, NonEmptyString]
    repository_files_changed: StrictBool

    @field_validator("command")
    @classmethod
    def command_is_an_argument_array(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument or "\x00" in argument for argument in value):
            raise ValueError("command arguments must be non-empty and contain no NUL")
        return value

    @field_validator("environment_keys")
    @classmethod
    def environment_keys_are_unique(
        cls,
        value: tuple[EnvironmentKey, ...],
    ) -> tuple[EnvironmentKey, ...]:
        if len(value) != len(set(value)):
            raise ValueError("environment keys must be unique")
        return value

    @field_validator("tool_versions")
    @classmethod
    def tool_versions_are_immutable(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        return freeze_mapping(value)

    @field_validator("duration_seconds")
    @classmethod
    def duration_is_finite(cls, value: float | int) -> float | int:
        if not math.isfinite(value):
            raise ValueError("duration_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_timing_and_red_semantics(self) -> EvidenceItem:
        ensure_started_before_finished(self.started_at, self.finished_at)
        if self.phase == "red":
            if self.termination_reason != "exited":
                raise ValueError("red evidence must complete by process exit")
            if self.exit_code in (None, 0):
                raise ValueError("red evidence must have a non-zero exit code")
            if self.expected_status != "failed" or self.actual_status != "failed":
                raise ValueError("red evidence must expect and observe failure")
            if self.failure_classification != "expected_behavioral_failure":
                raise ValueError("red evidence must be an expected behavioral failure")
        if self.actual_status == "passed" and self.exit_code != 0:
            raise ValueError("passing evidence must have exit code zero")
        return self


class VerificationEvidence(DocumentEnvelope):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"environment_kind": {"const": "authoring"}},
                        "required": ["environment_kind"],
                    },
                    "then": {"properties": {"change_set_digest": {"type": "null"}}},
                },
                {
                    "if": {
                        "properties": {
                            "environment_kind": {"const": "clean_verification"}
                        },
                        "required": ["environment_kind"],
                    },
                    "then": {
                        "properties": {"change_set_digest": {"not": {"type": "null"}}}
                    },
                },
            ]
        }
    )

    schema_: Literal["verification-evidence/v1"] = Field(alias="schema")
    environment_kind: Literal["authoring", "clean_verification"]
    runner: RunnerBuildIdentity
    context_digest: Sha256Digest
    change_set_digest: Sha256Digest | None
    items: tuple[EvidenceItem, ...] = Field(min_length=1)
    overall_status: EvidenceStatus
    advisory_observations: tuple[str, ...]
    evidence_set_digest: Sha256Digest

    @model_validator(mode="after")
    def evidence_digest_direction_is_one_way(self) -> VerificationEvidence:
        if self.environment_kind == "authoring" and self.change_set_digest is not None:
            raise ValueError("authoring evidence must not bind a future change set")
        if self.environment_kind == "clean_verification" and (
            self.change_set_digest is None
        ):
            raise ValueError("clean verification evidence requires change_set_digest")
        return self

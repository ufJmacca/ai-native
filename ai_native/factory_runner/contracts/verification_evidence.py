from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    field_serializer,
    field_validator,
    model_validator,
)

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    CommandArgument,
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
    thaw_json_value,
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
                },
                {
                    "if": {
                        "properties": {"phase": {"not": {"const": "red"}}},
                        "required": ["phase"],
                    },
                    "then": {"properties": {"expected_status": {"const": "passed"}}},
                },
                {
                    "if": {
                        "properties": {"actual_status": {"const": "passed"}},
                        "required": ["actual_status"],
                    },
                    "then": {
                        "properties": {
                            "exit_code": {"const": 0},
                            "termination_reason": {"const": "exited"},
                            "failure_classification": {"const": "none"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"termination_reason": {"const": "timed_out"}},
                        "required": ["termination_reason"],
                    },
                    "then": {
                        "properties": {
                            "actual_status": {"const": "failed"},
                            "failure_classification": {"const": "timeout"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"failure_classification": {"const": "timeout"}},
                        "required": ["failure_classification"],
                    },
                    "then": {
                        "properties": {"termination_reason": {"const": "timed_out"}}
                    },
                },
                {
                    "if": {
                        "properties": {"actual_status": {"const": "not_run"}},
                        "required": ["actual_status"],
                    },
                    "then": {
                        "properties": {
                            "exit_code": {"type": "null"},
                            "termination_reason": {"const": "not_started"},
                            "failure_classification": {"const": "none"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"termination_reason": {"const": "not_started"}},
                        "required": ["termination_reason"],
                    },
                    "then": {
                        "properties": {
                            "exit_code": {"type": "null"},
                            "actual_status": {
                                "enum": [
                                    "blocked",
                                    "not_run",
                                ]
                            },
                        }
                    },
                },
            ]
        }
    )

    phase: EvidencePhase
    command: tuple[CommandArgument, ...] = Field(min_length=1)
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
    tool_versions: Mapping[NonEmptyString, NonEmptyString]
    repository_files_changed: StrictBool

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
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        return freeze_mapping(value)

    @field_serializer("tool_versions")
    def serialize_tool_versions(
        self,
        value: Mapping[str, str],
    ) -> dict[str, str]:
        return thaw_json_value(value)

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
        elif self.expected_status != "passed":
            raise ValueError("non-red evidence must expect success")
        if self.actual_status == "passed" and (
            self.exit_code != 0
            or self.termination_reason != "exited"
            or self.failure_classification != "none"
        ):
            raise ValueError(
                "passing evidence must exit zero without a failure classification"
            )
        if self.termination_reason == "timed_out" and (
            self.actual_status != "failed" or self.failure_classification != "timeout"
        ):
            raise ValueError("timed-out evidence must be classified as a timeout")
        if (
            self.failure_classification == "timeout"
            and self.termination_reason != "timed_out"
        ):
            raise ValueError("timeout classification requires timed-out termination")
        if self.actual_status == "not_run" and (
            self.exit_code is not None
            or self.termination_reason != "not_started"
            or self.failure_classification != "none"
        ):
            raise ValueError("not-run evidence must describe an unstarted command")
        if self.termination_reason == "not_started" and (
            self.exit_code is not None
            or self.actual_status not in {"blocked", "not_run"}
        ):
            raise ValueError("unstarted evidence must be blocked or not run")
        if self.actual_status == "failed":
            if self.termination_reason == "not_started":
                raise ValueError("failed evidence must have started")
            if self.termination_reason == "exited" and self.exit_code in (None, 0):
                raise ValueError("failed exited evidence requires a non-zero exit code")
            if self.failure_classification == "none":
                raise ValueError("failed evidence requires a failure classification")
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
                        "properties": {
                            "change_set_digest": {"not": {"type": "null"}},
                            "items": {
                                "items": {
                                    "properties": {"phase": {"const": "verification"}},
                                    "required": ["phase"],
                                }
                            },
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "environment_kind": {"const": "clean_verification"},
                            "overall_status": {"const": "passed"},
                        },
                        "required": ["environment_kind", "overall_status"],
                    },
                    "then": {
                        "properties": {
                            "items": {
                                "items": {
                                    "properties": {
                                        "repository_files_changed": {"const": False}
                                    },
                                    "required": ["repository_files_changed"],
                                }
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"overall_status": {"const": "passed"}},
                        "required": ["overall_status"],
                    },
                    "then": {
                        "properties": {
                            "items": {
                                "not": {
                                    "contains": {
                                        "anyOf": [
                                            {
                                                "properties": {
                                                    "phase": {"not": {"const": "red"}},
                                                    "actual_status": {
                                                        "const": "failed"
                                                    },
                                                },
                                                "required": [
                                                    "phase",
                                                    "actual_status",
                                                ],
                                            },
                                            {
                                                "properties": {
                                                    "actual_status": {
                                                        "const": "blocked"
                                                    }
                                                },
                                                "required": ["actual_status"],
                                            },
                                            {
                                                "properties": {
                                                    "actual_status": {
                                                        "const": "not_run"
                                                    }
                                                },
                                                "required": ["actual_status"],
                                            },
                                        ]
                                    }
                                }
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"overall_status": {"const": "failed"}},
                        "required": ["overall_status"],
                    },
                    "then": {
                        "properties": {
                            "items": {
                                "contains": {
                                    "properties": {
                                        "phase": {"not": {"const": "red"}},
                                        "actual_status": {"const": "failed"},
                                    },
                                    "required": ["phase", "actual_status"],
                                }
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"overall_status": {"const": "blocked"}},
                        "required": ["overall_status"],
                    },
                    "then": {
                        "properties": {
                            "items": {
                                "allOf": [
                                    {
                                        "not": {
                                            "contains": {
                                                "properties": {
                                                    "phase": {"not": {"const": "red"}},
                                                    "actual_status": {
                                                        "const": "failed"
                                                    },
                                                },
                                                "required": [
                                                    "phase",
                                                    "actual_status",
                                                ],
                                            }
                                        }
                                    },
                                    {
                                        "contains": {
                                            "properties": {
                                                "actual_status": {"const": "blocked"}
                                            },
                                            "required": ["actual_status"],
                                        }
                                    },
                                ]
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"overall_status": {"const": "not_run"}},
                        "required": ["overall_status"],
                    },
                    "then": {
                        "properties": {
                            "items": {
                                "allOf": [
                                    {
                                        "not": {
                                            "contains": {
                                                "properties": {
                                                    "phase": {"not": {"const": "red"}},
                                                    "actual_status": {
                                                        "const": "failed"
                                                    },
                                                },
                                                "required": [
                                                    "phase",
                                                    "actual_status",
                                                ],
                                            }
                                        }
                                    },
                                    {
                                        "not": {
                                            "contains": {
                                                "properties": {
                                                    "actual_status": {
                                                        "const": "blocked"
                                                    }
                                                },
                                                "required": ["actual_status"],
                                            }
                                        }
                                    },
                                    {
                                        "contains": {
                                            "properties": {
                                                "actual_status": {"const": "not_run"}
                                            },
                                            "required": ["actual_status"],
                                        }
                                    },
                                ]
                            }
                        }
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
    advisory_observations: tuple[NonEmptyString, ...]
    evidence_set_digest: Sha256Digest

    @model_validator(mode="after")
    def evidence_digest_direction_is_one_way(self) -> VerificationEvidence:
        if self.environment_kind == "authoring" and self.change_set_digest is not None:
            raise ValueError("authoring evidence must not bind a future change set")
        if self.environment_kind == "clean_verification" and (
            self.change_set_digest is None
        ):
            raise ValueError("clean verification evidence requires change_set_digest")
        if self.environment_kind == "clean_verification" and any(
            item.phase != "verification" for item in self.items
        ):
            raise ValueError(
                "clean verification evidence may contain only verification items"
            )
        if (
            self.environment_kind == "clean_verification"
            and self.overall_status == "passed"
            and any(item.repository_files_changed for item in self.items)
        ):
            raise ValueError(
                "passing clean verification must not mutate repository files"
            )
        derived_status: EvidenceStatus
        if any(
            item.phase != "red" and item.actual_status == "failed"
            for item in self.items
        ):
            derived_status = "failed"
        elif any(item.actual_status == "blocked" for item in self.items):
            derived_status = "blocked"
        elif any(item.actual_status == "not_run" for item in self.items):
            derived_status = "not_run"
        else:
            derived_status = "passed"
        if self.overall_status != derived_status:
            raise ValueError(
                "overall_status must equal the deterministic item aggregate"
            )
        return self

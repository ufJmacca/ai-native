from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ai_native.factory_runner.contracts.common import (
    AbsolutePosixPath,
    DocumentEnvelope,
    EnvironmentKey,
    FactoryStage,
    MAX_SAFE_INTEGER,
    NonEmptyString,
    OpaqueId,
    PolicyPath,
    ProfileName,
    ProhibitedPath,
    Sha256Digest,
    StrictContractModel,
    contains_secret_reference,
    require_unique,
)


RunnerOperation = Literal["author", "verify"]
PositiveInt = Annotated[StrictInt, Field(gt=0, le=MAX_SAFE_INTEGER)]
CapabilityName = Annotated[
    OpaqueId,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
Command = Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]


class WorkspaceSpec(StrictContractModel):
    path: AbsolutePosixPath
    initial_state: Literal["clean_base", "prepared_verification"]


class TaskSpec(StrictContractModel):
    outcome: NonEmptyString
    acceptance_criteria: tuple[NonEmptyString, ...]
    non_goals: tuple[NonEmptyString, ...]
    constraints: tuple[NonEmptyString, ...]


class RunPolicy(StrictContractModel):
    allowed_paths: Annotated[
        tuple[PolicyPath, ...],
        Field(min_length=1, json_schema_extra={"uniqueItems": True}),
    ]
    prohibited_paths: tuple[ProhibitedPath, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    allowed_stages: Annotated[
        tuple[FactoryStage, ...],
        Field(min_length=1, json_schema_extra={"uniqueItems": True}),
    ]
    allowed_commands: tuple[Command, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    allowed_environment_keys: tuple[EnvironmentKey, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    network_profile: ProfileName
    credential_profile: Literal["no-external-credentials"]
    model_profile: ProfileName
    max_wall_seconds: PositiveInt
    max_agent_turns: PositiveInt
    max_model_tokens: PositiveInt

    @field_validator(
        "allowed_paths",
        "prohibited_paths",
        "allowed_stages",
        "allowed_commands",
        "allowed_environment_keys",
    )
    @classmethod
    def authority_entries_are_unique(
        cls,
        value: tuple[object, ...],
        info: object,
    ) -> tuple[object, ...]:
        field_name = getattr(info, "field_name", "authority")
        return require_unique(value, str(field_name))

    @field_validator("model_profile")
    @classmethod
    def model_profile_is_not_a_secret_or_url(cls, value: str) -> str:
        if contains_secret_reference(value):
            raise ValueError("model_profile must be an opaque non-secret profile name")
        return value

    @model_validator(mode="after")
    def allowed_and_prohibited_paths_do_not_conflict(self) -> RunPolicy:
        overlap = set(self.allowed_paths).intersection(self.prohibited_paths)
        if overlap:
            raise ValueError("a path cannot be both allowed and prohibited")
        return self


class CapabilityRequirements(StrictContractModel):
    required: tuple[CapabilityName, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    optional: tuple[CapabilityName, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )

    @model_validator(mode="after")
    def capabilities_are_unambiguous(self) -> CapabilityRequirements:
        require_unique(self.required, "required capabilities")
        require_unique(self.optional, "optional capabilities")
        if set(self.required).intersection(self.optional):
            raise ValueError("required and optional capabilities must not overlap")
        return self


class ContextInput(StrictContractModel):
    manifest_path: AbsolutePosixPath
    expected_digest: Sha256Digest


class VerificationInput(StrictContractModel):
    change_set_path: AbsolutePosixPath
    expected_digest: Sha256Digest


class ResumeInput(StrictContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "checkpoint_path": {"type": "null"},
                        "expected_digest": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "checkpoint_path": {"type": "string"},
                        "expected_digest": {"type": "string"},
                    }
                },
            ]
        }
    )

    checkpoint_path: AbsolutePosixPath | None
    expected_digest: Sha256Digest | None

    @model_validator(mode="after")
    def resume_path_and_digest_are_paired(self) -> ResumeInput:
        if (self.checkpoint_path is None) != (self.expected_digest is None):
            raise ValueError(
                "checkpoint_path and expected_digest must both be set or both be null"
            )
        return self


class OutputSpec(StrictContractModel):
    output_dir: AbsolutePosixPath
    stream_events_to_stdout: StrictBool


class RunSpec(DocumentEnvelope):
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
                            "workspace": {
                                "properties": {"initial_state": {"const": "clean_base"}}
                            },
                            "verification_input": {"type": "null"},
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
                            "workspace": {
                                "properties": {
                                    "initial_state": {"const": "prepared_verification"}
                                }
                            },
                            "policy": {
                                "properties": {"allowed_stages": {"const": ["verify"]}}
                            },
                            "verification_input": {"not": {"type": "null"}},
                        }
                    },
                },
            ]
        }
    )

    schema_: Literal["run-spec/v1"] = Field(alias="schema")
    operation: RunnerOperation
    workspace: WorkspaceSpec
    task: TaskSpec
    policy: RunPolicy
    capabilities: CapabilityRequirements
    context: ContextInput
    verification_input: VerificationInput | None
    resume: ResumeInput
    outputs: OutputSpec

    @model_validator(mode="after")
    def operation_matches_workspace_and_stage_authority(self) -> RunSpec:
        expected_state = (
            "clean_base" if self.operation == "author" else "prepared_verification"
        )
        if self.workspace.initial_state != expected_state:
            raise ValueError(
                f"{self.operation} requires workspace initial_state={expected_state}"
            )
        if self.operation == "verify" and set(self.policy.allowed_stages) != {"verify"}:
            raise ValueError("verify permits only the verification stage")
        if self.operation == "author" and self.verification_input is not None:
            raise ValueError("author does not accept verification_input")
        if self.operation == "verify" and self.verification_input is None:
            raise ValueError("verify requires a digest-bound verification_input")
        return self


__all__ = [
    "CapabilityRequirements",
    "Command",
    "ContextInput",
    "OutputSpec",
    "ResumeInput",
    "RunPolicy",
    "RunSpec",
    "RunnerOperation",
    "TaskSpec",
    "VerificationInput",
    "WorkspaceSpec",
]

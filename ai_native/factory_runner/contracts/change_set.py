from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, field_validator, model_validator

from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    NonEmptyString,
    OpaqueId,
    RepositoryPath,
    Sha256Digest,
    StrictContractModel,
)


ChangeOperation = Literal["add", "modify", "delete", "rename"]
RegularFileMode = Literal["100644", "100755"]


class PatchArtifact(ArtifactReference):
    media_type: Literal["application/vnd.git.binary-patch"]


class ChangedFile(StrictContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"const": "add"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "previous_path": {"type": "null"},
                            "previous_blob_digest": {"type": "null"},
                            "previous_mode": {"type": "null"},
                            "resulting_blob_digest": {"not": {"type": "null"}},
                            "resulting_mode": {"not": {"type": "null"}},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"operation": {"const": "delete"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "previous_path": {"type": "null"},
                            "previous_blob_digest": {"not": {"type": "null"}},
                            "previous_mode": {"not": {"type": "null"}},
                            "resulting_blob_digest": {"type": "null"},
                            "resulting_mode": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"operation": {"const": "modify"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "previous_path": {"type": "null"},
                            "previous_blob_digest": {"not": {"type": "null"}},
                            "previous_mode": {"not": {"type": "null"}},
                            "resulting_blob_digest": {"not": {"type": "null"}},
                            "resulting_mode": {"not": {"type": "null"}},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"operation": {"const": "rename"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {
                            "previous_path": {"not": {"type": "null"}},
                            "previous_blob_digest": {"not": {"type": "null"}},
                            "previous_mode": {"not": {"type": "null"}},
                            "resulting_blob_digest": {"not": {"type": "null"}},
                            "resulting_mode": {"not": {"type": "null"}},
                        }
                    },
                },
            ]
        }
    )

    path: RepositoryPath
    operation: ChangeOperation
    previous_path: RepositoryPath | None
    previous_blob_digest: Sha256Digest | None
    resulting_blob_digest: Sha256Digest | None
    previous_mode: RegularFileMode | None
    resulting_mode: RegularFileMode | None
    binary: StrictBool
    allowed_path_decision: Literal["allowed"]

    @model_validator(mode="after")
    def validate_operation_fields(self) -> ChangedFile:
        if self.operation != "rename" and self.previous_path is not None:
            raise ValueError("previous_path is valid only for rename")
        if self.operation == "add":
            if self.previous_blob_digest is not None or self.previous_mode is not None:
                raise ValueError("add must not describe a previous blob")
            if self.resulting_blob_digest is None or self.resulting_mode is None:
                raise ValueError("add requires a resulting blob and mode")
        elif self.operation == "delete":
            if self.previous_blob_digest is None or self.previous_mode is None:
                raise ValueError("delete requires a previous blob and mode")
            if (
                self.resulting_blob_digest is not None
                or self.resulting_mode is not None
            ):
                raise ValueError("delete must not describe a resulting blob")
        elif self.operation == "modify":
            if (
                self.previous_blob_digest is None
                or self.resulting_blob_digest is None
                or self.previous_mode is None
                or self.resulting_mode is None
            ):
                raise ValueError(
                    "modify requires previous and resulting blobs and modes"
                )
            if (
                self.previous_blob_digest == self.resulting_blob_digest
                and self.previous_mode == self.resulting_mode
            ):
                raise ValueError("modify must change the blob digest or file mode")
        elif self.operation == "rename":
            if self.previous_path is None or self.previous_path == self.path:
                raise ValueError("rename requires a distinct previous_path")
            if (
                self.previous_blob_digest is None
                or self.resulting_blob_digest is None
                or self.previous_mode is None
                or self.resulting_mode is None
            ):
                raise ValueError(
                    "rename requires previous and resulting blobs and modes"
                )
        return self


class AcceptanceCriterionResult(StrictContractModel):
    criterion: NonEmptyString
    status: Literal["passed", "failed", "blocked", "not_run"]


class ChangeSet(DocumentEnvelope):
    schema_: Literal["change-set/v1"] = Field(alias="schema")
    change_set_id: OpaqueId
    runner_digest: Sha256Digest
    context_digest: Sha256Digest
    patch: PatchArtifact
    diff_digest: Sha256Digest
    changed_files: list[ChangedFile] = Field(min_length=1)
    evidence_set_digest: Sha256Digest
    evidence_refs: list[ArtifactReference] = Field(min_length=1)
    acceptance_criteria_results: list[AcceptanceCriterionResult]
    outcome_summary: NonEmptyString
    assumptions: list[NonEmptyString]
    residual_risks: list[NonEmptyString]
    policy_observations: list[NonEmptyString]
    generated_artifacts: list[ArtifactReference]
    change_set_digest: Sha256Digest

    @field_validator("changed_files")
    @classmethod
    def changed_paths_are_unique(
        cls,
        value: list[ChangedFile],
    ) -> list[ChangedFile]:
        paths = [entry.path for entry in value]
        if len(paths) != len(set(paths)):
            raise ValueError("changed file paths must be unique")
        return value

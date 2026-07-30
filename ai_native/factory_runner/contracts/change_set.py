from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, field_validator, model_validator

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    DocumentEnvelope,
    JsonInteger,
    MAX_SAFE_INTEGER,
    NonEmptyString,
    OpaqueId,
    RepositoryPath,
    Sha256Digest,
    StrictContractModel,
)


ChangeOperation = Literal["add", "modify", "delete", "rename"]
RegularFileMode = Literal["100644", "100755"]


class PatchArtifact(ArtifactReference):
    byte_size: JsonInteger = Field(gt=0, le=MAX_SAFE_INTEGER)
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


def _validated_changed_file_manifest(
    entries: Sequence[ChangedFile | Mapping[str, object]],
) -> tuple[ChangedFile, ...]:
    if isinstance(entries, (str, bytes, bytearray)) or not isinstance(
        entries,
        Sequence,
    ):
        raise ValueError("changed-file manifest must be an ordered sequence")
    if not entries:
        raise ValueError("changed-file manifest must be a non-empty sequence")
    validated = tuple(
        ChangedFile.model_validate(
            entry.model_dump(mode="python") if isinstance(entry, ChangedFile) else entry
        )
        for entry in entries
    )
    target_paths = tuple(entry.path for entry in validated)
    if len(target_paths) != len(set(target_paths)):
        raise ValueError("changed file target paths must be unique")
    source_paths = tuple(
        entry.previous_path
        if entry.operation == "rename"
        else entry.path
        if entry.operation in {"modify", "delete"}
        else None
        for entry in validated
    )
    present_source_paths = tuple(path for path in source_paths if path is not None)
    if len(present_source_paths) != len(set(present_source_paths)):
        raise ValueError("changed file source paths must be unique")
    return validated


def changed_file_manifest_digest(
    entries: Sequence[ChangedFile | Mapping[str, object]],
) -> str:
    """Digest a validated, ordered changed-file manifest."""

    validated = _validated_changed_file_manifest(entries)
    manifest = [entry.model_dump(mode="json") for entry in validated]
    return sha256_digest(canonical_json_bytes(manifest))


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
    changed_files: tuple[ChangedFile, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    evidence_set_digest: Sha256Digest
    evidence_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    acceptance_criteria_results: tuple[AcceptanceCriterionResult, ...]
    outcome_summary: NonEmptyString
    assumptions: tuple[NonEmptyString, ...]
    residual_risks: tuple[NonEmptyString, ...]
    policy_observations: tuple[NonEmptyString, ...]
    generated_artifacts: tuple[ArtifactReference, ...]
    change_set_digest: Sha256Digest

    @field_validator("changed_files")
    @classmethod
    def changed_paths_are_unique(
        cls,
        value: tuple[ChangedFile, ...],
    ) -> tuple[ChangedFile, ...]:
        return _validated_changed_file_manifest(value)

    @model_validator(mode="after")
    def diff_digest_binds_changed_files(self) -> ChangeSet:
        if self.diff_digest != changed_file_manifest_digest(self.changed_files):
            raise ValueError("diff_digest must bind the ordered changed-file manifest")
        return self

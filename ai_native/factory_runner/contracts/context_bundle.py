from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ai_native.factory_runner.contracts.common import (
    ByteSize,
    DocumentEnvelope,
    NonEmptyString,
    OpaqueId,
    RepositoryPath,
    Sha256Digest,
    StrictContractModel,
    require_unique,
)


ContextClassification = Literal[
    "work_item_revision",
    "repository_instruction",
    "trusted_policy",
    "approved_project_memory",
    "dependency_output",
    "operator_input",
    "supporting_artifact",
]


class ContextManifestEntry(StrictContractModel):
    logical_path: RepositoryPath
    media_type: NonEmptyString
    byte_size: ByteSize
    digest: Sha256Digest
    classification: ContextClassification


class NormalisedWorkItemRevision(StrictContractModel):
    outcome: NonEmptyString
    acceptance_criteria: tuple[NonEmptyString, ...]


class ContextConstruction(StrictContractModel):
    builder: NonEmptyString
    source_digests: tuple[Sha256Digest, ...]

    @field_validator("source_digests")
    @classmethod
    def source_digests_are_unique(
        cls,
        value: tuple[Sha256Digest, ...],
    ) -> tuple[Sha256Digest, ...]:
        return require_unique(value, "source_digests")


class ContextBundle(DocumentEnvelope):
    schema_: Literal["context-bundle/v1"] = Field(alias="schema")
    context_bundle_id: OpaqueId
    manifest_entries: tuple[ContextManifestEntry, ...] = Field(min_length=1)
    work_item_revision: NormalisedWorkItemRevision
    repository_instructions: tuple[NonEmptyString, ...]
    trusted_policy_summary: tuple[NonEmptyString, ...]
    approved_repository_memory: tuple[NonEmptyString, ...]
    dependency_outputs: tuple[NonEmptyString, ...]
    operator_input: tuple[NonEmptyString, ...]
    construction: ContextConstruction
    bundle_digest: Sha256Digest

    @model_validator(mode="after")
    def manifest_is_deterministic_and_scoped(self) -> ContextBundle:
        paths = tuple(entry.logical_path for entry in self.manifest_entries)
        require_unique(paths, "manifest logical paths")
        revision_entries = sum(
            entry.classification == "work_item_revision"
            for entry in self.manifest_entries
        )
        if revision_entries != 1:
            raise ValueError(
                "context bundle requires exactly one work_item_revision entry"
            )
        return self


__all__ = [
    "ContextBundle",
    "ContextClassification",
    "ContextConstruction",
    "ContextManifestEntry",
    "NormalisedWorkItemRevision",
]

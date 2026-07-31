from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from ai_native.factory_runner.contracts import (
    ChangeSet,
    Checkpoint,
    CompletionManifest,
    ContextBundle,
    ProtocolManifest,
    RunnerEvent,
    RunResult,
    RunSpec,
    VerificationEvidence,
)


JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True, slots=True)
class ContractSchema:
    schema: str
    filename: str
    schema_id: str
    model: type[BaseModel]


CONTRACT_SCHEMAS = (
    ContractSchema(
        schema="change-set/v1",
        filename="change-set.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:change-set",
        model=ChangeSet,
    ),
    ContractSchema(
        schema="checkpoint/v1",
        filename="checkpoint.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:checkpoint",
        model=Checkpoint,
    ),
    ContractSchema(
        schema="completion/v1",
        filename="completion.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:completion",
        model=CompletionManifest,
    ),
    ContractSchema(
        schema="context-bundle/v1",
        filename="context-bundle.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:context-bundle",
        model=ContextBundle,
    ),
    ContractSchema(
        schema="protocol-manifest/v1",
        filename="protocol-manifest.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:protocol-manifest",
        model=ProtocolManifest,
    ),
    ContractSchema(
        schema="run-result/v1",
        filename="run-result.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:run-result",
        model=RunResult,
    ),
    ContractSchema(
        schema="run-spec/v1",
        filename="run-spec.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:run-spec",
        model=RunSpec,
    ),
    ContractSchema(
        schema="runner-event/v1",
        filename="runner-event.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:runner-event",
        model=RunnerEvent,
    ),
    ContractSchema(
        schema="verification-evidence/v1",
        filename="verification-evidence.schema.json",
        schema_id="urn:ai-native:factory-runner:v1:verification-evidence",
        model=VerificationEvidence,
    ),
)

CONTRACT_SCHEMA_BY_NAME = {entry.schema: entry for entry in CONTRACT_SCHEMAS}

if tuple(entry.filename for entry in CONTRACT_SCHEMAS) != tuple(
    sorted(entry.filename for entry in CONTRACT_SCHEMAS)
):
    raise RuntimeError("factory runner schema registry must be ordered by filename")


__all__ = [
    "CONTRACT_SCHEMAS",
    "CONTRACT_SCHEMA_BY_NAME",
    "ContractSchema",
    "JSON_SCHEMA_DRAFT",
]

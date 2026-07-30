from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from importlib import resources
import json
import math
from typing import Any, TypeAlias

from pydantic import BaseModel, ValidationError

from ai_native.factory_runner.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from ai_native.factory_runner.contracts import (
    ChangeSet,
    Checkpoint,
    ContextBundle,
    RunnerEvent,
    RunResult,
    RunSpec,
    VerificationEvidence,
    changed_file_manifest_digest,
)
from ai_native.factory_runner.contracts.common import (
    MAX_SAFE_INTEGER,
    normalise_json_integer,
)
from ai_native.factory_runner.errors import (
    ContractErrorCode,
    ContractValidationError,
)
from ai_native.factory_runner.negotiation import (
    CheckpointCompatibilityResult,
    ProtocolNegotiationResult,
    negotiate_protocol,
    validate_checkpoint_compatibility,
)
from ai_native.factory_runner.schema_registry import (
    CONTRACT_SCHEMAS,
    CONTRACT_SCHEMA_BY_NAME,
)


PROTOCOL_V1 = "factory-runner-protocol/v1"
SELF_DIGEST_FIELDS = {
    "context-bundle/v1": "bundle_digest",
    "checkpoint/v1": "checkpoint_digest",
    "verification-evidence/v1": "evidence_set_digest",
    "change-set/v1": "change_set_digest",
    "run-result/v1": "result_digest",
}

ContractDocument: TypeAlias = (
    RunSpec
    | ContextBundle
    | Checkpoint
    | VerificationEvidence
    | ChangeSet
    | RunResult
    | RunnerEvent
)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(
                ContractErrorCode.INVALID_JSON,
                "JSON object contains a duplicate member name",
            )
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> None:
    raise ContractValidationError(
        ContractErrorCode.INVALID_JSON,
        f"JSON number is not finite: {value}",
    )


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_non_finite_number(value)
    return parsed


def _parse_safe_integer(value: str) -> int:
    digits = value.removeprefix("-")
    maximum = str(MAX_SAFE_INTEGER)
    if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
        raise ContractValidationError(
            ContractErrorCode.INVALID_JSON,
            "JSON integer exceeds the RFC 8785 interoperable domain",
        )
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractValidationError(
            ContractErrorCode.INVALID_JSON,
            "JSON integer is not in the RFC 8785 interoperable domain",
        ) from exc


def decode_json_document(value: str | bytes | bytearray) -> dict[str, Any]:
    try:
        text = (
            bytes(value).decode("utf-8")
            if isinstance(value, (bytes, bytearray))
            else value
        )
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite_number,
            parse_float=_parse_finite_float,
            parse_int=_parse_safe_integer,
        )
    except ContractValidationError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractValidationError(
            ContractErrorCode.INVALID_JSON,
            "input is not a valid UTF-8 JSON document",
        ) from exc
    if not isinstance(decoded, dict):
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "contract document must be a JSON object",
        )
    return decoded


def _mapping_document(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = deepcopy(dict(value))
        canonical_json_bytes(payload)
    except Exception as exc:
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "contract mapping must contain JSON-compatible values",
        ) from exc
    return payload


def _select_contract(
    payload: Mapping[str, Any],
    *,
    expected_schema: str | None,
) -> type[BaseModel]:
    protocol = payload.get("protocol")
    if not isinstance(protocol, str):
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "contract protocol is required",
        )
    if protocol != PROTOCOL_V1:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_PROTOCOL,
            "unsupported factory runner protocol",
        )

    schema = payload.get("schema")
    if not isinstance(schema, str):
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "contract schema is required",
        )
    if expected_schema is not None and schema != expected_schema:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_SCHEMA,
            "contract schema does not match the expected schema",
        )
    entry = CONTRACT_SCHEMA_BY_NAME.get(schema)
    if entry is None:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_SCHEMA,
            "unsupported factory runner contract schema",
        )

    try:
        schema_version = normalise_json_integer(payload.get("schema_version"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            "unsupported factory runner contract schema version",
        ) from exc
    if schema_version != 1:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            "unsupported factory runner contract schema version",
        )
    return entry.model


def validate_contract(
    value: ContractDocument | Mapping[str, Any] | str | bytes | bytearray,
    *,
    expected_schema: str | None = None,
) -> ContractDocument:
    """Structurally validate one v1 contract without importing workflow code."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = _mapping_document(value)
    elif isinstance(value, (str, bytes, bytearray)):
        payload = decode_json_document(value)
    else:
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "contract must be a model, mapping, or JSON document",
        )

    model = _select_contract(payload, expected_schema=expected_schema)
    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.errors()[0].get("loc", ()))
        suffix = f" at /{location}" if location else ""
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            f"contract failed structural validation{suffix}",
        ) from exc
    return validated  # type: ignore[return-value]


def contract_document_digest(
    value: ContractDocument | Mapping[str, Any],
    *,
    digest_field: str | None = None,
) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else _mapping_document(value)
    )
    selected_field = digest_field
    if selected_field is None:
        schema = payload.get("schema")
        selected_field = (
            SELF_DIGEST_FIELDS.get(schema) if isinstance(schema, str) else None
        )
    projected = deepcopy(payload)
    if selected_field is not None:
        if selected_field not in projected:
            raise ContractValidationError(
                ContractErrorCode.INVALID_INPUT,
                "self-digest field is missing",
            )
        del projected[selected_field]
    return sha256_digest(canonical_json_bytes(projected))


def verify_digest(content: bytes, expected_digest: str) -> None:
    if sha256_digest(content) != expected_digest:
        raise ContractValidationError(
            ContractErrorCode.DIGEST_MISMATCH,
            "content does not match its declared SHA-256 digest",
        )


def verify_contract_digest(
    value: ContractDocument | Mapping[str, Any],
) -> None:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else _mapping_document(value)
    )
    schema = payload.get("schema")
    digest_field = SELF_DIGEST_FIELDS.get(schema) if isinstance(schema, str) else None
    if digest_field is None:
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "this contract has no self-digest field",
        )
    declared = payload.get(digest_field)
    if (
        not isinstance(declared, str)
        or contract_document_digest(
            payload,
            digest_field=digest_field,
        )
        != declared
    ):
        raise ContractValidationError(
            ContractErrorCode.DIGEST_MISMATCH,
            "contract does not match its declared self digest",
        )


def _schema_resource(filename: str) -> resources.abc.Traversable:
    return (
        resources.files("ai_native")
        .joinpath("schemas")
        .joinpath("factory_runner")
        .joinpath("v1")
        .joinpath(filename)
    )


def load_contract_schema(schema: str) -> dict[str, Any]:
    entry = CONTRACT_SCHEMA_BY_NAME.get(schema)
    if entry is None:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_SCHEMA,
            "unsupported factory runner contract schema",
        )
    loaded = json.loads(_schema_resource(entry.filename).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ContractValidationError(
            ContractErrorCode.INVALID_INPUT,
            "packaged contract schema is not a JSON object",
        )
    return loaded


def schema_set_digest() -> str:
    return _schema_resource("schema-set.sha256").read_text(encoding="ascii").strip()


def schema_manifest_digest() -> str:
    return sha256_digest(_schema_resource("schema-manifest.json").read_bytes())


def iter_contract_schemas() -> tuple[str, ...]:
    return tuple(entry.schema for entry in CONTRACT_SCHEMAS)


__all__ = [
    "ChangeSet",
    "Checkpoint",
    "CheckpointCompatibilityResult",
    "ContextBundle",
    "ContractDocument",
    "ContractErrorCode",
    "ContractValidationError",
    "PROTOCOL_V1",
    "ProtocolNegotiationResult",
    "RunResult",
    "RunSpec",
    "RunnerEvent",
    "VerificationEvidence",
    "canonical_json_bytes",
    "changed_file_manifest_digest",
    "contract_document_digest",
    "decode_json_document",
    "iter_contract_schemas",
    "load_contract_schema",
    "negotiate_protocol",
    "schema_manifest_digest",
    "schema_set_digest",
    "sha256_digest",
    "validate_checkpoint_compatibility",
    "validate_contract",
    "verify_contract_digest",
    "verify_digest",
]

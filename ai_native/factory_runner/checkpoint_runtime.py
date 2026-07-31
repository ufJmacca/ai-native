"""Construct and publish portable checkpoints at completed safe boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any

from ai_native import __version__
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.checkpoints import CheckpointManager
from ai_native.factory_runner.contracts.checkpoint import (
    Checkpoint,
    ResourceBudget,
)
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    FactoryStage,
    MAX_SAFE_INTEGER,
)
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.protocol import contract_document_digest
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner


_MAX_CHECKPOINT_OBJECT_BYTES = 16 * 1024 * 1024
_ABSOLUTE_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)*[^/\s]+")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
_HOME_RELATIVE_PATH = re.compile(r"(?<![A-Za-z0-9])~/")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:"
    r"\b(?:api[_-]?key|authorization|password|access[_-]?token|"
    r"refresh[_-]?token|private[_ -]?key)\s*[:=]"
    r"|\bbearer\s+\S"
    r")"
)
_CREDENTIAL_KEY_MARKERS = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
    }
)


class CheckpointRuntimeError(FactoryPolicyViolation):
    """A safe-boundary checkpoint cannot be represented without policy loss."""


@dataclass(frozen=True, slots=True)
class CheckpointBundle:
    """A validated checkpoint and its detached immutable object payloads."""

    checkpoint: Checkpoint
    objects: Mapping[str, bytes]
    workflow_state_reference: ArtifactReference
    workspace_patch_reference: ArtifactReference | None


@dataclass(frozen=True, slots=True)
class WrittenCheckpoint:
    """A published checkpoint plus references needed by events and results."""

    checkpoint: Checkpoint
    reference: ArtifactReference
    workflow_state_reference: ArtifactReference
    workspace_patch_reference: ArtifactReference | None


def _is_credential_key(value: str) -> bool:
    normalised = "".join(
        character for character in value.casefold() if character.isalnum()
    )
    return any(marker in normalised for marker in _CREDENTIAL_KEY_MARKERS)


def _is_absolute_path(value: str) -> bool:
    return (
        _ABSOLUTE_POSIX_PATH.search(value) is not None
        or _ABSOLUTE_WINDOWS_PATH.search(value) is not None
        or _HOME_RELATIVE_PATH.search(value) is not None
        or "file://" in value.casefold()
    )


def _require_portable_json(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise CheckpointRuntimeError("portable workflow state exceeds its depth limit")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CheckpointRuntimeError(
                    "portable workflow state keys must be strings"
                )
            if _is_credential_key(key):
                raise CheckpointRuntimeError(
                    "portable workflow state contains credential material"
                )
            _require_portable_json(item, depth=depth + 1)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _require_portable_json(item, depth=depth + 1)
        return
    if isinstance(value, str):
        if _is_absolute_path(value):
            raise CheckpointRuntimeError(
                "portable checkpoint state contains an absolute path"
            )
        if _CREDENTIAL_VALUE.search(value) is not None:
            raise CheckpointRuntimeError(
                "portable checkpoint state contains credential material"
            )
        return
    if value is None or isinstance(value, bool | int | float):
        return
    raise CheckpointRuntimeError("portable workflow state must contain JSON values")


def _portable_texts(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise CheckpointRuntimeError(f"{field_name} must be an ordered sequence")
    copied = tuple(values)
    for value in copied:
        if not isinstance(value, str):
            raise CheckpointRuntimeError(f"{field_name} entries must be text")
        _require_portable_json(value)
    return copied


def _validated_consumed_budget(
    consumed: ResourceBudget,
    *,
    run_spec: RunSpec,
) -> tuple[ResourceBudget, ResourceBudget]:
    try:
        validated = ResourceBudget.model_validate(consumed.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CheckpointRuntimeError("checkpoint consumed budget is invalid") from exc

    limits = (
        ("wall_seconds", run_spec.policy.max_wall_seconds),
        ("agent_turns", run_spec.policy.max_agent_turns),
        ("model_tokens", run_spec.policy.max_model_tokens),
    )
    remaining: dict[str, int] = {}
    for field_name, limit in limits:
        used = getattr(validated, field_name)
        if used > limit:
            raise CheckpointRuntimeError(
                "checkpoint consumed budget exceeds run authority"
            )
        remaining[field_name] = limit - used
    return validated, ResourceBudget.model_validate(remaining)


def _validated_stages(
    completed_stages: Sequence[FactoryStage],
    next_permitted_stage: FactoryStage | None,
    *,
    run_spec: RunSpec,
) -> tuple[FactoryStage, ...]:
    if isinstance(completed_stages, str | bytes | bytearray):
        raise CheckpointRuntimeError(
            "checkpoint completed stages must be an ordered sequence"
        )
    completed = tuple(completed_stages)
    allowed = set(run_spec.policy.allowed_stages)
    if not set(completed).issubset(allowed):
        raise CheckpointRuntimeError("checkpoint completed stages exceed run authority")
    if next_permitted_stage is not None and next_permitted_stage not in allowed:
        raise CheckpointRuntimeError("checkpoint next stage exceeds run authority")
    if run_spec.operation == "verify" and (
        any(stage != "verify" for stage in completed)
        or next_permitted_stage not in {None, "verify"}
    ):
        raise CheckpointRuntimeError(
            "verify checkpoint stages exceed verify-only authority"
        )
    return completed


def _object_reference(
    *,
    sequence: int,
    content: bytes,
    media_type: str,
) -> ArtifactReference:
    digest = sha256_digest(content)
    return ArtifactReference(
        path=(f"checkpoints/{sequence}/objects/{digest.removeprefix('sha256:')}"),
        media_type=media_type,
        byte_size=len(content),
        digest=digest,
    )


def _checkpoint_id(run_spec: RunSpec, sequence: int) -> str:
    seed = canonical_json_bytes(
        {
            "identity": run_spec.identity,
            "sequence": sequence,
        }
    )
    return f"checkpoint-{sha256_digest(seed).removeprefix('sha256:')}"


def build_checkpoint_bundle(
    *,
    run_spec: RunSpec,
    context_bundle_digest: str,
    sequence: int,
    created_at: str,
    completed_stages: Sequence[FactoryStage],
    next_permitted_stage: FactoryStage | None,
    workflow_state: Mapping[str, Any],
    consumed: ResourceBudget,
    workspace_patch: bytes | None = None,
    evidence_refs: Sequence[ArtifactReference] = (),
    decisions: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    minimum_runner_version: str = __version__,
    secret_scanner: SecretScanner | None = None,
) -> CheckpointBundle:
    """Build a complete checkpoint bundle without touching the filesystem."""

    if not isinstance(run_spec, RunSpec):
        raise TypeError("run_spec must be an admitted RunSpec")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_SAFE_INTEGER
    ):
        raise CheckpointRuntimeError(
            "checkpoint sequence must be a positive JSON integer"
        )
    if context_bundle_digest != run_spec.context.expected_digest:
        raise CheckpointRuntimeError(
            "checkpoint context digest differs from the admitted run"
        )
    if not isinstance(workflow_state, Mapping):
        raise CheckpointRuntimeError("portable workflow state must be a JSON object")
    if workspace_patch is not None and not isinstance(workspace_patch, bytes):
        raise TypeError("workspace_patch must be bytes or null")
    if (
        workspace_patch is not None
        and len(workspace_patch) > _MAX_CHECKPOINT_OBJECT_BYTES
    ):
        raise CheckpointRuntimeError("checkpoint workspace patch exceeds its limit")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")

    _require_portable_json(workflow_state)
    durable_decisions = _portable_texts(decisions, field_name="decisions")
    durable_assumptions = _portable_texts(assumptions, field_name="assumptions")
    durable_questions = _portable_texts(
        open_questions,
        field_name="open_questions",
    )
    completed = _validated_stages(
        completed_stages,
        next_permitted_stage,
        run_spec=run_spec,
    )
    consumed_budget, remaining_budget = _validated_consumed_budget(
        consumed,
        run_spec=run_spec,
    )
    try:
        durable_evidence = tuple(
            ArtifactReference.model_validate(reference.model_dump(mode="json"))
            for reference in evidence_refs
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CheckpointRuntimeError(
            "checkpoint evidence references are invalid"
        ) from exc

    try:
        workflow_state_bytes = canonical_json_bytes(workflow_state)
    except ValueError as exc:
        raise CheckpointRuntimeError(
            "portable workflow state is not canonical JSON"
        ) from exc
    object_payloads: dict[str, bytes] = {}
    references: dict[str, ArtifactReference] = {}

    def add_object(content: bytes, media_type: str) -> ArtifactReference:
        reference = _object_reference(
            sequence=sequence,
            content=content,
            media_type=media_type,
        )
        existing = references.get(reference.path)
        if existing is not None:
            return existing
        references[reference.path] = reference
        object_payloads[reference.path] = bytes(content)
        return reference

    workflow_reference = add_object(
        workflow_state_bytes,
        "application/json",
    )
    patch_reference = (
        add_object(workspace_patch, "application/octet-stream")
        if workspace_patch is not None
        else None
    )
    manifest = tuple(references[path] for path in sorted(references))
    object_digests = tuple(reference.digest for reference in manifest)

    verification_digest = (
        run_spec.verification_input.expected_digest
        if run_spec.verification_input is not None
        else None
    )
    placeholder_digest = "sha256:" + ("0" * 64)
    try:
        draft = Checkpoint.model_validate(
            {
                "protocol": run_spec.protocol,
                "schema": "checkpoint/v1",
                "schema_version": 1,
                "created_at": created_at,
                "identity": run_spec.identity.model_dump(mode="json"),
                "repository": run_spec.repository.model_dump(mode="json"),
                "checkpoint_id": _checkpoint_id(run_spec, sequence),
                "sequence": sequence,
                "producer_attempt_id": run_spec.identity.attempt_id,
                "compatibility": {
                    "protocol": run_spec.protocol,
                    "required_capabilities": list(run_spec.capabilities.required),
                    "minimum_runner_version": minimum_runner_version,
                },
                "context_bundle_digest": context_bundle_digest,
                "run_spec_digest": contract_document_digest(run_spec),
                "operation": run_spec.operation,
                "verification_change_set_digest": verification_digest,
                "workspace_patch_digest": (
                    patch_reference.digest if patch_reference is not None else None
                ),
                "completed_stages": list(completed),
                "next_permitted_stage": next_permitted_stage,
                "workflow_state": workflow_state,
                "evidence_refs": [
                    reference.model_dump(mode="json") for reference in durable_evidence
                ],
                "artifact_manifest": [
                    reference.model_dump(mode="json") for reference in manifest
                ],
                "authority": run_spec.policy.model_dump(mode="json"),
                "budgets": {
                    "consumed": consumed_budget.model_dump(mode="json"),
                    "remaining": remaining_budget.model_dump(mode="json"),
                },
                "decisions": list(durable_decisions),
                "assumptions": list(durable_assumptions),
                "open_questions": list(durable_questions),
                "object_digests": list(object_digests),
                "checkpoint_digest": placeholder_digest,
            }
        )
        payload = draft.model_dump(mode="json")
        payload["checkpoint_digest"] = contract_document_digest(draft)
        checkpoint = Checkpoint.model_validate(payload)
    except CheckpointRuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise CheckpointRuntimeError(
            "checkpoint contract violates safe-boundary invariants"
        ) from exc

    scanner.require_clean_chunks(
        (
            *(object_payloads[path] for path in sorted(object_payloads)),
            canonical_json_bytes(checkpoint),
        )
    )
    return CheckpointBundle(
        checkpoint=checkpoint,
        objects=MappingProxyType(object_payloads),
        workflow_state_reference=workflow_reference,
        workspace_patch_reference=patch_reference,
    )


def write_checkpoint_boundary(
    manager: CheckpointManager,
    *,
    run_spec: RunSpec,
    context_bundle_digest: str,
    sequence: int,
    created_at: str,
    completed_stages: Sequence[FactoryStage],
    next_permitted_stage: FactoryStage | None,
    workflow_state: Mapping[str, Any],
    consumed: ResourceBudget,
    workspace_patch: bytes | None = None,
    evidence_refs: Sequence[ArtifactReference] = (),
    decisions: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    minimum_runner_version: str = __version__,
    secret_scanner: SecretScanner | None = None,
) -> WrittenCheckpoint:
    """Build, scan, and atomically publish one immutable safe boundary."""

    if not isinstance(manager, CheckpointManager):
        raise TypeError("manager must be a CheckpointManager")
    bundle = build_checkpoint_bundle(
        run_spec=run_spec,
        context_bundle_digest=context_bundle_digest,
        sequence=sequence,
        created_at=created_at,
        completed_stages=completed_stages,
        next_permitted_stage=next_permitted_stage,
        workflow_state=workflow_state,
        consumed=consumed,
        workspace_patch=workspace_patch,
        evidence_refs=evidence_refs,
        decisions=decisions,
        assumptions=assumptions,
        open_questions=open_questions,
        minimum_runner_version=minimum_runner_version,
        secret_scanner=secret_scanner,
    )
    reference = manager.write_safe_boundary(
        checkpoint=bundle.checkpoint,
        objects=bundle.objects,
    )
    return WrittenCheckpoint(
        checkpoint=bundle.checkpoint,
        reference=reference,
        workflow_state_reference=bundle.workflow_state_reference,
        workspace_patch_reference=bundle.workspace_patch_reference,
    )


__all__ = [
    "CheckpointBundle",
    "CheckpointRuntimeError",
    "WrittenCheckpoint",
    "build_checkpoint_bundle",
    "write_checkpoint_boundary",
]

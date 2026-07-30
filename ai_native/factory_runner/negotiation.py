from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from pydantic import BaseModel

from ai_native import __version__
from ai_native.factory_runner.errors import (
    ContractErrorCode,
    ContractValidationError,
)


PROTOCOL_V1 = "factory-runner-protocol/v1"
_SEMANTIC_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)$"
)


@dataclass(frozen=True, slots=True)
class ProtocolNegotiationResult:
    """Immutable record of a successful protocol negotiation."""

    protocol: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    negotiated_capabilities: tuple[str, ...]
    ignored_optional_capabilities: tuple[str, ...]

    @property
    def enabled_capabilities(self) -> tuple[str, ...]:
        return self.negotiated_capabilities

    @property
    def unsupported_optional_capabilities(self) -> tuple[str, ...]:
        return self.ignored_optional_capabilities


@dataclass(frozen=True, slots=True)
class CheckpointCompatibilityResult:
    """Immutable receipt for a successful checkpoint compatibility check."""

    protocol: str
    run_id: str
    producer_attempt_id: str
    resumed_attempt_id: str
    narrowed_fields: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return True

    @property
    def authority_narrowed(self) -> bool:
        return bool(self.narrowed_fields)


def _invalid_input(message: str) -> ContractValidationError:
    return ContractValidationError(ContractErrorCode.INVALID_INPUT, message)


def _capability_sequence(value: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        raise _invalid_input(f"{field_name} must be a collection of names")
    try:
        capabilities = tuple(value)
    except TypeError as exc:
        raise _invalid_input(f"{field_name} must be a collection of names") from exc

    if any(not isinstance(item, str) or not item for item in capabilities):
        raise _invalid_input(f"{field_name} contains an invalid capability name")
    if len(set(capabilities)) != len(capabilities):
        raise _invalid_input(f"{field_name} contains duplicate capability names")
    return capabilities


def negotiate_protocol(
    *,
    protocol: str,
    required_capabilities: Iterable[str],
    optional_capabilities: Iterable[str],
    supported_capabilities: Iterable[str],
) -> ProtocolNegotiationResult:
    """Negotiate the exact v1 protocol and its declared capabilities."""

    if protocol != PROTOCOL_V1:
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_PROTOCOL,
            f"unsupported factory-runner protocol: {protocol!r}",
        )

    required = _capability_sequence(
        required_capabilities,
        "required_capabilities",
    )
    optional = _capability_sequence(
        optional_capabilities,
        "optional_capabilities",
    )
    supported = _capability_sequence(
        supported_capabilities,
        "supported_capabilities",
    )

    overlap = set(required).intersection(optional)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise _invalid_input(
            f"required_capabilities and optional_capabilities overlap: {names}"
        )

    supported_names = set(supported)
    unsupported_required = tuple(
        name for name in required if name not in supported_names
    )
    if unsupported_required:
        names = ", ".join(unsupported_required)
        raise ContractValidationError(
            ContractErrorCode.UNSUPPORTED_CAPABILITY,
            f"unsupported required capabilities: {names}",
        )

    negotiated_optional = tuple(name for name in optional if name in supported_names)
    ignored_optional = tuple(name for name in optional if name not in supported_names)
    return ProtocolNegotiationResult(
        protocol=protocol,
        required_capabilities=required,
        optional_capabilities=optional,
        negotiated_capabilities=required + negotiated_optional,
        ignored_optional_capabilities=ignored_optional,
    )


def _contract_models() -> tuple[type[Any], type[Any]]:
    # Deliberately lazy: contract modules can import these pure helpers safely.
    from ai_native.factory_runner.contracts.checkpoint import Checkpoint
    from ai_native.factory_runner.contracts.run_spec import RunSpec

    return Checkpoint, RunSpec


def _validated_model(value: Any, model_type: type[Any], label: str) -> Any:
    if not isinstance(value, (BaseModel, Mapping)):
        raise ContractValidationError(
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
            f"{label} must be a {model_type.__name__} or mapping",
        )
    try:
        payload = (
            deepcopy(value.model_dump(mode="python"))
            if isinstance(value, BaseModel)
            else deepcopy(dict(value))
        )
        return model_type.model_validate(payload)
    except Exception as exc:
        raise ContractValidationError(
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
            f"{label} is not a valid {model_type.__name__}",
        ) from exc


def _checkpoint_incompatible(message: str) -> None:
    raise ContractValidationError(
        ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
        message,
    )


def _require_equal(
    checkpoint_value: Any,
    run_spec_value: Any,
    field_name: str,
) -> None:
    if checkpoint_value != run_spec_value:
        _checkpoint_incompatible(f"checkpoint {field_name} does not match run spec")


def _require_subset(
    candidate_values: Iterable[Any],
    authority_values: Iterable[Any],
    field_name: str,
) -> bool:
    candidate = set(candidate_values)
    authority = set(authority_values)
    if not candidate.issubset(authority):
        _checkpoint_incompatible(f"run spec {field_name} exceeds checkpoint authority")
    return candidate < authority


def _command_set(commands: Iterable[Iterable[str]]) -> set[tuple[str, ...]]:
    return {tuple(command) for command in commands}


def _semantic_version(value: str, field_name: str) -> tuple[int, int, int]:
    match = _SEMANTIC_VERSION_PATTERN.fullmatch(value)
    if match is None:
        _checkpoint_incompatible(f"{field_name} is not a semantic version")
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def _path_rule_covers(authority_rule: str, candidate_rule: str) -> bool:
    if authority_rule == "**":
        return True
    if candidate_rule == "**":
        return authority_rule == "**"
    if authority_rule.endswith("/**"):
        authority_root = authority_rule.removesuffix("/**")
        candidate_root = candidate_rule.removesuffix("/**")
        return candidate_root == authority_root or candidate_root.startswith(
            f"{authority_root}/"
        )
    return authority_rule == candidate_rule


def _allowed_paths_are_narrower(
    candidate_rules: Iterable[str],
    authority_rules: Iterable[str],
) -> bool:
    candidate = tuple(candidate_rules)
    authority = tuple(authority_rules)
    if any(
        not any(
            _path_rule_covers(authority_rule, candidate_rule)
            for authority_rule in authority
        )
        for candidate_rule in candidate
    ):
        _checkpoint_incompatible("run spec allowed_paths exceeds checkpoint authority")
    return set(candidate) != set(authority)


def _prohibitions_are_preserved(
    authority_rules: Iterable[str],
    candidate_rules: Iterable[str],
) -> bool:
    authority = tuple(authority_rules)
    candidate = tuple(candidate_rules)
    if any(
        not any(
            _path_rule_covers(candidate_rule, authority_rule)
            for candidate_rule in candidate
        )
        for authority_rule in authority
    ):
        _checkpoint_incompatible(
            "run spec prohibited_paths removes a checkpoint restriction"
        )
    return set(candidate) != set(authority)


def validate_checkpoint_compatibility(
    checkpoint: Any,
    run_spec: Any,
    *,
    supported_capabilities: Iterable[str],
    runner_version: str | None = None,
) -> CheckpointCompatibilityResult:
    """Validate that a later attempt resumes without widening authority."""

    checkpoint_type, run_spec_type = _contract_models()
    stored = _validated_model(checkpoint, checkpoint_type, "checkpoint")
    resumed = _validated_model(run_spec, run_spec_type, "run_spec")

    try:
        from ai_native.factory_runner.protocol import verify_contract_digest

        verify_contract_digest(stored)
    except Exception as exc:
        raise ContractValidationError(
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
            "checkpoint self digest is invalid",
        ) from exc

    if (
        stored.protocol != PROTOCOL_V1
        or stored.compatibility.protocol != PROTOCOL_V1
        or resumed.protocol != PROTOCOL_V1
    ):
        _checkpoint_incompatible(
            f"checkpoint resume requires exact protocol {PROTOCOL_V1!r}"
        )

    _require_equal(
        stored.identity.work_item_id,
        resumed.identity.work_item_id,
        "work_item_id",
    )
    _require_equal(
        stored.identity.work_item_revision_id,
        resumed.identity.work_item_revision_id,
        "work_item_revision_id",
    )
    _require_equal(
        stored.identity.delivery_phase_id,
        resumed.identity.delivery_phase_id,
        "delivery_phase_id",
    )
    _require_equal(stored.identity.run_id, resumed.identity.run_id, "run_id")
    _require_equal(
        stored.repository.repository_id,
        resumed.repository.repository_id,
        "repository_id",
    )
    _require_equal(
        stored.repository.base_commit_sha,
        resumed.repository.base_commit_sha,
        "base_commit_sha",
    )
    _require_equal(
        stored.context_bundle_digest,
        resumed.context.expected_digest,
        "context_bundle_digest",
    )
    if resumed.identity.attempt_id == stored.producer_attempt_id:
        _checkpoint_incompatible(
            "resumed attempt_id must differ from producer_attempt_id"
        )
    if resumed.resume.checkpoint_path is None:
        _checkpoint_incompatible("run spec does not reference a checkpoint")
    _require_equal(
        stored.checkpoint_digest,
        resumed.resume.expected_digest,
        "checkpoint_digest",
    )
    effective_runner_version = runner_version or __version__
    if _semantic_version(
        effective_runner_version,
        "runner_version",
    ) < _semantic_version(
        stored.compatibility.minimum_runner_version,
        "minimum_runner_version",
    ):
        _checkpoint_incompatible(
            "runner version does not satisfy checkpoint compatibility"
        )
    declared_capabilities = set(resumed.capabilities.required).union(
        resumed.capabilities.optional
    )
    if not set(stored.compatibility.required_capabilities).issubset(
        declared_capabilities
    ):
        _checkpoint_incompatible(
            "run spec does not declare a checkpoint-required capability"
        )
    try:
        negotiation = negotiate_protocol(
            protocol=resumed.protocol,
            required_capabilities=resumed.capabilities.required,
            optional_capabilities=resumed.capabilities.optional,
            supported_capabilities=supported_capabilities,
        )
    except ContractValidationError as exc:
        raise ContractValidationError(
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
            "runner capabilities do not satisfy the resumed run spec",
        ) from exc
    if not set(stored.compatibility.required_capabilities).issubset(
        negotiation.enabled_capabilities
    ):
        _checkpoint_incompatible(
            "runner does not support a checkpoint-required capability"
        )

    authority = stored.authority
    policy = resumed.policy
    narrowed_fields: list[str] = []

    set_fields = (
        "allowed_stages",
        "allowed_environment_keys",
    )
    for field_name in set_fields:
        if _require_subset(
            getattr(policy, field_name),
            getattr(authority, field_name),
            field_name,
        ):
            narrowed_fields.append(field_name)
    if _allowed_paths_are_narrower(
        policy.allowed_paths,
        authority.allowed_paths,
    ):
        narrowed_fields.append("allowed_paths")

    candidate_commands = _command_set(policy.allowed_commands)
    authority_commands = _command_set(authority.allowed_commands)
    if not candidate_commands.issubset(authority_commands):
        _checkpoint_incompatible(
            "run spec allowed_commands exceeds checkpoint authority"
        )
    if candidate_commands < authority_commands:
        narrowed_fields.append("allowed_commands")

    if _prohibitions_are_preserved(
        authority.prohibited_paths,
        policy.prohibited_paths,
    ):
        narrowed_fields.append("prohibited_paths")

    for field_name in (
        "network_profile",
        "credential_profile",
        "model_profile",
    ):
        _require_equal(
            getattr(authority, field_name),
            getattr(policy, field_name),
            field_name,
        )

    for field_name in (
        "max_wall_seconds",
        "max_agent_turns",
        "max_model_tokens",
    ):
        resumed_limit = getattr(policy, field_name)
        authority_limit = getattr(authority, field_name)
        consumed_field = {
            "max_wall_seconds": "wall_seconds",
            "max_agent_turns": "agent_turns",
            "max_model_tokens": "model_tokens",
        }[field_name]
        consumed = getattr(stored.budgets.consumed, consumed_field)
        if resumed_limit < consumed:
            _checkpoint_incompatible(
                f"run spec {field_name} is below already consumed budget"
            )
        if resumed_limit > authority_limit:
            _checkpoint_incompatible(
                f"run spec {field_name} exceeds checkpoint authority"
            )
        if resumed_limit < authority_limit:
            narrowed_fields.append(field_name)

    if (
        stored.next_permitted_stage is not None
        and stored.next_permitted_stage not in policy.allowed_stages
    ):
        _checkpoint_incompatible(
            "run spec removes the checkpoint's next permitted stage"
        )

    return CheckpointCompatibilityResult(
        protocol=PROTOCOL_V1,
        run_id=str(resumed.identity.run_id),
        producer_attempt_id=str(stored.producer_attempt_id),
        resumed_attempt_id=str(resumed.identity.attempt_id),
        narrowed_fields=tuple(narrowed_fields),
    )


__all__ = [
    "CheckpointCompatibilityResult",
    "PROTOCOL_V1",
    "ProtocolNegotiationResult",
    "negotiate_protocol",
    "validate_checkpoint_compatibility",
]

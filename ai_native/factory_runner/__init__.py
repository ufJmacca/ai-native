"""Public boundary for AI Native's non-interactive factory mode.

Exports are lazy so importing the contract-only protocol surface cannot load
legacy workflow, publication, adapter, or control-plane modules.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_POLICY_EXPORTS = frozenset(
    {
        "DEFAULT_FACTORY_MODE_CAPABILITIES",
        "ExecutionCapability",
        "ExecutionMode",
        "FactoryModeCapabilities",
    }
)
_PROTOCOL_EXPORTS = frozenset(
    {
        "ChangeSet",
        "Checkpoint",
        "CompletionManifest",
        "ContextBundle",
        "ContractErrorCode",
        "ContractValidationError",
        "ProtocolManifest",
        "RunResult",
        "RunSpec",
        "RunnerEvent",
        "VerificationEvidence",
        "canonical_json_bytes",
        "changed_file_manifest_digest",
        "contract_document_digest",
        "load_contract_schema",
        "negotiate_protocol",
        "schema_set_digest",
        "sha256_digest",
        "validate_checkpoint_compatibility",
        "validate_contract",
        "verify_contract_digest",
        "verify_digest",
    }
)

__all__ = sorted(_POLICY_EXPORTS | _PROTOCOL_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _POLICY_EXPORTS:
        module = import_module("ai_native.factory_runner.policy")
    elif name in _PROTOCOL_EXPORTS:
        module = import_module("ai_native.factory_runner.protocol")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value

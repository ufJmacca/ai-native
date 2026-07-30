"""Public boundary for AI Native's non-interactive factory mode.

The v1 contract API is dependency-light and does not import workflow,
publication, adapter, or control-plane modules. Executable runner commands
remain reserved for a later phase.
"""

from ai_native.factory_runner.policy import (
    DEFAULT_FACTORY_MODE_CAPABILITIES,
    ExecutionCapability,
    ExecutionMode,
    FactoryModeCapabilities,
)
from ai_native.factory_runner.protocol import (
    ChangeSet,
    Checkpoint,
    ContextBundle,
    ContractErrorCode,
    ContractValidationError,
    RunnerEvent,
    RunResult,
    RunSpec,
    VerificationEvidence,
    canonical_json_bytes,
    contract_document_digest,
    load_contract_schema,
    negotiate_protocol,
    schema_set_digest,
    sha256_digest,
    validate_checkpoint_compatibility,
    validate_contract,
    verify_contract_digest,
    verify_digest,
)

__all__ = [
    "ChangeSet",
    "Checkpoint",
    "ContextBundle",
    "ContractErrorCode",
    "ContractValidationError",
    "DEFAULT_FACTORY_MODE_CAPABILITIES",
    "ExecutionCapability",
    "ExecutionMode",
    "FactoryModeCapabilities",
    "RunResult",
    "RunSpec",
    "RunnerEvent",
    "VerificationEvidence",
    "canonical_json_bytes",
    "contract_document_digest",
    "load_contract_schema",
    "negotiate_protocol",
    "schema_set_digest",
    "sha256_digest",
    "validate_checkpoint_compatibility",
    "validate_contract",
    "verify_contract_digest",
    "verify_digest",
]

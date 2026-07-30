from __future__ import annotations

from enum import StrEnum


class ContractErrorCode(StrEnum):
    """Stable public failure codes for factory-runner contract validation."""

    INVALID_JSON = "invalid_json"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    DIGEST_MISMATCH = "digest_mismatch"
    CHECKPOINT_INCOMPATIBLE = "checkpoint_incompatible"
    POLICY_DENIED = "policy_denied"


class ContractValidationError(ValueError):
    """Contract failure with a protocol-stable machine-readable code."""

    def __init__(self, code: ContractErrorCode | str, message: str) -> None:
        stable_code = ContractErrorCode(code)
        self.code = stable_code.value
        self.error_code = stable_code
        self.message = message
        super().__init__(message)


__all__ = [
    "ContractErrorCode",
    "ContractValidationError",
]

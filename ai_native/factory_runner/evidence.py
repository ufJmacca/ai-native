"""Deterministic AN-03 evidence classification and construction primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ai_native.factory_runner.contracts.common import (
    RepositoryIdentity,
    RunIdentity,
    RunnerBuildIdentity,
)
from ai_native.factory_runner.contracts.verification_evidence import (
    EvidenceItem,
    FailureClassification,
    VerificationEvidence,
)
from ai_native.factory_runner.outputs import EMPTY_DIGEST
from ai_native.factory_runner.protocol import contract_document_digest


REDACTION_MARKER = b"[REDACTED]"


class EvidenceSufficiencyError(ValueError):
    """Evidence cannot support the provenance or lifecycle being claimed."""


@dataclass(frozen=True, slots=True)
class RedFailureObservation:
    exit_code: int | None
    termination_reason: str
    observed_failure: str
    intended_test: str
    failed_tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RedFailureDecision:
    accepted: bool
    failure_classification: FailureClassification


@dataclass(frozen=True, slots=True)
class RedactedOutput:
    content: bytes
    redaction_count: int


_FALSE_RED_CLASSIFICATIONS: frozenset[FailureClassification] = frozenset(
    {
        "syntax_error",
        "collection_error",
        "dependency_error",
        "credential_error",
        "infrastructure_error",
        "timeout",
        "unrelated_failure",
    }
)


def classify_red_failure(observation: RedFailureObservation) -> RedFailureDecision:
    """Accept Red only for one intended assertion that exited non-zero."""

    if observation.termination_reason == "timed_out":
        return RedFailureDecision(False, "timeout")
    if observation.termination_reason != "exited" or observation.exit_code in (None, 0):
        return RedFailureDecision(False, "infrastructure_error")

    observed = observation.observed_failure
    if observed in _FALSE_RED_CLASSIFICATIONS:
        return RedFailureDecision(False, cast(FailureClassification, observed))
    if observed != "assertion_failure" or observation.failed_tests != (
        observation.intended_test,
    ):
        return RedFailureDecision(False, "unrelated_failure")
    return RedFailureDecision(True, "expected_behavioral_failure")


def redact_output_bytes(
    content: bytes,
    *,
    secret_values: tuple[bytes, ...],
) -> RedactedOutput:
    """Replace exact secret values without decoding or normalising output."""

    redacted = bytes(content)
    redaction_count = 0
    unique_values = sorted(
        {value for value in secret_values if value},
        key=lambda value: (-len(value), value),
    )
    for value in unique_values:
        occurrences = redacted.count(value)
        if occurrences:
            redacted = redacted.replace(value, REDACTION_MARKER)
            redaction_count += occurrences
    return RedactedOutput(content=redacted, redaction_count=redaction_count)


def _missing_required_phase(items: tuple[EvidenceItem, ...]) -> str | None:
    phases = tuple(item.phase for item in items)
    required = ("red", "green", "refactor", "verification")
    for phase in required:
        if phase not in phases:
            return phase
    positions = {phase: index for index, phase in enumerate(required)}
    if any(
        positions[current] > positions[following]
        for current, following in zip(phases, phases[1:], strict=False)
    ):
        return "ordered red, green, refactor, verification"
    return None


def build_authoring_evidence(
    *,
    created_at: str,
    identity: RunIdentity,
    repository: RepositoryIdentity,
    runner: RunnerBuildIdentity,
    context_digest: str,
    items: tuple[EvidenceItem, ...],
    advisory_observations: tuple[str, ...],
    claimed_environment_kind: str = "authoring",
) -> VerificationEvidence:
    """Build self-digested evidence only for a complete author TDD lifecycle."""

    if claimed_environment_kind != "authoring":
        raise EvidenceSufficiencyError(
            "authoring evidence cannot claim clean-verification provenance"
        )
    missing = _missing_required_phase(items)
    if missing is not None:
        raise EvidenceSufficiencyError(
            f"authoring evidence is missing required {missing} phase"
        )
    payload = {
        "protocol": "factory-runner-protocol/v1",
        "schema": "verification-evidence/v1",
        "schema_version": 1,
        "created_at": created_at,
        "identity": identity.model_dump(mode="json"),
        "repository": repository.model_dump(mode="json"),
        "environment_kind": "authoring",
        "runner": runner.model_dump(mode="json"),
        "context_digest": context_digest,
        "change_set_digest": None,
        "items": [item.model_dump(mode="json") for item in items],
        "overall_status": "passed",
        "advisory_observations": list(advisory_observations),
        "evidence_set_digest": EMPTY_DIGEST,
    }
    payload["evidence_set_digest"] = contract_document_digest(payload)
    return VerificationEvidence.model_validate(payload)


__all__ = [
    "EvidenceSufficiencyError",
    "REDACTION_MARKER",
    "RedFailureDecision",
    "RedFailureObservation",
    "RedactedOutput",
    "build_authoring_evidence",
    "classify_red_failure",
    "redact_output_bytes",
]

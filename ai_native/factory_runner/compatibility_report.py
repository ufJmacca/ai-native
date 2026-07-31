"""Deterministic cross-artifact compatibility certification contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, StrictStr, model_validator

from ai_native.factory_runner.build_identity import FactoryRunnerBuildIdentity
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.common import (
    GitCommitSha,
    Sha256Digest,
    StrictContractModel,
    UtcTimestamp,
)
from ai_native.factory_runner.protocol import decode_json_document


COMPATIBILITY_REPORT_SCHEMA = "factory-runner-compatibility-report/v1"
COMPATIBILITY_SUITE_VERSION = "factory-runner-compatibility/v1"
CERTIFIED_ARTIFACT_ORDER = ("source", "wheel", "oci")
CERTIFIED_FIXTURE_ORDER = (
    "author-success",
    "author-no-change",
    "verify-success",
)

ArtifactKind = Literal["source", "wheel", "oci"]
FixtureId = Literal["author-success", "author-no-change", "verify-success"]
Operation = Literal["author", "verify"]
SuccessfulOutcome = Literal["succeeded", "no_change"]


class CertifiedArtifact(StrictContractModel):
    """One executable form certified by the compatibility suite."""

    kind: ArtifactKind
    reference: StrictStr = Field(min_length=1, max_length=1024)
    digest: Sha256Digest | None
    build_identity: FactoryRunnerBuildIdentity

    @model_validator(mode="after")
    def validate_artifact_reference(self) -> CertifiedArtifact:
        identity = self.build_identity
        if identity.source_commit is None:
            raise ValueError("certified artifact build identity requires source_commit")

        if self.kind == "source":
            expected = f"{identity.source_repository}@{identity.source_commit}"
            if self.reference != expected or self.digest is not None:
                raise ValueError(
                    "source artifact reference must equal repository@commit "
                    "and its digest must be null"
                )
            if identity.image is not None:
                raise ValueError("source artifact build identity image must be null")
        elif self.kind == "wheel":
            expected = (
                f"{identity.distribution.replace('-', '_')}-"
                f"{identity.version}-py3-none-any.whl"
            )
            if self.reference != expected or self.digest is None:
                raise ValueError(
                    "wheel artifact requires its exact filename and digest"
                )
            if identity.image is not None:
                raise ValueError("wheel artifact build identity image must be null")
        else:
            if (
                self.digest is None
                or self.reference
                != "ghcr.io/ufjmacca/ai-native-factory-runner@" + self.digest
                or identity.image != self.reference
            ):
                raise ValueError(
                    "OCI artifact requires one matching digest-pinned "
                    "reference and build identity"
                )
        return self


class ArtifactFixtureResult(StrictContractModel):
    """Observed terminal identity for one fixture and executable form."""

    artifact: ArtifactKind
    status: Literal["passed"]
    actual_outcome: SuccessfulOutcome
    run_result_digest: Sha256Digest
    output_manifest_digest: Sha256Digest
    output_tree_digest: Sha256Digest


class FixtureCertification(StrictContractModel):
    """Cross-artifact equivalence proof for one mandatory fixture."""

    fixture_id: FixtureId
    operation: Operation
    expected_outcome: SuccessfulOutcome
    status: Literal["passed"]
    canonical_output_tree_digest: Sha256Digest
    results: tuple[ArtifactFixtureResult, ...] = Field(
        min_length=3,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_fixture_equivalence(self) -> FixtureCertification:
        expected_operation_and_outcome = {
            "author-success": ("author", "succeeded"),
            "author-no-change": ("author", "no_change"),
            "verify-success": ("verify", "succeeded"),
        }[self.fixture_id]
        if (
            self.operation,
            self.expected_outcome,
        ) != expected_operation_and_outcome:
            raise ValueError(
                "fixture operation and expected outcome do not match "
                "the compatibility suite"
            )

        artifact_order = tuple(result.artifact for result in self.results)
        if artifact_order != CERTIFIED_ARTIFACT_ORDER:
            raise ValueError("fixture results must be ordered source, wheel, oci")
        if any(
            result.actual_outcome != self.expected_outcome for result in self.results
        ):
            raise ValueError(
                "every artifact result must have the fixture's expected outcome"
            )

        run_result_digests = {result.run_result_digest for result in self.results}
        output_manifest_digests = {
            result.output_manifest_digest for result in self.results
        }
        output_tree_digests = {result.output_tree_digest for result in self.results}
        if (
            len(run_result_digests) != 1
            or len(output_manifest_digests) != 1
            or output_tree_digests != {self.canonical_output_tree_digest}
        ):
            raise ValueError(
                "source, wheel, and OCI fixture outputs must be equivalent"
            )
        return self


def _shared_identity_projection(
    identity: FactoryRunnerBuildIdentity,
) -> tuple[str, ...]:
    return (
        identity.distribution,
        identity.version,
        identity.source_repository,
        identity.source_commit or "",
        identity.source_tag or "",
        identity.schema_set_digest,
        identity.schema_manifest_sha256,
    )


class FactoryRunnerCompatibilityReport(StrictContractModel):
    """Complete, passed-only compatibility certificate for one runner build."""

    schema_: Literal["factory-runner-compatibility-report/v1"] = Field(alias="schema")
    protocol: Literal["factory-runner-protocol/v1"]
    suite_version: Literal["factory-runner-compatibility/v1"]
    generated_at: UtcTimestamp
    source_commit: GitCommitSha
    schema_set_digest: Sha256Digest
    schema_manifest_sha256: Sha256Digest
    artifacts: tuple[CertifiedArtifact, ...] = Field(
        min_length=3,
        max_length=3,
    )
    fixtures: tuple[FixtureCertification, ...] = Field(
        min_length=3,
        max_length=3,
    )
    status: Literal["passed"]
    report_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_complete_certification(
        self,
    ) -> FactoryRunnerCompatibilityReport:
        artifact_order = tuple(artifact.kind for artifact in self.artifacts)
        if artifact_order != CERTIFIED_ARTIFACT_ORDER:
            raise ValueError("certified artifacts must be ordered source, wheel, oci")
        fixture_order = tuple(fixture.fixture_id for fixture in self.fixtures)
        if fixture_order != CERTIFIED_FIXTURE_ORDER:
            raise ValueError("report must contain every mandatory fixture in order")

        identity_projections = {
            _shared_identity_projection(artifact.build_identity)
            for artifact in self.artifacts
        }
        if len(identity_projections) != 1:
            raise ValueError(
                "all certified artifacts must share one shared build identity"
            )
        identity = self.artifacts[0].build_identity
        if (
            identity.source_commit != self.source_commit
            or identity.schema_set_digest != self.schema_set_digest
            or identity.schema_manifest_sha256 != self.schema_manifest_sha256
        ):
            raise ValueError(
                "report source and schema identity must match every artifact"
            )
        if self.report_digest != compatibility_report_digest(self):
            raise ValueError(
                "report_digest does not match the canonical report projection"
            )
        return self


def compatibility_report_digest(
    value: FactoryRunnerCompatibilityReport | Mapping[str, Any],
) -> str:
    """Digest the canonical report projection without its self-digest."""

    if isinstance(value, FactoryRunnerCompatibilityReport):
        payload = value.model_dump(mode="json", by_alias=True)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("compatibility report digest requires a model or mapping")
    payload.pop("report_digest", None)
    return sha256_digest(canonical_json_bytes(payload))


def validate_compatibility_report(
    value: (
        FactoryRunnerCompatibilityReport | Mapping[str, Any] | str | bytes | bytearray
    ),
) -> FactoryRunnerCompatibilityReport:
    """Decode and fully validate one compatibility certificate."""

    if isinstance(value, FactoryRunnerCompatibilityReport):
        payload = value.model_dump(mode="json", by_alias=True)
    elif isinstance(value, Mapping):
        payload = dict(value)
    elif isinstance(value, (str, bytes, bytearray)):
        payload = decode_json_document(value)
    else:
        raise TypeError(
            "compatibility report must be a model, mapping, or JSON document"
        )
    return FactoryRunnerCompatibilityReport.model_validate(payload)


def canonical_compatibility_report_bytes(
    value: (
        FactoryRunnerCompatibilityReport | Mapping[str, Any] | str | bytes | bytearray
    ),
) -> bytes:
    """Return the one RFC 8785 representation of a validated report."""

    report = validate_compatibility_report(value)
    return canonical_json_bytes(report.model_dump(mode="json", by_alias=True))


__all__ = [
    "CERTIFIED_ARTIFACT_ORDER",
    "CERTIFIED_FIXTURE_ORDER",
    "COMPATIBILITY_REPORT_SCHEMA",
    "COMPATIBILITY_SUITE_VERSION",
    "ArtifactFixtureResult",
    "CertifiedArtifact",
    "FactoryRunnerCompatibilityReport",
    "FixtureCertification",
    "canonical_compatibility_report_bytes",
    "compatibility_report_digest",
    "validate_compatibility_report",
]

"""Strict factory-runner release receipt contract and structural validator."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictStr, field_validator, model_validator

from ai_native.factory_runner.contracts.common import (
    GitCommitSha,
    SemanticVersion,
    Sha256Digest,
    StrictContractModel,
    UtcTimestamp,
)
from ai_native.factory_runner.protocol import decode_json_document


RECEIPT_SCHEMA = "factory-runner-release-receipt/v1"
COMPATIBILITY_SUITE = "factory-runner-compatibility/v1"
_PLATFORM_PATTERN = re.compile(r"^linux/[a-z0-9][a-z0-9._-]{1,31}$")
_ATTESTATION_PATTERN = re.compile(
    r"^https://github\.com/ufJmacca/ai-native/attestations/[0-9a-f]{64}$"
)


def _validate_https_url(value: str, field_name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{field_name} must be an HTTPS URL without credentials, "
            "query parameters, or fragments"
        )
    return value


def _contains_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return (
        "<" in value
        or ">" in value
        or "${" in value
        or "placeholder" in lowered
        or "todo" in lowered
    )


class ReleaseSource(StrictContractModel):
    repository: Literal["ufJmacca/ai-native"]
    git_commit_sha: GitCommitSha
    git_tag: StrictStr = Field(min_length=1, max_length=256)


class ReleaseWheel(StrictContractModel):
    distribution: Literal["ai-native-base"]
    version: SemanticVersion
    filename: StrictStr = Field(min_length=1, max_length=256)
    sha256: Sha256Digest
    download_url: StrictStr = Field(min_length=1, max_length=2048)

    @field_validator("download_url")
    @classmethod
    def validate_download_url(cls, value: str) -> str:
        return _validate_https_url(value, "wheel.download_url")

    @model_validator(mode="after")
    def validate_filename(self) -> ReleaseWheel:
        expected = (
            f"{self.distribution.replace('-', '_')}-{self.version}-py3-none-any.whl"
        )
        if self.filename != expected:
            raise ValueError("wheel filename must match the distribution and version")
        return self


class ReleaseOciImage(StrictContractModel):
    repository: Literal["ghcr.io/ufjmacca/ai-native-factory-runner"]
    digest: Sha256Digest
    pinned_reference: StrictStr = Field(min_length=1, max_length=1024)
    platforms: tuple[StrictStr, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_immutable_reference(self) -> ReleaseOciImage:
        expected = f"{self.repository}@{self.digest}"
        if self.pinned_reference != expected:
            raise ValueError("OCI pinned_reference must equal repository@digest")
        if tuple(sorted(set(self.platforms))) != self.platforms:
            raise ValueError("OCI platforms must be sorted and unique")
        if any(_PLATFORM_PATTERN.fullmatch(item) is None for item in self.platforms):
            raise ValueError("OCI platforms contain an invalid platform")
        return self


class ReleaseContracts(StrictContractModel):
    schema_set_digest: Sha256Digest
    schema_manifest_sha256: Sha256Digest


class ReleaseCompatibility(StrictContractModel):
    suite_version: Literal["factory-runner-compatibility/v1"]
    status: Literal["passed"]
    report_url: StrictStr = Field(min_length=1, max_length=2048)
    report_sha256: Sha256Digest

    @field_validator("report_url")
    @classmethod
    def validate_report_url(cls, value: str) -> str:
        return _validate_https_url(value, "compatibility.report_url")


class VulnerabilityScan(StrictContractModel):
    scanner: StrictStr = Field(min_length=1, max_length=128)
    policy: StrictStr = Field(min_length=1, max_length=256)
    status: Literal["passed"]
    report_url: StrictStr = Field(min_length=1, max_length=2048)
    report_sha256: Sha256Digest

    @field_validator("report_url")
    @classmethod
    def validate_report_url(cls, value: str) -> str:
        return _validate_https_url(value, "vulnerability_scan.report_url")


class ReleaseSupplyChain(StrictContractModel):
    sbom_url: StrictStr = Field(min_length=1, max_length=2048)
    sbom_sha256: Sha256Digest
    vulnerability_scan: VulnerabilityScan
    provenance_url: StrictStr = Field(min_length=1, max_length=2048)
    provenance_sha256: Sha256Digest
    signature_reference: StrictStr = Field(min_length=1, max_length=2048)

    @field_validator("sbom_url", "provenance_url")
    @classmethod
    def validate_evidence_url(cls, value: str) -> str:
        return _validate_https_url(value, "supply-chain evidence URL")

    @field_validator("signature_reference")
    @classmethod
    def validate_signature_reference(cls, value: str) -> str:
        if _ATTESTATION_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "signature_reference must identify an immutable GitHub attestation"
            )
        return value


class FactoryRunnerReleaseReceipt(StrictContractModel):
    receipt_schema: Literal["factory-runner-release-receipt/v1"]
    protocol: Literal["factory-runner-protocol/v1"]
    released_at: UtcTimestamp
    source: ReleaseSource
    wheel: ReleaseWheel
    oci_image: ReleaseOciImage
    contracts: ReleaseContracts
    compatibility: ReleaseCompatibility
    supply_chain: ReleaseSupplyChain

    @model_validator(mode="after")
    def validate_cross_artifact_identity(self) -> FactoryRunnerReleaseReceipt:
        expected_tag = f"{self.wheel.distribution}-v{self.wheel.version}"
        if self.source.git_tag != expected_tag:
            raise ValueError("source git tag must match the released wheel version")

        release_root = (
            "https://github.com/ufJmacca/ai-native/releases/download/"
            f"{self.source.git_tag}/"
        )
        if self.wheel.download_url != release_root + self.wheel.filename:
            raise ValueError(
                "wheel download_url must bind the source repository, tag, and filename"
            )
        for field_name, url in (
            ("compatibility.report_url", self.compatibility.report_url),
            ("supply_chain.sbom_url", self.supply_chain.sbom_url),
            (
                "vulnerability_scan.report_url",
                self.supply_chain.vulnerability_scan.report_url,
            ),
            ("supply_chain.provenance_url", self.supply_chain.provenance_url),
        ):
            if not url.startswith(release_root):
                raise ValueError(
                    f"{field_name} must bind the source repository and tag"
                )
            if PurePosixPath(urlsplit(url).path).name == "":
                raise ValueError(f"{field_name} must name a release artifact")

        payload = self.model_dump(mode="json")
        pending: list[Any] = [payload]
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, Mapping):
                pending.extend(candidate.values())
            elif isinstance(candidate, list):
                pending.extend(candidate)
            elif isinstance(candidate, str) and _contains_placeholder(candidate):
                raise ValueError("release receipt contains a placeholder value")
        return self


def validate_release_receipt(
    value: (FactoryRunnerReleaseReceipt | Mapping[str, Any] | str | bytes | bytearray),
) -> FactoryRunnerReleaseReceipt:
    """Decode and structurally validate one strict release receipt."""

    if isinstance(value, FactoryRunnerReleaseReceipt):
        return FactoryRunnerReleaseReceipt.model_validate(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        payload = dict(value)
    elif isinstance(value, (str, bytes, bytearray)):
        payload = decode_json_document(value)
    else:
        raise TypeError("release receipt must be a model, mapping, or JSON document")
    return FactoryRunnerReleaseReceipt.model_validate(payload)


__all__ = [
    "COMPATIBILITY_SUITE",
    "RECEIPT_SCHEMA",
    "FactoryRunnerReleaseReceipt",
    "ReleaseCompatibility",
    "ReleaseContracts",
    "ReleaseOciImage",
    "ReleaseSource",
    "ReleaseSupplyChain",
    "ReleaseWheel",
    "VulnerabilityScan",
    "validate_release_receipt",
]

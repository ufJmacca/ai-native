"""Immutable factory-runner build identity shared by release artifacts."""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, StrictStr, model_validator

from ai_native import __version__
from ai_native.factory_runner.contracts.common import (
    GitCommitSha,
    SemanticVersion,
    Sha256Digest,
    StrictContractModel,
)
from ai_native.factory_runner.protocol import (
    decode_json_document,
    schema_manifest_digest,
    schema_set_digest,
)


BUILD_IDENTITY_SCHEMA = "factory-runner-build-identity/v1"
BUILD_IDENTITY_RESOURCE = "_build_identity.json"
FACTORY_RUNNER_DISTRIBUTION = "ai-native-base"
FACTORY_RUNNER_SOURCE_REPOSITORY = "ufJmacca/ai-native"
_PINNED_IMAGE_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)


class FactoryRunnerBuildIdentity(StrictContractModel):
    """Identity embedded in a wheel and inherited by its OCI image."""

    schema_: Literal["factory-runner-build-identity/v1"] = Field(alias="schema")
    distribution: Literal["ai-native-base"]
    version: SemanticVersion
    source_repository: Literal["ufJmacca/ai-native"]
    source_commit: GitCommitSha | None
    source_tag: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    image: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    schema_set_digest: Sha256Digest
    schema_manifest_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_release_binding(self) -> FactoryRunnerBuildIdentity:
        if self.source_tag is not None:
            expected_tag = f"{self.distribution}-v{self.version}"
            if self.source_tag != expected_tag:
                raise ValueError(
                    "source_tag must equal the actual distribution version tag"
                )
            if self.source_commit is None:
                raise ValueError("source_tag requires source_commit")
        if (
            self.image is not None
            and _PINNED_IMAGE_PATTERN.fullmatch(self.image) is None
        ):
            raise ValueError(
                "image must be null or an immutable digest-pinned reference; "
                "mutable image references are forbidden"
            )
        return self

    def runner_identity(self):
        """Return the v1 wire identity used by results and evidence."""

        from ai_native.factory_runner.contracts.common import RunnerBuildIdentity

        return RunnerBuildIdentity(
            version=self.version,
            image=self.image,
            source_commit=self.source_commit,
        )


def _source_identity() -> FactoryRunnerBuildIdentity:
    return FactoryRunnerBuildIdentity(
        schema=BUILD_IDENTITY_SCHEMA,
        distribution=FACTORY_RUNNER_DISTRIBUTION,
        version=__version__,
        source_repository=FACTORY_RUNNER_SOURCE_REPOSITORY,
        source_commit=None,
        source_tag=None,
        image=None,
        schema_set_digest=schema_set_digest(),
        schema_manifest_sha256=schema_manifest_digest(),
    )


def _packaged_identity_bytes() -> bytes | None:
    resource = resources.files("ai_native.factory_runner").joinpath(
        BUILD_IDENTITY_RESOURCE
    )
    try:
        return resource.read_bytes()
    except FileNotFoundError:
        return None


def load_build_identity(
    path: Path | str | None = None,
) -> FactoryRunnerBuildIdentity:
    """Load and cross-check an explicit or wheel-embedded build identity."""

    if path is None:
        encoded = _packaged_identity_bytes()
        if encoded is None:
            return _source_identity()
    else:
        encoded = Path(path).read_bytes()

    payload = decode_json_document(encoded)
    identity = FactoryRunnerBuildIdentity.model_validate(payload)
    if identity.version != __version__:
        raise ValueError(
            "build identity version does not match the installed distribution"
        )
    if identity.schema_set_digest != schema_set_digest():
        raise ValueError(
            "build identity schema-set digest does not match packaged schemas"
        )
    if identity.schema_manifest_sha256 != schema_manifest_digest():
        raise ValueError(
            "build identity schema-manifest digest does not match packaged schemas"
        )
    return identity


def canonical_build_identity_bytes(
    identity: FactoryRunnerBuildIdentity,
) -> bytes:
    """Encode build identity deterministically for wheel inclusion."""

    return (
        json.dumps(
            identity.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


__all__ = [
    "BUILD_IDENTITY_RESOURCE",
    "BUILD_IDENTITY_SCHEMA",
    "FACTORY_RUNNER_DISTRIBUTION",
    "FACTORY_RUNNER_SOURCE_REPOSITORY",
    "FactoryRunnerBuildIdentity",
    "canonical_build_identity_bytes",
    "load_build_identity",
]

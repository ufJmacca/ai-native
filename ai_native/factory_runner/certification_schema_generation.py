"""Separate schema-set generation for factory-runner release certification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel

from ai_native.factory_runner.build_identity import (
    BUILD_IDENTITY_SCHEMA,
    FactoryRunnerBuildIdentity,
)
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.compatibility_report import (
    COMPATIBILITY_REPORT_SCHEMA,
    FactoryRunnerCompatibilityReport,
)
from ai_native.factory_runner.release_receipt import (
    RECEIPT_SCHEMA,
    FactoryRunnerReleaseReceipt,
)
from ai_native.factory_runner.schema_registry import JSON_SCHEMA_DRAFT


CERTIFICATION_SCHEMA_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "factory_runner"
    / "certification"
    / "v1"
)
CERTIFICATION_MANIFEST_SCHEMA = "factory-runner-certification-schema-manifest/v1"
MANIFEST_FILENAME = "schema-manifest.json"
SCHEMA_SET_DIGEST_FILENAME = "schema-set.sha256"
CANONICALIZATION = "RFC 8785"


@dataclass(frozen=True, slots=True)
class CertificationSchema:
    schema: str
    filename: str
    schema_id: str
    model: type[BaseModel]


CERTIFICATION_SCHEMAS = (
    CertificationSchema(
        schema=BUILD_IDENTITY_SCHEMA,
        filename="build-identity.schema.json",
        schema_id=("urn:ai-native:factory-runner:certification:v1:build-identity"),
        model=FactoryRunnerBuildIdentity,
    ),
    CertificationSchema(
        schema=COMPATIBILITY_REPORT_SCHEMA,
        filename="compatibility-report.schema.json",
        schema_id=(
            "urn:ai-native:factory-runner:certification:v1:compatibility-report"
        ),
        model=FactoryRunnerCompatibilityReport,
    ),
    CertificationSchema(
        schema=RECEIPT_SCHEMA,
        filename="release-receipt.schema.json",
        schema_id=("urn:ai-native:factory-runner:certification:v1:release-receipt"),
        model=FactoryRunnerReleaseReceipt,
    ),
)

if tuple(entry.filename for entry in CERTIFICATION_SCHEMAS) != tuple(
    sorted(entry.filename for entry in CERTIFICATION_SCHEMAS)
):
    raise RuntimeError("certification schema registry must be ordered by filename")


def _pretty_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def render_certification_schema_artifacts() -> dict[str, bytes]:
    """Render the certification-only schemas and their canonical manifest."""

    rendered: dict[str, bytes] = {}
    manifest_entries: list[dict[str, str]] = []
    for entry in CERTIFICATION_SCHEMAS:
        schema = entry.model.model_json_schema(
            by_alias=True,
            mode="validation",
        )
        schema["$id"] = entry.schema_id
        schema["$schema"] = JSON_SCHEMA_DRAFT
        rendered[entry.filename] = _pretty_json_bytes(schema)
        manifest_entries.append(
            {
                "digest": sha256_digest(canonical_json_bytes(schema)),
                "path": entry.filename,
                "schema": entry.schema,
                "schema_id": entry.schema_id,
            }
        )

    manifest = {
        "canonicalization": CANONICALIZATION,
        "json_schema_draft": JSON_SCHEMA_DRAFT,
        "manifest_schema": CERTIFICATION_MANIFEST_SCHEMA,
        "schemas": manifest_entries,
    }
    rendered[MANIFEST_FILENAME] = _pretty_json_bytes(manifest)
    rendered[SCHEMA_SET_DIGEST_FILENAME] = (
        sha256_digest(canonical_json_bytes(manifest)) + "\n"
    ).encode("ascii")
    return dict(sorted(rendered.items()))


def write_certification_schema_artifacts(output_dir: Path) -> None:
    """Write the complete generated certification schema set."""

    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in render_certification_schema_artifacts().items():
        (output_dir / relative_path).write_bytes(content)


def certification_schema_artifact_drift(
    output_dir: Path,
    *,
    expected: Mapping[str, bytes] | None = None,
) -> tuple[str, ...]:
    """Return exact missing, unexpected, and changed certification artifacts."""

    wanted = dict(
        expected if expected is not None else render_certification_schema_artifacts()
    )
    actual = (
        {
            path.name: path.read_bytes()
            for path in output_dir.iterdir()
            if path.is_file()
        }
        if output_dir.is_dir()
        else {}
    )

    differences: list[str] = []
    for missing in sorted(wanted.keys() - actual.keys()):
        differences.append(f"missing: {missing}")
    for unexpected in sorted(actual.keys() - wanted.keys()):
        differences.append(f"unexpected: {unexpected}")
    for changed in sorted(wanted.keys() & actual.keys()):
        if wanted[changed] != actual[changed]:
            differences.append(f"changed: {changed}")
    return tuple(differences)


__all__ = [
    "CANONICALIZATION",
    "CERTIFICATION_MANIFEST_SCHEMA",
    "CERTIFICATION_SCHEMA_DIRECTORY",
    "CERTIFICATION_SCHEMAS",
    "MANIFEST_FILENAME",
    "SCHEMA_SET_DIGEST_FILENAME",
    "CertificationSchema",
    "certification_schema_artifact_drift",
    "render_certification_schema_artifacts",
    "write_certification_schema_artifacts",
]

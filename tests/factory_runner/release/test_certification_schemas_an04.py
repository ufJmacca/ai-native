from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.certification_schema_generation import (
    CERTIFICATION_SCHEMA_DIRECTORY,
    CERTIFICATION_SCHEMAS,
    render_certification_schema_artifacts,
    certification_schema_artifact_drift,
    write_certification_schema_artifacts,
)


EXPECTED_SCHEMA_FILENAMES = (
    "build-identity.schema.json",
    "compatibility-report.schema.json",
    "release-receipt.schema.json",
)
EXPECTED_ARTIFACT_FILENAMES = (
    *EXPECTED_SCHEMA_FILENAMES,
    "schema-manifest.json",
    "schema-set.sha256",
)


def test_certification_schema_generation_is_separate_exact_and_deterministic(
    tmp_path: Path,
) -> None:
    first = render_certification_schema_artifacts()
    second = render_certification_schema_artifacts()

    assert first == second
    assert tuple(first) == EXPECTED_ARTIFACT_FILENAMES
    assert tuple(entry.filename for entry in CERTIFICATION_SCHEMAS) == (
        EXPECTED_SCHEMA_FILENAMES
    )
    assert (
        certification_schema_artifact_drift(
            CERTIFICATION_SCHEMA_DIRECTORY,
            expected=first,
        )
        == ()
    )

    generated = tmp_path / "certification"
    write_certification_schema_artifacts(generated)
    assert {
        path.name: path.read_bytes() for path in sorted(generated.iterdir())
    } == first


def test_certification_schemas_are_valid_draft_2020_12() -> None:
    for filename in EXPECTED_SCHEMA_FILENAMES:
        schema = json.loads(
            (CERTIFICATION_SCHEMA_DIRECTORY / filename).read_text(encoding="utf-8")
        )
        assert schema["$schema"] == Draft202012Validator.META_SCHEMA["$id"]
        Draft202012Validator.check_schema(schema)


def test_certification_manifest_binds_every_schema_and_its_set_digest() -> None:
    manifest_bytes = (
        CERTIFICATION_SCHEMA_DIRECTORY / "schema-manifest.json"
    ).read_bytes()
    manifest = json.loads(manifest_bytes)

    assert manifest == {
        "canonicalization": "RFC 8785",
        "json_schema_draft": Draft202012Validator.META_SCHEMA["$id"],
        "manifest_schema": "factory-runner-certification-schema-manifest/v1",
        "schemas": [
            {
                "digest": sha256_digest(
                    canonical_json_bytes(
                        json.loads(
                            (
                                CERTIFICATION_SCHEMA_DIRECTORY / entry.filename
                            ).read_bytes()
                        )
                    )
                ),
                "path": entry.filename,
                "schema": entry.schema,
                "schema_id": entry.schema_id,
            }
            for entry in CERTIFICATION_SCHEMAS
        ],
    }
    assert CERTIFICATION_SCHEMA_DIRECTORY.joinpath("schema-set.sha256").read_text(
        encoding="ascii"
    ).strip() == sha256_digest(canonical_json_bytes(manifest))

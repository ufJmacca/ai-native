from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.schema_registry import (
    CONTRACT_SCHEMAS,
    JSON_SCHEMA_DRAFT,
)


PROTOCOL = "factory-runner-protocol/v1"
MANIFEST_FILENAME = "schema-manifest.json"
SCHEMA_SET_DIGEST_FILENAME = "schema-set.sha256"
CANONICALIZATION = "RFC 8785"


def pretty_json_bytes(value: object) -> bytes:
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


def render_schema_artifacts() -> dict[str, bytes]:
    rendered: dict[str, bytes] = {}
    manifest_entries: list[dict[str, str]] = []

    for entry in CONTRACT_SCHEMAS:
        schema = entry.model.model_json_schema(mode="validation")
        schema["$id"] = entry.schema_id
        schema["$schema"] = JSON_SCHEMA_DRAFT
        rendered[entry.filename] = pretty_json_bytes(schema)
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
        "manifest_version": 1,
        "protocol": PROTOCOL,
        "schemas": manifest_entries,
    }
    rendered[MANIFEST_FILENAME] = pretty_json_bytes(manifest)
    rendered[SCHEMA_SET_DIGEST_FILENAME] = (
        sha256_digest(canonical_json_bytes(manifest)) + "\n"
    ).encode("ascii")
    return dict(sorted(rendered.items()))


def write_schema_artifacts(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in render_schema_artifacts().items():
        (output_dir / relative_path).write_bytes(content)


def schema_artifact_drift(
    output_dir: Path,
    *,
    expected: Mapping[str, bytes] | None = None,
) -> tuple[str, ...]:
    wanted = dict(expected or render_schema_artifacts())
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
    for missing in sorted(set(wanted) - set(actual)):
        differences.append(f"missing: {missing}")
    for unexpected in sorted(set(actual) - set(wanted)):
        differences.append(f"unexpected: {unexpected}")
    for changed in sorted(set(actual) & set(wanted)):
        if actual[changed] != wanted[changed]:
            differences.append(f"changed: {changed}")
    return tuple(differences)


__all__ = [
    "CANONICALIZATION",
    "MANIFEST_FILENAME",
    "PROTOCOL",
    "SCHEMA_SET_DIGEST_FILENAME",
    "pretty_json_bytes",
    "render_schema_artifacts",
    "schema_artifact_drift",
    "write_schema_artifacts",
]

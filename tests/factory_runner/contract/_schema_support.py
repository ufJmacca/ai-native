from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.factory_runner.contract._support import BUILDERS, MODEL_NAMES


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIRECTORY = REPOSITORY_ROOT / "ai_native" / "schemas" / "factory_runner" / "v1"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "factory_runner"
GOLDEN_DIRECTORY = FIXTURE_ROOT / "golden"
SCHEMA_INVALID_DIRECTORY = FIXTURE_ROOT / "schema-invalid"
SCHEMA_INVALID_MANIFEST = SCHEMA_INVALID_DIRECTORY / "manifest.json"


@dataclass(frozen=True, slots=True)
class ContractCase:
    model_name: str
    schema_name: str
    schema_filename: str
    fixture_stem: str
    builder: Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SchemaInvalidCase:
    schema_name: str
    fixture_filename: str
    validator: str


def _contract_case(model_name: str) -> ContractCase:
    builder = BUILDERS[model_name]
    schema_name = builder()["schema"]
    fixture_stem = schema_name.removesuffix("/v1")
    return ContractCase(
        model_name=model_name,
        schema_name=schema_name,
        schema_filename=f"{fixture_stem}.schema.json",
        fixture_stem=fixture_stem,
        builder=builder,
    )


CONTRACT_CASES = tuple(
    sorted(
        (_contract_case(model_name) for model_name in MODEL_NAMES),
        key=lambda case: case.schema_filename,
    )
)
EXPECTED_SCHEMA_FILENAMES = tuple(case.schema_filename for case in CONTRACT_CASES)
EXPECTED_SCHEMA_ARTIFACT_FILENAMES = tuple(
    sorted(
        (
            *EXPECTED_SCHEMA_FILENAMES,
            "schema-manifest.json",
            "schema-set.sha256",
        )
    )
)
GOLDEN_VARIANTS = ("minimal", "complete")
EXPECTED_GOLDEN_FILENAMES = tuple(
    sorted(
        f"{case.fixture_stem}.{variant}.json"
        for case in CONTRACT_CASES
        for variant in GOLDEN_VARIANTS
    )
)
EXPECTED_GOLDEN_PATHS = tuple(
    GOLDEN_DIRECTORY / filename for filename in EXPECTED_GOLDEN_FILENAMES
)
_SCHEMA_NAMES_BY_STEM = {case.fixture_stem: case.schema_name for case in CONTRACT_CASES}
SCHEMA_INVALID_CASES = (
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["change-set"],
        "change-set.invalid-file-mode.json",
        "enum",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["checkpoint"],
        "checkpoint.unknown-stage.json",
        "enum",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["completion"],
        "completion.invalid-run-result-path.json",
        "const",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["context-bundle"],
        "context-bundle.malformed-digest.json",
        "pattern",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["protocol-manifest"],
        "protocol-manifest.invalid-event-path.json",
        "const",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["run-result"],
        "run-result.unknown-outcome.json",
        "enum",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["run-spec"],
        "run-spec.unknown-field.json",
        "additionalProperties",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["runner-event"],
        "runner-event.zero-sequence.json",
        "exclusiveMinimum",
    ),
    SchemaInvalidCase(
        _SCHEMA_NAMES_BY_STEM["verification-evidence"],
        "verification-evidence.shell-command.json",
        "type",
    ),
)


def load_required_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        pytest.skip(f"blocked by intentional fixture-inventory RED: {path.name}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must contain one JSON object"
    return loaded


__all__ = [
    "CONTRACT_CASES",
    "EXPECTED_GOLDEN_FILENAMES",
    "EXPECTED_GOLDEN_PATHS",
    "EXPECTED_SCHEMA_ARTIFACT_FILENAMES",
    "EXPECTED_SCHEMA_FILENAMES",
    "GOLDEN_DIRECTORY",
    "GOLDEN_VARIANTS",
    "REPOSITORY_ROOT",
    "SCHEMA_DIRECTORY",
    "SCHEMA_INVALID_CASES",
    "SCHEMA_INVALID_DIRECTORY",
    "SCHEMA_INVALID_MANIFEST",
    "ContractCase",
    "SchemaInvalidCase",
    "load_required_json",
]

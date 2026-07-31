from __future__ import annotations

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from ai_native.factory_runner.protocol import (
    load_contract_schema,
    verify_contract_digest,
)
from tests.factory_runner.contract._schema_support import (
    CONTRACT_CASES,
    EXPECTED_GOLDEN_FILENAMES,
    EXPECTED_GOLDEN_PATHS,
    GOLDEN_DIRECTORY,
    GOLDEN_VARIANTS,
    SCHEMA_INVALID_CASES,
    SCHEMA_INVALID_MANIFEST,
    load_required_json,
)
from tests.factory_runner.contract._support import protocol_api


def test_required_golden_examples_and_invalid_manifest_exist() -> None:
    missing = [
        path.relative_to(GOLDEN_DIRECTORY.parent.parent).as_posix()
        for path in (*EXPECTED_GOLDEN_PATHS, SCHEMA_INVALID_MANIFEST)
        if not path.is_file()
    ]
    assert not missing, "missing required AN-01 fixtures:\n" + "\n".join(missing)


def test_golden_directory_contains_exactly_eighteen_examples() -> None:
    if not GOLDEN_DIRECTORY.is_dir():
        pytest.skip("blocked by intentional fixture-inventory RED")
    actual = tuple(
        sorted(path.name for path in GOLDEN_DIRECTORY.iterdir() if path.is_file())
    )
    assert len(EXPECTED_GOLDEN_FILENAMES) == 18
    assert actual == EXPECTED_GOLDEN_FILENAMES


@pytest.mark.parametrize(
    ("case", "variant"),
    [(case, variant) for case in CONTRACT_CASES for variant in GOLDEN_VARIANTS],
    ids=[
        f"{case.fixture_stem}-{variant}"
        for case in CONTRACT_CASES
        for variant in GOLDEN_VARIANTS
    ],
)
def test_golden_examples_validate_with_pydantic_and_jsonschema(
    case,
    variant: str,
) -> None:
    fixture_path = GOLDEN_DIRECTORY / f"{case.fixture_stem}.{variant}.json"
    payload = load_required_json(fixture_path)

    model = getattr(protocol_api(), case.model_name)
    model.model_validate(payload)
    if case.schema_name not in {
        "completion/v1",
        "protocol-manifest/v1",
        "run-spec/v1",
        "runner-event/v1",
    }:
        verify_contract_digest(payload)
    schema = load_contract_schema(case.schema_name)
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(payload)) == []


def test_schema_invalid_corpus_has_pydantic_jsonschema_parity() -> None:
    manifest = load_required_json(SCHEMA_INVALID_MANIFEST)
    cases = manifest.get("cases")
    assert manifest.get("manifest_version") == 1
    assert isinstance(cases, list)
    assert cases == [
        {
            "path": case.fixture_filename,
            "schema": case.schema_name,
            "validator": case.validator,
        }
        for case in SCHEMA_INVALID_CASES
    ]

    known = {case.schema_name: case for case in CONTRACT_CASES}
    for invalid_case in cases:
        assert isinstance(invalid_case, dict)
        schema_name = invalid_case["schema"]
        case = known[schema_name]
        payload = load_required_json(
            SCHEMA_INVALID_MANIFEST.parent / invalid_case["path"]
        )

        with pytest.raises(ValueError):
            getattr(protocol_api(), case.model_name).model_validate(payload)
        validator = Draft202012Validator(
            load_contract_schema(schema_name),
            format_checker=FormatChecker(),
        )
        errors = list(validator.iter_errors(payload))
        assert errors, f"{invalid_case['path']} is not independently schema-invalid"
        assert invalid_case["validator"] in {error.validator for error in errors}

    declared_paths = tuple(
        sorted(case.fixture_filename for case in SCHEMA_INVALID_CASES)
    )
    actual_paths = tuple(
        sorted(
            path.name
            for path in SCHEMA_INVALID_MANIFEST.parent.glob("*.json")
            if path.name != SCHEMA_INVALID_MANIFEST.name
        )
    )
    assert actual_paths == declared_paths

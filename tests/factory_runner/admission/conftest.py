from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.factory_runner.admission._fixtures import (
    AdmissionCase,
    SUPPORTED_ENVIRONMENT,
    make_admission_case,
)


REQUIRED_ADMISSION_API = (
    "FactoryAdmissionError",
    "ValidatedInputs",
    "admit_inputs",
    "validate_workspace",
)


def load_admission_api() -> tuple[ModuleType | None, str | None]:
    try:
        module = importlib.import_module("ai_native.factory_runner.admission")
    except ModuleNotFoundError:
        return None, "public module ai_native.factory_runner.admission is missing"
    missing = [name for name in REQUIRED_ADMISSION_API if not hasattr(module, name)]
    if missing:
        return (
            None,
            "admission API is incomplete: " + ", ".join(sorted(missing)),
        )
    return module, None


@pytest.fixture
def admission_api() -> ModuleType:
    module, error = load_admission_api()
    if module is None:
        pytest.skip(f"blocked by intended AN-02 RED: {error}")
    return module


@pytest.fixture
def admission_case(tmp_path: Path) -> AdmissionCase:
    return make_admission_case(tmp_path / "admission-case")


def admit(
    admission_api: ModuleType,
    case: AdmissionCase,
    *,
    expected_operation: str | None = None,
    output_dir: Path | None = None,
    environment: dict[str, str] | None = None,
) -> Any:
    return admission_api.admit_inputs(
        expected_operation=expected_operation or case.operation,
        run_spec_path=case.run_spec_path,
        output_dir=output_dir or case.output_dir,
        environment=(SUPPORTED_ENVIRONMENT if environment is None else environment),
    )

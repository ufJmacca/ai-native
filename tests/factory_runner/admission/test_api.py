from __future__ import annotations

from tests.factory_runner.admission.conftest import load_admission_api


def test_an02_admission_api_surface_exists() -> None:
    module, error = load_admission_api()

    assert module is not None, f"AN-02 RED: {error}"
    assert issubclass(module.FactoryAdmissionError, Exception)
    assert isinstance(module.ValidatedInputs, type)
    assert callable(module.admit_inputs)
    assert callable(module.validate_workspace)

    failure = module.FactoryAdmissionError("invalid_input")
    assert failure.reason_code == "invalid_input"

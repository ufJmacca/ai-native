from __future__ import annotations

from types import ModuleType

import pytest

from tests.factory_runner.admission._fixtures import (
    AdmissionCase,
    assert_read_only,
)
from tests.factory_runner.admission.conftest import admit


def assert_workspace_rejected(
    admission_api: ModuleType,
    case: AdmissionCase,
    reason_code: str,
) -> None:
    inputs = admit(admission_api, case)

    def reject() -> None:
        with pytest.raises(admission_api.FactoryAdmissionError) as captured:
            admission_api.validate_workspace(inputs)
        assert captured.value.reason_code == reason_code

    assert_read_only(case.root, reject)


def test_author_workspace_accepts_exact_clean_repository_and_head(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    inputs = admit(admission_api, admission_case)

    assert_read_only(
        admission_case.root,
        lambda: admission_api.validate_workspace(inputs),
    )


def test_workspace_path_must_be_exact_repository_root(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.set_workspace_path(admission_case.workspace / "src")

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


def test_workspace_path_must_be_a_git_repository(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    non_repository = admission_case.root / "not-a-repository"
    non_repository.mkdir()
    admission_case.set_workspace_path(non_repository)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


def test_author_head_must_equal_declared_base(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.set_declared_base("b" * 40)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_author_workspace_must_be_clean(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    dirty_kind: str,
) -> None:
    if dirty_kind == "tracked":
        (admission_case.workspace / "src" / "app.py").write_text(
            "dirty tracked content\n",
            encoding="utf-8",
        )
    else:
        (admission_case.workspace / "untracked.txt").write_text(
            "dirty untracked content\n",
            encoding="utf-8",
        )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_verify_accepts_a_prepared_uncommitted_change(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    inputs = admit(admission_api, admission_case)

    assert_read_only(
        admission_case.root,
        lambda: admission_api.validate_workspace(inputs),
    )


def test_verify_rejects_a_workspace_without_prepared_changes(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=False)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )

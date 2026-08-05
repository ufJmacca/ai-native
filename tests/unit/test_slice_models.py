from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_native.models import SliceDefinition, SlicePlan


def _slice(slice_id: str, *, dependencies: list[str] | None = None) -> SliceDefinition:
    return SliceDefinition(
        id=slice_id,
        name=f"Slice {slice_id}",
        goal="Deliver the slice",
        dependencies=dependencies or [],
    )


@pytest.mark.parametrize(
    "slice_id",
    ["S001", "api-v2", "api.v2", "api_v2"],
)
def test_slice_definition_preserves_portable_ids(slice_id: str) -> None:
    assert _slice(slice_id).id == slice_id


@pytest.mark.parametrize(
    "slice_id",
    [
        "",
        "../S001",
        "S001/child",
        r"S001\child",
        " S001",
        "S001 ",
        "S001.",
        "S001..child",
        "CON",
        "con.txt",
        "résumé",
        "S" * 65,
    ],
)
def test_slice_definition_rejects_non_portable_or_unbounded_ids(
    slice_id: str,
) -> None:
    with pytest.raises(ValidationError, match="portable identifier"):
        _slice(slice_id)


def test_slice_definition_applies_id_invariant_to_dependencies() -> None:
    with pytest.raises(ValidationError, match="portable identifier"):
        _slice("S002", dependencies=["../S001"])


def test_slice_plan_accepts_a_valid_dependency_dag() -> None:
    plan = SlicePlan(
        title="Feature",
        summary="A valid dependency graph",
        slices=[
            _slice("S001"),
            _slice("S002", dependencies=["S001"]),
            _slice("S003", dependencies=["S001", "S002"]),
        ],
    )

    assert [slice_def.id for slice_def in plan.slices] == ["S001", "S002", "S003"]


def test_slice_plan_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate slice id `S001`"):
        SlicePlan(
            title="Feature",
            summary="Duplicate IDs",
            slices=[_slice("S001"), _slice("S001")],
        )


def test_slice_plan_rejects_ids_that_collide_on_case_insensitive_filesystems() -> None:
    with pytest.raises(
        ValidationError,
        match="slice id `s001` conflicts with `S001` on case-insensitive filesystems",
    ):
        SlicePlan(
            title="Feature",
            summary="Non-portable ID collision",
            slices=[_slice("S001"), _slice("s001")],
        )


def test_slice_plan_rejects_unknown_dependencies() -> None:
    with pytest.raises(
        ValidationError,
        match="slice `S002` references unknown dependency `S999`",
    ):
        SlicePlan(
            title="Feature",
            summary="Unknown dependency",
            slices=[_slice("S001"), _slice("S002", dependencies=["S999"])],
        )


def test_slice_plan_rejects_duplicate_dependencies() -> None:
    with pytest.raises(
        ValidationError,
        match="slice `S002` repeats dependency `S001`",
    ):
        SlicePlan(
            title="Feature",
            summary="Duplicate dependency",
            slices=[
                _slice("S001"),
                _slice("S002", dependencies=["S001", "S001"]),
            ],
        )


def test_slice_plan_rejects_self_dependencies() -> None:
    with pytest.raises(
        ValidationError,
        match="slice `S001` cannot depend on itself",
    ):
        SlicePlan(
            title="Feature",
            summary="Self dependency",
            slices=[_slice("S001", dependencies=["S001"])],
        )


def test_slice_plan_rejects_dependency_cycles() -> None:
    with pytest.raises(
        ValidationError,
        match=r"slice dependency cycle detected: S001 -> S002 -> S003 -> S001",
    ):
        SlicePlan(
            title="Feature",
            summary="Dependency cycle",
            slices=[
                _slice("S001", dependencies=["S002"]),
                _slice("S002", dependencies=["S003"]),
                _slice("S003", dependencies=["S001"]),
            ],
        )

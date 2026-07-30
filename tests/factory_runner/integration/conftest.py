from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from tests.factory_runner.integration._support import (
    FactoryInvocation,
    build_invocation,
)


@pytest.fixture()
def factory_invocation(
    tmp_path: Path,
) -> Callable[..., FactoryInvocation]:
    sequence = 0

    def create(
        *,
        operation: Literal["author", "verify"],
        acceptance_criteria: list[str] | None = None,
        verification_passes: bool = True,
    ) -> FactoryInvocation:
        nonlocal sequence
        sequence += 1
        return build_invocation(
            tmp_path / f"invocation-{sequence}",
            operation=operation,
            acceptance_criteria=acceptance_criteria,
            verification_passes=verification_passes,
        )

    return create

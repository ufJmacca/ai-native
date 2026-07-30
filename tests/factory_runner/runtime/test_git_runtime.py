from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.factory_runner.git_runtime import (
    FactoryGitCancelled,
    FactoryGitRuntime,
    FactoryGitTimedOut,
)
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessRunner,
)


def _runtime(
    tmp_path: Path,
    *,
    cancellation_token: CancellationToken,
    deadline: Deadline,
) -> FactoryGitRuntime:
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()
    return FactoryGitRuntime(
        workspace=workspace,
        output_dir=output_dir,
        environment={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        process_runner=FactoryProcessRunner(
            cancellation_token=cancellation_token,
            deadline=deadline,
        ),
        deadline=deadline,
    )


def test_runner_owned_git_observes_cancellation_before_spawning(
    tmp_path: Path,
) -> None:
    cancellation_token = CancellationToken()
    cancellation_token.cancel()
    runtime = _runtime(
        tmp_path,
        cancellation_token=cancellation_token,
        deadline=Deadline.from_timeout(30),
    )

    with pytest.raises(FactoryGitCancelled):
        runtime.run("status", "--porcelain=v1")


def test_runner_owned_git_observes_shared_deadline_before_spawning(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(0),
    )

    with pytest.raises(FactoryGitTimedOut):
        runtime.run("status", "--porcelain=v1")

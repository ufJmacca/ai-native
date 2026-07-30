from __future__ import annotations

from collections.abc import Callable

from tests.factory_runner.integration._support import (
    AUTHORED_APP,
    FactoryInvocation,
    git_output,
    git_status,
    invoke_factory,
    load_valid_result,
)


def test_factory_author_runs_unattended_without_committing(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert invocation.marker_path.exists()
    assert (invocation.workspace / "app.py").read_text(encoding="utf-8") == AUTHORED_APP
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == " M app.py"

    result = load_valid_result(invocation)
    assert result.operation == "author"
    assert result.outcome == "succeeded"
    assert result.identity is not None
    assert result.identity.attempt_id == "attempt-an-02"
    assert result.repository is not None
    assert result.repository.base_commit_sha == invocation.base_sha
    assert result.change_set is not None
    assert result.verification_evidence is None


def test_factory_author_blocks_missing_acceptance_criteria_without_prompting(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(
        operation="author",
        acceptance_criteria=[],
    )

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 4, completed.stderr
    assert completed.stdout == ""
    assert not invocation.marker_path.exists()
    assert git_output(invocation.workspace, "rev-parse", "HEAD") == invocation.base_sha
    assert git_status(invocation.workspace) == ""

    result = load_valid_result(invocation)
    assert result.operation == "author"
    assert result.outcome == "blocked"
    assert result.reason_code == "missing_requirements"
    assert result.change_set is None
    assert result.verification_evidence is None

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json

import pytest

from tests.factory_runner.integration._support import (
    FactoryInvocation,
    git_status,
    invoke_factory,
    load_valid_result,
)


def test_output_inside_workspace_is_rejected_without_creating_it(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    unsafe_output = invocation.workspace / "factory-output"
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    payload["outputs"]["output_dir"] = str(unsafe_output)
    invocation.run_spec_path.write_text(json.dumps(payload), encoding="utf-8")
    invocation = replace(invocation, output_dir=unsafe_output)

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 3, completed.stderr
    assert completed.stdout == ""
    assert not unsafe_output.exists()
    assert git_status(invocation.workspace) == ""


def test_nonempty_output_root_is_rejected_without_overwriting_it(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    invocation.output_dir.mkdir()
    sentinel = invocation.output_dir / "existing.txt"
    sentinel.write_text("preserve me\n", encoding="utf-8")

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 2, completed.stderr
    assert completed.stdout == ""
    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert sorted(path.name for path in invocation.output_dir.iterdir()) == [
        "existing.txt"
    ]
    assert git_status(invocation.workspace) == ""


@pytest.mark.parametrize(
    ("mutate", "reason_code"),
    [
        (lambda _payload: "{", "invalid_json"),
        (
            lambda payload: json.dumps({**payload, "schema": "unknown/v1"}),
            "unsupported_schema",
        ),
        (
            lambda payload: json.dumps({**payload, "schema_version": 2}),
            "unsupported_schema_version",
        ),
    ],
    ids=["invalid-json", "unsupported-schema", "unsupported-schema-version"],
)
def test_invalid_contract_failures_return_stable_exit_without_traceback(
    factory_invocation: Callable[..., FactoryInvocation],
    mutate: Callable[[dict[str, object]], str],
    reason_code: str,
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    invocation.run_spec_path.write_text(mutate(payload), encoding="utf-8")

    completed = invoke_factory(invocation, agent_mode="fail-if-called")

    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "invalid_input"
    assert result.reason_code == reason_code
    assert (invocation.output_dir / "completion.json").is_file()
    assert git_status(invocation.workspace) == ""

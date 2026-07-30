from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, cast

import pytest

from ai_native.adapters.base import AgentResult
from ai_native.factory_runner.author import _BudgetedGatewayAdapter
from ai_native.factory_runner.workflow_adapter import (
    FactoryGatewayAdapter,
    FactoryWorkflowError,
    load_gateway_command,
)


SECRET_CANARY = "gateway-secret-canary"


@dataclass(frozen=True, slots=True)
class StubProcessResult:
    returncode: int | None = 0
    stdout: str = ""
    stderr: str = ""
    termination_reason: str = "exited"


class RecordingProcessRunner:
    def __init__(
        self,
        *,
        result: StubProcessResult | None = None,
        action: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.result = result or StubProcessResult()
        self.action = action
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> StubProcessResult:
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "environment": dict(environment),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.action is not None:
            self.action(environment)
        return self.result


class RecordingGateway:
    def __init__(self, response: str = "done") -> None:
        self.response = response
        self.prompts: list[str] = []

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        del cwd, schema_path, image_paths
        self.prompts.append(prompt)
        return AgentResult(
            text=self.response,
            json_data=None,
            stdout="",
            stderr="",
            command=[],
            returncode=0,
        )


def _write_agent_output(content: str) -> Callable[[dict[str, str]], None]:
    def write(environment: dict[str, str]) -> None:
        prompt_path = Path(environment["AINATIVE_PROMPT_FILE"])
        assert prompt_path.read_text(encoding="utf-8") == "Implement the fixture."
        Path(environment["AINATIVE_OUTPUT_FILE"]).write_text(
            content,
            encoding="utf-8",
        )

    return write


def _adapter(
    tmp_path: Path,
    process_runner: RecordingProcessRunner,
    *,
    environment: dict[str, str] | None = None,
) -> FactoryGatewayAdapter:
    return FactoryGatewayAdapter(
        command=("fixture-agent", "--non-interactive"),
        process_runner=process_runner,
        environment=environment or {"PATH": "/factory/bin"},
        model_profile="fixture-model",
        temp_root=tmp_path / "gateway-temp",
        timeout_seconds=12.5,
    )


def test_load_gateway_command_accepts_only_a_nonempty_json_argv() -> None:
    command = load_gateway_command(
        {
            "AINATIVE_FACTORY_AGENT_COMMAND_JSON": json.dumps(
                ["agent", "--mode", "factory"]
            )
        }
    )

    assert command == ("agent", "--mode", "factory")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-json",
        "{}",
        "[]",
        '["agent", ""]',
        '["agent", 3]',
        '["agent", "bad\\u0000argument"]',
    ],
)
def test_load_gateway_command_rejects_invalid_configuration_without_echoing_it(
    value: str | None,
) -> None:
    environment = {}
    if value is not None:
        environment["AINATIVE_FACTORY_AGENT_COMMAND_JSON"] = value + SECRET_CANARY

    with pytest.raises(FactoryWorkflowError) as exc_info:
        load_gateway_command(environment)

    assert SECRET_CANARY not in str(exc_info.value)


def test_run_uses_only_supplied_environment_and_adapter_file_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOST_SECRET", SECRET_CANARY)
    supplied_environment = {
        "PATH": "/factory/bin",
        "ATTEMPT_GATEWAY_TOKEN_FILE": "/run/secrets/gateway",
        "AINATIVE_SCHEMA_FILE": "stale-schema",
    }
    original_environment = dict(supplied_environment)
    process_runner = RecordingProcessRunner(
        action=_write_agent_output('{"status": "ok"}'),
        result=StubProcessResult(
            stdout=SECRET_CANARY,
            stderr=SECRET_CANARY,
        ),
    )
    schema_path = tmp_path / "response.schema.json"
    schema_path.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _adapter(
        tmp_path,
        process_runner,
        environment=supplied_environment,
    ).run(
        "Implement the fixture.",
        cwd=workspace,
        schema_path=schema_path,
    )

    assert result.text == '{"status": "ok"}'
    assert result.json_data == {"status": "ok"}
    assert result.stdout == ""
    assert result.stderr == ""
    assert supplied_environment == original_environment

    assert len(process_runner.calls) == 1
    call = process_runner.calls[0]
    assert call["command"] == ("fixture-agent", "--non-interactive")
    assert call["cwd"] == workspace
    assert call["timeout_seconds"] == 12.5
    child_environment = call["environment"]
    assert isinstance(child_environment, dict)
    assert child_environment["PATH"] == "/factory/bin"
    assert child_environment["ATTEMPT_GATEWAY_TOKEN_FILE"] == "/run/secrets/gateway"
    assert child_environment["AINATIVE_MODEL_PROFILE"] == "fixture-model"
    assert child_environment["AINATIVE_SCHEMA_FILE"] == str(schema_path)
    assert "HOST_SECRET" not in child_environment
    assert SECRET_CANARY not in repr(child_environment)


def test_run_omits_stale_schema_binding_when_no_schema_is_requested(
    tmp_path: Path,
) -> None:
    process_runner = RecordingProcessRunner(
        action=_write_agent_output("completed"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _adapter(
        tmp_path,
        process_runner,
        environment={
            "PATH": "/factory/bin",
            "AINATIVE_SCHEMA_FILE": "stale-schema",
        },
    ).run("Implement the fixture.", cwd=workspace)

    assert result.text == "completed"
    child_environment = process_runner.calls[0]["environment"]
    assert isinstance(child_environment, dict)
    assert "AINATIVE_SCHEMA_FILE" not in child_environment


def test_review_uses_the_same_noninteractive_gateway_contract(
    tmp_path: Path,
) -> None:
    process_runner = RecordingProcessRunner(
        action=_write_agent_output("reviewed"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(tmp_path, process_runner)

    result = adapter.review(
        cwd=workspace,
        prompt="Implement the fixture.",
        base_branch="main",
    )

    assert result.text == "reviewed"
    assert len(process_runner.calls) == 1


def test_gateway_adapter_does_not_support_image_inputs(tmp_path: Path) -> None:
    process_runner = RecordingProcessRunner(
        action=_write_agent_output("unused"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(tmp_path, process_runner)

    assert adapter.supports_image_inputs() is False
    with pytest.raises(FactoryWorkflowError, match="image"):
        adapter.run(
            "Implement the fixture.",
            cwd=workspace,
            image_paths=[tmp_path / "reference.png"],
        )
    assert process_runner.calls == []


def test_gateway_adapter_rejects_runner_owned_state_mutation(
    tmp_path: Path,
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    state_path = protected_root / "state.json"
    state_path.write_text('{"status":"running"}\n', encoding="utf-8")

    def mutate(environment: dict[str, str]) -> None:
        _write_agent_output("completed")(environment)
        state_path.write_text('{"status":"forged"}\n', encoding="utf-8")

    process_runner = RecordingProcessRunner(action=mutate)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = FactoryGatewayAdapter(
        command=("fixture-agent", "--non-interactive"),
        process_runner=process_runner,
        environment={"PATH": "/factory/bin"},
        model_profile="fixture-model",
        temp_root=tmp_path / "gateway-temp",
        timeout_seconds=12.5,
        protected_roots=(protected_root,),
    )

    with pytest.raises(FactoryWorkflowError, match="runner-owned state"):
        adapter.run("Implement the fixture.", cwd=workspace)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            StubProcessResult(
                returncode=None,
                stdout=SECRET_CANARY,
                stderr=SECRET_CANARY,
                termination_reason="timed_out",
            ),
            "timed out",
        ),
        (
            StubProcessResult(
                returncode=None,
                stdout=SECRET_CANARY,
                stderr=SECRET_CANARY,
                termination_reason="cancelled",
            ),
            "cancelled",
        ),
        (
            StubProcessResult(
                returncode=17,
                stdout=SECRET_CANARY,
                stderr=SECRET_CANARY,
                termination_reason="exited",
            ),
            "exit code 17",
        ),
    ],
)
def test_process_failures_map_to_safe_gateway_errors(
    tmp_path: Path,
    result: StubProcessResult,
    message: str,
) -> None:
    process_runner = RecordingProcessRunner(result=result)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(FactoryWorkflowError) as exc_info:
        _adapter(tmp_path, process_runner).run(
            "Implement the fixture.",
            cwd=workspace,
        )

    assert message in str(exc_info.value)
    assert SECRET_CANARY not in str(exc_info.value)


def test_missing_output_maps_to_safe_gateway_error(tmp_path: Path) -> None:
    process_runner = RecordingProcessRunner(
        result=StubProcessResult(
            stdout=SECRET_CANARY,
            stderr=SECRET_CANARY,
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(FactoryWorkflowError) as exc_info:
        _adapter(tmp_path, process_runner).run(
            "Implement the fixture.",
            cwd=workspace,
        )

    assert "did not produce output" in str(exc_info.value)
    assert SECRET_CANARY not in str(exc_info.value)


def test_invalid_schema_json_maps_to_safe_gateway_error(tmp_path: Path) -> None:
    process_runner = RecordingProcessRunner(
        action=_write_agent_output("{invalid-" + SECRET_CANARY),
    )
    schema_path = tmp_path / "response.schema.json"
    schema_path.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(FactoryWorkflowError) as exc_info:
        _adapter(tmp_path, process_runner).run(
            "Implement the fixture.",
            cwd=workspace,
            schema_path=schema_path,
        )

    assert "invalid JSON" in str(exc_info.value)
    assert SECRET_CANARY not in str(exc_info.value)


def test_budgeted_gateway_enforces_one_shared_turn_limit(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    adapter = _BudgetedGatewayAdapter(
        cast(FactoryGatewayAdapter, gateway),
        max_turns=1,
        max_tokens=100,
    )

    adapter.run("first", tmp_path)
    with pytest.raises(FactoryWorkflowError, match="turn budget"):
        adapter.run("second", tmp_path)

    assert gateway.prompts == ["first"]


def test_budgeted_gateway_rejects_prompt_before_exceeding_token_estimate(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    adapter = _BudgetedGatewayAdapter(
        cast(FactoryGatewayAdapter, gateway),
        max_turns=2,
        max_tokens=1,
    )

    with pytest.raises(FactoryWorkflowError, match="token budget"):
        adapter.run("five!", tmp_path)

    assert gateway.prompts == []


def test_budgeted_gateway_rejects_response_that_exceeds_token_estimate(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway(response="five!")
    adapter = _BudgetedGatewayAdapter(
        cast(FactoryGatewayAdapter, gateway),
        max_turns=2,
        max_tokens=2,
    )

    with pytest.raises(FactoryWorkflowError, match="token budget"):
        adapter.run("one", tmp_path)

    assert gateway.prompts == ["one"]

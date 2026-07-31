from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from ai_native.factory_runner import runner as runner_module
from ai_native.factory_runner.attempt_secrets import AttemptSecretRuntimeError
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.run_result import RunResult
from ai_native.factory_runner.process import CancellationToken
from ai_native.factory_runner.protocol import (
    sha256_digest,
    validate_contract,
    verify_contract_digest,
)
from ai_native.factory_runner.verification import PhaseExecutionOutcome
from tests.factory_runner.integration._support import (
    FactoryInvocation,
    REPOSITORY_ROOT,
    factory_command,
    factory_environment,
    invoke_factory,
    load_valid_result,
)


def _write_run_spec(
    invocation: FactoryInvocation,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    mutate(payload)
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _latest_checkpoint(
    invocation: FactoryInvocation,
    result: RunResult,
) -> Checkpoint | None:
    reference = result.latest_checkpoint
    if reference is None:
        return None
    content = (invocation.output_dir / reference.path).read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    checkpoint = validate_contract(content, expected_schema="checkpoint/v1")
    assert isinstance(checkpoint, Checkpoint)
    verify_contract_digest(checkpoint)
    return checkpoint


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def _replacement(
    invocation: FactoryInvocation,
    result: RunResult,
    checkpoint: Checkpoint,
) -> FactoryInvocation:
    reference = result.latest_checkpoint
    assert reference is not None
    resume_root = invocation.input_dir / "resume"
    shutil.copytree(
        invocation.output_dir / "checkpoints",
        resume_root / "checkpoints",
    )
    output_dir = invocation.output_dir.with_name("output-resume-failure-boundary")

    def mutate(payload: dict[str, object]) -> None:
        identity = payload["identity"]
        assert isinstance(identity, dict)
        identity["attempt_id"] = "attempt-an-03-failure-boundary-resume"
        payload["resume"] = {
            "checkpoint_path": str((resume_root / reference.path).resolve()),
            "expected_digest": checkpoint.checkpoint_digest,
        }
        outputs = payload["outputs"]
        assert isinstance(outputs, dict)
        outputs["output_dir"] = str(output_dir.resolve())

    _write_run_spec(invocation, mutate)
    return replace(
        invocation,
        output_dir=output_dir,
        marker_path=invocation.marker_path.with_name("agent-calls-resumed.log"),
    )


def test_advertised_deterministic_phase_cancellation_checkpoint_is_resumable(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = factory_invocation(operation="author")
    execute_declared_phase = runner_module.execute_declared_phase

    def report_green_cancellation(
        *args: object,
        **kwargs: object,
    ) -> PhaseExecutionOutcome:
        outcome = execute_declared_phase(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs["phase"] == "green":
            return replace(outcome, cancelled=True)
        return outcome

    monkeypatch.setattr(
        runner_module,
        "execute_declared_phase",
        report_green_cancellation,
    )
    logs: list[str] = []
    exit_code = runner_module.execute_factory(
        expected_operation="author",
        run_spec_path=invocation.run_spec_path,
        output_dir=invocation.output_dir,
        environment=factory_environment(invocation, agent_mode="author"),
        cancellation_token=CancellationToken(),
        log=logs.append,
    )

    assert exit_code == 8, logs
    result = load_valid_result(invocation)
    assert result.outcome == "cancelled"
    checkpoint = _latest_checkpoint(invocation, result)
    assert checkpoint is not None

    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=invocation.workspace,
        check=True,
        capture_output=True,
    )
    resumed = _replacement(invocation, result, checkpoint)
    completed = invoke_factory(resumed, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    assert load_valid_result(resumed).reason_code == "completed"


def test_author_stage_cancellation_excludes_partial_in_flight_state(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    stage_marker = invocation.marker_path.with_name("partial-stage.started")
    partial_content = "PARTIAL IN-FLIGHT AUTHOR STATE\n"
    agent_source = "\n".join(
        (
            "from pathlib import Path",
            "import time",
            f"Path('app.py').write_text({partial_content!r}, encoding='utf-8')",
            f"Path({str(stage_marker)!r}).write_text('started', encoding='utf-8')",
            "time.sleep(30)",
        )
    )
    environment = factory_environment(invocation, agent_mode="fail-if-called")
    environment["AINATIVE_FACTORY_AGENT_COMMAND_JSON"] = json.dumps(
        [sys.executable, "-B", "-c", agent_source]
    )
    process = subprocess.Popen(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for(stage_marker)
    process.terminate()
    stdout, stderr = process.communicate(timeout=20)

    assert process.returncode == 8, stderr
    assert stdout == ""
    result = load_valid_result(invocation)
    assert result.outcome == "cancelled"
    checkpoint = _latest_checkpoint(invocation, result)
    assert checkpoint is not None

    assert checkpoint.completed_stages == ()
    assert checkpoint.next_permitted_stage == "plan"
    assert checkpoint.workflow_state["completed_phases"] == ("red",)
    assert checkpoint.workspace_patch_digest is None
    assert "private_author_state" not in checkpoint.workflow_state


@pytest.mark.parametrize(
    ("agent_mode", "max_wall_seconds", "expected_outcome", "expected_reason"),
    (
        ("fail-if-called", 30, "failed", "runner_failed"),
        ("sleep", 3, "timed_out", "timed_out"),
    ),
    ids=("gateway-failure", "gateway-timeout"),
)
def test_failure_result_does_not_advertise_checkpoint_with_unspent_authority(
    factory_invocation: Callable[..., FactoryInvocation],
    agent_mode: str,
    max_wall_seconds: int,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    invocation = factory_invocation(operation="author")

    def use_wall_budget(payload: dict[str, object]) -> None:
        policy = payload["policy"]
        assert isinstance(policy, dict)
        policy["max_wall_seconds"] = max_wall_seconds

    _write_run_spec(invocation, use_wall_budget)
    completed = invoke_factory(invocation, agent_mode=agent_mode)  # type: ignore[arg-type]

    assert completed.returncode in {7, 9}, completed.stderr
    result = load_valid_result(invocation)
    assert result.outcome == expected_outcome
    assert result.reason_code == expected_reason
    assert invocation.marker_path.exists()
    checkpoint = _latest_checkpoint(invocation, result)
    if checkpoint is None:
        return

    consumed = checkpoint.budgets.consumed
    assert consumed.agent_turns >= 1
    assert consumed.model_tokens > 0
    if expected_outcome == "timed_out":
        assert consumed.wall_seconds == max_wall_seconds


def test_terminal_checkpoint_failure_does_not_advertise_stale_predecessor(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = factory_invocation(operation="author")
    publish_external_bundle = runner_module.OutputWriter.publish_external_bundle

    def fail_terminal_checkpoint(
        writer: object,
        references: object,
        contents: object,
    ) -> object:
        assert isinstance(contents, dict)
        checkpoint_contents = [
            content
            for path, content in contents.items()
            if isinstance(path, str) and path.endswith("/checkpoint.json")
        ]
        if checkpoint_contents:
            checkpoint = json.loads(checkpoint_contents[0])
            if checkpoint["workflow_state"]["boundary"] == "author-failed":
                raise OSError("simulated terminal checkpoint publication failure")
        return publish_external_bundle(writer, references, contents)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runner_module.OutputWriter,
        "publish_external_bundle",
        fail_terminal_checkpoint,
    )
    logs: list[str] = []
    exit_code = runner_module.execute_factory(
        expected_operation="author",
        run_spec_path=invocation.run_spec_path,
        output_dir=invocation.output_dir,
        environment=factory_environment(
            invocation,
            agent_mode="fail-if-called",
        ),
        cancellation_token=CancellationToken(),
        log=logs.append,
    )

    assert exit_code == 7, logs
    result = load_valid_result(invocation)
    assert result.outcome == "failed"
    assert result.reason_code == "runner_failed"
    assert result.latest_checkpoint is None
    assert "[factory] terminal checkpoint could not be written" in logs


def test_safe_boundary_publication_failure_does_not_advertise_stale_predecessor(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = factory_invocation(operation="author")
    publish_external_bundle = runner_module.OutputWriter.publish_external_bundle

    def fail_plan_checkpoint(
        writer: object,
        references: object,
        contents: object,
    ) -> object:
        assert isinstance(contents, dict)
        checkpoint_contents = [
            content
            for path, content in contents.items()
            if isinstance(path, str) and path.endswith("/checkpoint.json")
        ]
        if checkpoint_contents:
            checkpoint = json.loads(checkpoint_contents[0])
            if checkpoint["workflow_state"]["boundary"] == "stage:plan":
                raise OSError("simulated safe-boundary publication failure")
        return publish_external_bundle(writer, references, contents)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runner_module.OutputWriter,
        "publish_external_bundle",
        fail_plan_checkpoint,
    )
    logs: list[str] = []
    exit_code = runner_module.execute_factory(
        expected_operation="author",
        run_spec_path=invocation.run_spec_path,
        output_dir=invocation.output_dir,
        environment=factory_environment(invocation, agent_mode="author"),
        cancellation_token=CancellationToken(),
        log=logs.append,
    )

    assert exit_code == 7, logs
    result = load_valid_result(invocation)
    assert result.outcome == "failed"
    assert result.reason_code == "runner_failed"
    assert result.latest_checkpoint is None


def test_local_secret_materialization_failure_is_a_runner_failure(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = factory_invocation(operation="author")

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise AttemptSecretRuntimeError("simulated local credential staging failure")

    monkeypatch.setattr(
        runner_module,
        "materialize_attempt_secret_files",
        fail_materialization,
    )
    logs: list[str] = []
    exit_code = runner_module.execute_factory(
        expected_operation="author",
        run_spec_path=invocation.run_spec_path,
        output_dir=invocation.output_dir,
        environment=factory_environment(invocation, agent_mode="author"),
        cancellation_token=CancellationToken(),
        log=logs.append,
    )

    assert exit_code == 7, logs
    result = load_valid_result(invocation)
    assert result.outcome == "failed"
    assert result.reason_code == "runner_failed"
    assert not invocation.marker_path.exists()

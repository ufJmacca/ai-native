from __future__ import annotations

from collections.abc import Callable
import json
import subprocess
import sys
import time

from tests.factory_runner.integration._support import (
    FactoryInvocation,
    REPOSITORY_ROOT,
    factory_command,
    factory_environment,
    load_valid_result,
)


def test_factory_verify_maps_sigterm_during_declared_command_to_cancelled(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="verify")
    command_marker = invocation.marker_path.with_name("verification-command.started")
    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    run_spec["policy"]["max_wall_seconds"] = 15
    run_spec["policy"]["allowed_commands"] = [
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from pathlib import Path; import time; "
                f"Path({str(command_marker)!r}).write_text('started'); "
                "time.sleep(30)"
            ),
        ]
    ]
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    process = subprocess.Popen(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=factory_environment(invocation, agent_mode="fail-if-called"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    marker_deadline = time.monotonic() + 10
    while not command_marker.exists() and time.monotonic() < marker_deadline:
        time.sleep(0.01)
    assert command_marker.exists()

    process.terminate()
    stdout, stderr = process.communicate(timeout=20)

    assert process.returncode == 8, stderr
    assert stdout == ""
    result = load_valid_result(invocation)
    assert result.operation == "verify"
    assert result.outcome == "cancelled"
    assert result.reason_code == "cancelled"
    assert result.verification_evidence is None

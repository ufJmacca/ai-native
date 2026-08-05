from __future__ import annotations

from pathlib import Path
import sys
import threading
import time

import pytest

from ai_native.factory_runner import process as process_module
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessIsolationError,
    FactoryProcessRunner,
)


def test_cancellation_token_is_one_way_and_idempotent() -> None:
    token = CancellationToken()

    assert token.cancelled is False

    token.cancel()
    token.cancel()

    assert token.cancelled is True


def test_deadline_uses_an_absolute_monotonic_budget() -> None:
    now = [100.0]
    deadline = Deadline.from_timeout(5.0, clock=lambda: now[0])

    assert deadline.expired is False
    assert deadline.remaining_seconds() == pytest.approx(5.0)

    now[0] = 103.5
    assert deadline.remaining_seconds() == pytest.approx(1.5)

    now[0] = 106.0
    assert deadline.expired is True
    assert deadline.remaining_seconds() == 0.0


def test_process_runner_connects_standard_input_to_devnull(
    tmp_path: Path,
) -> None:
    result = FactoryProcessRunner().run(
        (
            sys.executable,
            "-c",
            (
                "import sys; "
                "value = sys.stdin.buffer.read(1); "
                "print('EOF' if value == b'' else 'INPUT')"
            ),
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=2.0,
    )

    assert result.returncode == 0
    assert result.stdout == "EOF\n"
    assert result.stderr == ""
    assert result.termination_reason == "exited"


def test_process_runner_enforces_its_deadline(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    result = FactoryProcessRunner().run(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=0.05,
    )

    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert result.returncode is None
    assert result.termination_reason == "timed_out"


def test_process_runner_does_not_spawn_after_cancellation(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "spawned"
    token = CancellationToken()
    token.cancel()

    result = FactoryProcessRunner(cancellation_token=token).run(
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(canary)!r}).touch()",
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=2.0,
    )

    assert not canary.exists()
    assert result.returncode is None
    assert result.termination_reason == "cancelled"


def test_process_runner_honours_a_shared_absolute_deadline(
    tmp_path: Path,
) -> None:
    deadline = Deadline.from_timeout(0.05)
    started = time.monotonic()

    result = FactoryProcessRunner(deadline=deadline).run(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5.0,
    )

    assert time.monotonic() - started < 2.0
    assert result.returncode is None
    assert result.termination_reason == "timed_out"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux child-subreaper isolation",
)
def test_process_runner_fails_closed_without_linux_subreaper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = tmp_path / "spawned-without-subreaper"
    monkeypatch.setattr(process_module, "_enable_child_subreaper", lambda: False)

    with pytest.raises(FactoryProcessIsolationError, match="subreaper"):
        FactoryProcessRunner().run(
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(canary)!r}).touch()",
            ),
            cwd=tmp_path,
            environment={},
            timeout_seconds=2.0,
        )

    assert not canary.exists()


def test_process_runner_serializes_exclusive_operations(tmp_path: Path) -> None:
    first_runner = FactoryProcessRunner()
    second_runner = FactoryProcessRunner()
    canary = tmp_path / "concurrent-process"
    attempted = threading.Event()
    completed = threading.Event()

    def run_process() -> None:
        attempted.set()
        second_runner.run(
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(canary)!r}).touch()",
            ),
            cwd=tmp_path,
            environment={},
            timeout_seconds=2.0,
        )
        completed.set()

    with first_runner.exclusive_operation():
        contender = threading.Thread(target=run_process)
        contender.start()
        assert attempted.wait(timeout=1.0)
        assert not completed.wait(timeout=0.1)
        assert not canary.exists()

    contender.join(timeout=2.0)
    assert completed.is_set()
    assert canary.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
def test_process_runner_terminates_descendants_after_leader_exits(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "escaped-descendant"

    result = FactoryProcessRunner(termination_grace_seconds=0.05).run(
        (
            sys.executable,
            "-c",
            (
                "import os, time; "
                "pid = os.fork(); "
                f"(time.sleep(0.25), open({str(canary)!r}, 'wb').close()) "
                "if pid == 0 else None; "
                "os._exit(0)"
            ),
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=2.0,
    )

    time.sleep(0.4)
    assert result.returncode == 0
    assert result.termination_reason == "exited"
    assert not canary.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux child-subreaper isolation",
)
def test_process_runner_terminates_detached_descendants_after_leader_exits(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "escaped-detached-descendant"

    result = FactoryProcessRunner(termination_grace_seconds=0.1).run(
        (
            sys.executable,
            "-c",
            (
                "import os, time; "
                "pid = os.fork(); "
                f"(os.setsid(), time.sleep(0.3), open({str(canary)!r}, 'wb').close()) "
                "if pid == 0 else None; "
                "os._exit(0)"
            ),
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=2.0,
    )

    time.sleep(0.5)
    assert result.returncode == 0
    assert result.termination_reason == "exited"
    assert not canary.exists()

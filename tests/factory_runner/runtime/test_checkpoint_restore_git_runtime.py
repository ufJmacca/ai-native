from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import subprocess

import pytest

from ai_native.factory_runner.checkpoints import (
    CheckpointError,
    CheckpointManager,
    LoadedCheckpoint,
)
from ai_native.factory_runner.git_runtime import FactoryGitRuntime
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessResult,
    FactoryProcessRunner,
    TerminationReason,
)
from tests.factory_runner.runtime.test_checkpoints_an03 import _bundle, _load


def _git(workspace: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(workspace), *arguments),
        input=input_bytes,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        },
    )
    return completed.stdout


def _repository(tmp_path: Path) -> tuple[Path, bytes]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    source = workspace / "app.py"
    source.write_text("before\n", encoding="utf-8")
    _git(workspace, "add", "app.py")
    _git(
        workspace,
        "-c",
        "user.name=Factory Test",
        "-c",
        "user.email=factory@example.invalid",
        "commit",
        "-m",
        "base",
    )
    source.write_text("after\n", encoding="utf-8")
    patch = _git(
        workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "HEAD",
        "--",
    )
    _git(workspace, "checkout", "--", "app.py")
    return workspace, patch


def _runtime(
    tmp_path: Path,
    workspace: Path,
    *,
    process_runner: FactoryProcessRunner | None = None,
) -> FactoryGitRuntime:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    temporary_dir = tmp_path / "runtime-tmp"
    temporary_dir.mkdir()
    deadline = Deadline.from_timeout(30)
    runner = process_runner or FactoryProcessRunner(
        cancellation_token=CancellationToken(),
        deadline=deadline,
    )
    return FactoryGitRuntime(
        workspace=workspace,
        output_dir=output_dir,
        environment={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": str(temporary_dir),
        },
        process_runner=runner,
        deadline=deadline,
    )


def _loaded(
    tmp_path: Path,
    patch: bytes,
) -> tuple[CheckpointManager, LoadedCheckpoint]:
    root = tmp_path / "checkpoints"
    root.mkdir()
    checkpoint, run_spec, objects = _bundle(root, patch)
    manager = CheckpointManager(root)
    reference = manager.write_safe_boundary(
        checkpoint=checkpoint,
        objects=objects,
    )
    return manager, _load(manager, reference.path, checkpoint, run_spec)


def test_restore_uses_runner_owned_git_without_direct_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, patch = _repository(tmp_path)
    manager, loaded = _loaded(tmp_path, patch)
    runtime = _runtime(tmp_path, workspace)

    def reject_direct_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("checkpoint restore bypassed the bounded Git runtime")

    monkeypatch.setattr(subprocess, "run", reject_direct_subprocess)

    manager.restore_transactionally(loaded, git_runtime=runtime)

    assert (workspace / "app.py").read_text(encoding="utf-8") == "after\n"
    assert not tuple((tmp_path / "runtime-tmp").iterdir())


@pytest.mark.parametrize("ignored", [False, True], ids=["untracked", "ignored"])
def test_restore_rejects_a_dirty_workspace_without_mutation(
    tmp_path: Path,
    ignored: bool,
) -> None:
    workspace, patch = _repository(tmp_path)
    unrelated = workspace / "unrelated.txt"
    if ignored:
        (workspace / ".git/info/exclude").write_text(
            "unrelated.txt\n",
            encoding="utf-8",
        )
    unrelated.write_text("preserve me\n", encoding="utf-8")
    manager, loaded = _loaded(tmp_path, patch)
    runtime = _runtime(tmp_path, workspace)

    with pytest.raises(CheckpointError, match="clean|restore"):
        manager.restore_transactionally(loaded, git_runtime=runtime)

    assert (workspace / "app.py").read_text(encoding="utf-8") == "before\n"
    assert unrelated.read_text(encoding="utf-8") == "preserve me\n"


class _RacingProcessRunner:
    def __init__(
        self,
        delegate: FactoryProcessRunner,
        *,
        workspace: Path,
        interruption: TerminationReason | None = None,
    ) -> None:
        self.delegate = delegate
        self.workspace = workspace
        self.interruption = interruption
        self.commands: list[tuple[str, ...]] = []
        self.raced = False

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        argv = tuple(command)
        self.commands.append(argv)
        if (
            not self.raced
            and len(argv) > 1
            and argv[1] == "apply"
            and "--check" not in argv
        ):
            (self.workspace / "app.py").write_text(
                "raced\n",
                encoding="utf-8",
            )
            (self.workspace / "race.tmp").write_text(
                "remove during rollback\n",
                encoding="utf-8",
            )
            self.raced = True
            if self.interruption is not None:
                return FactoryProcessResult(
                    command=argv,
                    returncode=None,
                    stdout="",
                    stderr="",
                    termination_reason=self.interruption,
                )
        return self.delegate.run(
            command,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )


def test_failed_check_never_attempts_apply_or_mutates_the_workspace(
    tmp_path: Path,
) -> None:
    workspace, _ = _repository(tmp_path)
    manager, loaded = _loaded(tmp_path, b"not a valid patch\n")
    deadline = Deadline.from_timeout(30)
    recording_runner = _RacingProcessRunner(
        FactoryProcessRunner(
            cancellation_token=CancellationToken(),
            deadline=deadline,
        ),
        workspace=workspace,
    )
    runtime = _runtime(
        tmp_path,
        workspace,
        process_runner=recording_runner,  # type: ignore[arg-type]
    )

    with pytest.raises(CheckpointError, match="restore|patch"):
        manager.restore_transactionally(loaded, git_runtime=runtime)

    apply_commands = [
        command
        for command in recording_runner.commands
        if len(command) > 1 and command[1] == "apply"
    ]
    assert len(apply_commands) == 1
    assert "--check" in apply_commands[0]
    assert not recording_runner.raced
    assert (workspace / "app.py").read_text(encoding="utf-8") == "before\n"
    assert (
        _git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == b""
    )


@pytest.mark.parametrize(
    "interruption",
    [None, "cancelled", "timed_out"],
    ids=["apply-failure", "cancelled", "timed-out"],
)
def test_restore_rolls_back_if_apply_loses_a_race_after_check(
    tmp_path: Path,
    interruption: TerminationReason | None,
) -> None:
    workspace, patch = _repository(tmp_path)
    manager, loaded = _loaded(tmp_path, patch)
    deadline = Deadline.from_timeout(30)
    racing_runner = _RacingProcessRunner(
        FactoryProcessRunner(
            cancellation_token=CancellationToken(),
            deadline=deadline,
        ),
        workspace=workspace,
        interruption=interruption,
    )
    runtime = _runtime(
        tmp_path,
        workspace,
        process_runner=racing_runner,  # type: ignore[arg-type]
    )

    with pytest.raises(CheckpointError, match="restore|apply"):
        manager.restore_transactionally(loaded, git_runtime=runtime)

    apply_commands = [
        command
        for command in racing_runner.commands
        if len(command) > 1 and command[1] == "apply"
    ]
    assert len(apply_commands) == 2
    assert "--check" in apply_commands[0]
    assert "--check" not in apply_commands[1]
    assert (workspace / "app.py").read_text(encoding="utf-8") == "before\n"
    assert not (workspace / "race.tmp").exists()
    assert (
        _git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == b""
    )

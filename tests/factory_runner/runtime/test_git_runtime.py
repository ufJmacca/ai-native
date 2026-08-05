from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from ai_native.factory_runner import git_runtime as git_runtime_module
from ai_native.factory_runner.changes import (
    ChangePolicyError,
    capture_repository_security_snapshot,
    validate_author_boundary,
)
from ai_native.factory_runner.git_runtime import (
    FactoryGitCancelled,
    FactoryGitError,
    FactoryGitRuntime,
    FactoryGitTimedOut,
)
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessResult,
    FactoryProcessRunner,
)


class _RecordingProcessRunner(FactoryProcessRunner):
    def __init__(
        self,
        *,
        cancellation_token: CancellationToken,
        deadline: Deadline,
    ) -> None:
        super().__init__(
            cancellation_token=cancellation_token,
            deadline=deadline,
        )
        self.index_paths: list[Path] = []
        self.index_contents: list[bytes] = []
        self.index_modes: list[int] = []
        self.index_mtimes_ns: list[int] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        private_index = Path(environment["GIT_INDEX_FILE"])
        metadata = private_index.stat(follow_symlinks=False)
        self.index_paths.append(private_index)
        self.index_contents.append(private_index.read_bytes())
        self.index_modes.append(stat.S_IMODE(metadata.st_mode))
        self.index_mtimes_ns.append(metadata.st_mtime_ns)
        return super().run(command, cwd, environment, timeout_seconds)


class _RemovingProcessRunner(FactoryProcessRunner):
    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        del cwd, timeout_seconds
        Path(environment["GIT_INDEX_FILE"]).unlink()
        return FactoryProcessResult(
            command=tuple(command),
            returncode=0,
            stdout="",
            stderr="",
            termination_reason="exited",
        )


class _LockingProcessRunner(FactoryProcessRunner):
    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        del cwd, timeout_seconds
        private_index = Path(environment["GIT_INDEX_FILE"])
        private_lock = Path(f"{private_index}.lock")
        private_lock.write_bytes(b"lock")
        private_lock.chmod(0o600)
        return FactoryProcessResult(
            command=tuple(command),
            returncode=0,
            stdout="",
            stderr="",
            termination_reason="exited",
        )


class _PausingProcessRunner(FactoryProcessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.index_materialised = threading.Event()
        self.release = threading.Event()

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        del cwd, timeout_seconds
        Path(environment["GIT_INDEX_FILE"]).stat(follow_symlinks=False)
        self.index_materialised.set()
        if not self.release.wait(timeout=2.0):
            raise RuntimeError("private-index test process was not released")
        return FactoryProcessResult(
            command=tuple(command),
            returncode=0,
            stdout="",
            stderr="",
            termination_reason="exited",
        )


def _runtime(
    tmp_path: Path,
    *,
    cancellation_token: CancellationToken,
    deadline: Deadline,
    process_runner: FactoryProcessRunner | None = None,
) -> FactoryGitRuntime:
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()
    return FactoryGitRuntime(
        workspace=workspace,
        output_dir=output_dir,
        environment={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        process_runner=process_runner
        or FactoryProcessRunner(
            cancellation_token=cancellation_token,
            deadline=deadline,
        ),
        deadline=deadline,
    )


def _initialise_repository(workspace: Path) -> Path:
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Factory Runner Test"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "factory-runner-test@example.invalid",
        ],
        cwd=workspace,
        check=True,
    )
    tracked = workspace / "tracked.txt"
    tracked.write_text("admitted content\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture base"],
        cwd=workspace,
        check=True,
    )
    return tracked


def _private_root(tmp_path: Path) -> Path:
    private_root = tmp_path / "runner-private"
    private_root.mkdir(mode=0o700)
    return private_root


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


def test_ephemeral_private_index_keeps_diff_off_the_repository_index(
    tmp_path: Path,
) -> None:
    cancellation_token = CancellationToken()
    deadline = Deadline.from_timeout(30)
    process_runner = _RecordingProcessRunner(
        cancellation_token=cancellation_token,
        deadline=deadline,
    )
    runtime = _runtime(
        tmp_path,
        cancellation_token=cancellation_token,
        deadline=deadline,
        process_runner=process_runner,
    )
    tracked = _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)

    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    repository_index = runtime.workspace / ".git" / "index"
    repository_index_before = repository_index.read_bytes()
    repository_metadata_before = repository_index.stat(follow_symlinks=False)
    assert "GIT_INDEX_FILE" not in isolated.environment
    assert not any(private_root.iterdir())

    # Force Git to reconsider the cached stat data without changing content.
    tracked_metadata = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_metadata.st_atime_ns, tracked_metadata.st_mtime_ns + 1_000_000_000),
    )
    assert isolated.run_diff("diff", "--quiet", "HEAD", "--") == b""

    repository_metadata_after = repository_index.stat(follow_symlinks=False)
    assert repository_index.read_bytes() == repository_index_before
    assert (
        repository_metadata_after.st_mtime_ns == repository_metadata_before.st_mtime_ns
    )
    assert (
        repository_metadata_after.st_ctime_ns == repository_metadata_before.st_ctime_ns
    )
    assert "GIT_INDEX_FILE" not in runtime.environment
    assert len(process_runner.index_paths) == 1
    private_index = process_runner.index_paths[0]
    assert private_index.parent == private_root.resolve(strict=True)
    assert private_index != repository_index
    assert process_runner.index_modes == [0o600]
    assert process_runner.index_mtimes_ns == [repository_metadata_before.st_mtime_ns]
    assert not private_index.exists()
    assert not Path(f"{private_index}.lock").exists()
    assert not any(private_root.iterdir())


def test_private_index_preserves_racy_clean_detection(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
    )
    tracked = _initialise_repository(runtime.workspace)
    subprocess.run(
        ["git", "config", "core.trustctime", "false"],
        cwd=runtime.workspace,
        check=True,
    )
    racy_timestamp_ns = 946_684_800_000_000_000
    tracked_metadata = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_metadata.st_atime_ns, racy_timestamp_ns),
    )
    subprocess.run(
        ["git", "update-index", "--refresh"],
        cwd=runtime.workspace,
        check=True,
    )
    repository_index = runtime.workspace / ".git" / "index"
    index_metadata = repository_index.stat(follow_symlinks=False)
    os.utime(
        repository_index,
        ns=(index_metadata.st_atime_ns, racy_timestamp_ns),
    )
    tracked.write_text("modified content\n", encoding="utf-8")
    assert tracked.stat().st_size == len("admitted content\n")
    tracked_metadata = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_metadata.st_atime_ns, racy_timestamp_ns),
    )
    isolated = runtime.with_ephemeral_private_indexes(
        private_root=_private_root(tmp_path)
    )

    assert isolated.run(
        "status", "--porcelain=v1", "--untracked-files=no"
    ) == b" M tracked.txt\n"


def test_prior_private_index_tampering_cannot_affect_the_next_git_call(
    tmp_path: Path,
) -> None:
    cancellation_token = CancellationToken()
    deadline = Deadline.from_timeout(30)
    process_runner = _RecordingProcessRunner(
        cancellation_token=cancellation_token,
        deadline=deadline,
    )
    runtime = _runtime(
        tmp_path,
        cancellation_token=cancellation_token,
        deadline=deadline,
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    repository_index = runtime.workspace / ".git" / "index"

    assert isolated.run("rev-parse", "HEAD")
    remembered_path = process_runner.index_paths[0]
    assert not remembered_path.exists()
    remembered_path.write_bytes(b"adversarial prior copy")
    remembered_path.chmod(0o600)

    assert isolated.run("status", "--porcelain=v1") == b""

    next_path = process_runner.index_paths[1]
    assert next_path != remembered_path
    assert process_runner.index_contents[1] == repository_index.read_bytes()
    assert remembered_path.read_bytes() == b"adversarial prior copy"
    assert not next_path.exists()
    assert not Path(f"{next_path}.lock").exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux process isolation",
)
def test_private_index_excludes_a_concurrent_same_uid_runner_process(
    tmp_path: Path,
) -> None:
    process_runner = _PausingProcessRunner()
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    tamper_marker = tmp_path / "private-index-tampered"
    git_errors: list[BaseException] = []
    adversary_errors: list[BaseException] = []
    adversary_attempted = threading.Event()
    adversary_completed = threading.Event()

    def run_git() -> None:
        try:
            isolated.run("status", "--porcelain=v1")
        except BaseException as exc:
            git_errors.append(exc)

    def run_adversary() -> None:
        adversary_attempted.set()
        try:
            FactoryProcessRunner().run(
                (
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"root = Path({str(private_root)!r}); "
                        "indexes = tuple(root.glob('factory-git-index-*')); "
                        "[path.write_bytes(b'tampered') for path in indexes]; "
                        f"Path({str(tamper_marker)!r}).touch() if indexes else None"
                    ),
                ),
                cwd=tmp_path,
                environment={},
                timeout_seconds=2.0,
            )
        except BaseException as exc:
            adversary_errors.append(exc)
        finally:
            adversary_completed.set()

    git_thread = threading.Thread(target=run_git)
    git_thread.start()
    assert process_runner.index_materialised.wait(timeout=1.0)
    adversary_thread = threading.Thread(target=run_adversary)
    adversary_thread.start()
    assert adversary_attempted.wait(timeout=1.0)
    assert not adversary_completed.wait(timeout=0.1)

    process_runner.release.set()
    git_thread.join(timeout=2.0)
    adversary_thread.join(timeout=2.0)

    assert not git_errors
    assert not adversary_errors
    assert adversary_completed.is_set()
    assert not tamper_marker.exists()
    assert not any(private_root.iterdir())


def test_private_index_copy_rejects_an_unsafe_private_root(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
    )
    _initialise_repository(runtime.workspace)
    real_private_root = _private_root(tmp_path)
    private_root_alias = tmp_path / "runner-private-alias"
    private_root_alias.symlink_to(real_private_root, target_is_directory=True)

    with pytest.raises(FactoryGitError, match="private index root"):
        runtime.with_ephemeral_private_indexes(private_root=private_root_alias)

    assert not any(real_private_root.iterdir())


def test_private_index_copy_rejects_a_hard_linked_repository_index(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
    )
    _initialise_repository(runtime.workspace)
    repository_index = runtime.workspace / ".git" / "index"
    os.link(repository_index, tmp_path / "unexpected-index-link")
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)

    with pytest.raises(FactoryGitError, match="repository index is unsafe"):
        isolated.run("status", "--porcelain=v1")

    assert not any(private_root.iterdir())


def test_private_index_copy_rejects_source_mutation_and_removes_the_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
    )
    _initialise_repository(runtime.workspace)
    repository_index = runtime.workspace / ".git" / "index"
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    real_read = git_runtime_module.os.read
    mutated = False

    def mutate_index_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        content = real_read(descriptor, size)
        if content and not mutated:
            mutated = True
            repository_index.write_bytes(repository_index.read_bytes() + b"mutated")
        return content

    monkeypatch.setattr(git_runtime_module.os, "read", mutate_index_after_read)

    with pytest.raises(FactoryGitError, match="changed during private copy"):
        isolated.run("status", "--porcelain=v1")

    assert mutated
    assert not any(private_root.iterdir())


def test_private_index_materialisation_attempts_all_cleanup_after_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    real_close = git_runtime_module.os.close
    closed_descriptors: list[int] = []

    def close_and_fail_once(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)
        if len(closed_descriptors) == 1:
            raise OSError("synthetic close failure")

    monkeypatch.setattr(git_runtime_module.os, "close", close_and_fail_once)

    with pytest.raises(FactoryGitError, match="private index cleanup failed"):
        isolated.run("status", "--porcelain=v1")

    assert len(closed_descriptors) == 2
    assert len(set(closed_descriptors)) == 2
    assert not any(private_root.iterdir())


def test_private_index_cleanup_attempts_lock_and_index_unlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_runner = _LockingProcessRunner()
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    real_unlink = Path.unlink
    attempted_unlinks: list[Path] = []

    def unlink_and_fail_for_lock(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        attempted_unlinks.append(path)
        real_unlink(path, missing_ok=missing_ok)
        if path.name.endswith(".lock"):
            raise OSError("synthetic lock unlink failure")

    monkeypatch.setattr(Path, "unlink", unlink_and_fail_for_lock)

    with pytest.raises(FactoryGitError, match="private index cleanup failed"):
        isolated.run("rev-parse", "HEAD")

    assert len(attempted_unlinks) == 2
    assert attempted_unlinks[0].name.endswith(".lock")
    assert not attempted_unlinks[1].name.endswith(".lock")
    assert not any(private_root.iterdir())


def test_private_index_cleanup_fails_closed_when_the_copy_disappears(
    tmp_path: Path,
) -> None:
    process_runner = _RemovingProcessRunner()
    runtime = _runtime(
        tmp_path,
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)

    with pytest.raises(FactoryGitError, match="cleanup was unsafe"):
        isolated.run("rev-parse", "HEAD")

    assert not any(private_root.iterdir())


def test_boundary_detects_real_index_tampering_before_spawning_git(
    tmp_path: Path,
) -> None:
    cancellation_token = CancellationToken()
    deadline = Deadline.from_timeout(30)
    process_runner = _RecordingProcessRunner(
        cancellation_token=cancellation_token,
        deadline=deadline,
    )
    runtime = _runtime(
        tmp_path,
        cancellation_token=cancellation_token,
        deadline=deadline,
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    security_snapshot = capture_repository_security_snapshot(isolated)
    assert process_runner.index_paths == []

    repository_index = runtime.workspace / ".git" / "index"
    repository_index.write_bytes(repository_index.read_bytes() + b"tampered")

    with pytest.raises(ChangePolicyError, match="changed Git security metadata"):
        validate_author_boundary(
            object(),  # type: ignore[arg-type]
            git_runtime=isolated,
            security_snapshot=security_snapshot,
        )

    assert process_runner.index_paths == []


@pytest.mark.parametrize("marker_name", ["commondir", "gitdir"])
def test_boundary_rejects_post_admission_git_indirection_before_spawning_git(
    tmp_path: Path,
    marker_name: str,
) -> None:
    cancellation_token = CancellationToken()
    deadline = Deadline.from_timeout(30)
    process_runner = _RecordingProcessRunner(
        cancellation_token=cancellation_token,
        deadline=deadline,
    )
    runtime = _runtime(
        tmp_path,
        cancellation_token=cancellation_token,
        deadline=deadline,
        process_runner=process_runner,
    )
    _initialise_repository(runtime.workspace)
    private_root = _private_root(tmp_path)
    isolated = runtime.with_ephemeral_private_indexes(private_root=private_root)
    security_snapshot = capture_repository_security_snapshot(isolated)
    assert process_runner.index_paths == []

    (runtime.workspace / ".git" / marker_name).write_text(".\n", encoding="utf-8")

    with pytest.raises(ChangePolicyError, match="metadata indirection"):
        validate_author_boundary(
            object(),  # type: ignore[arg-type]
            git_runtime=isolated,
            security_snapshot=security_snapshot,
        )

    assert process_runner.index_paths == []

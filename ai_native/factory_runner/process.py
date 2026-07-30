from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Literal


TerminationReason = Literal["exited", "timed_out", "cancelled"]
DEFAULT_MAX_CAPTURE_BYTES = 1_048_576
DEFAULT_TERMINATION_GRACE_SECONDS = 0.5
_POLL_INTERVAL_SECONDS = 0.02
_READ_CHUNK_BYTES = 65_536
_PR_SET_CHILD_SUBREAPER = 36
_SUBREAPER_LOCK = threading.Lock()
_SUBREAPER_ENABLED = False


class CancellationToken:
    """Thread-safe, one-way cancellation state shared by a factory attempt."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def _wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)


@dataclass(frozen=True, slots=True)
class Deadline:
    """An absolute deadline measured by a monotonic clock."""

    _expires_at: float
    _clock: Callable[[], float]

    @classmethod
    def from_timeout(
        cls,
        timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> Deadline:
        timeout = _validated_timeout(timeout_seconds)
        return cls(_expires_at=clock() + timeout, _clock=clock)

    @property
    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def remaining_seconds(self) -> float:
        return max(0.0, self._expires_at - self._clock())


@dataclass(frozen=True, slots=True)
class FactoryProcessResult:
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    termination_reason: TerminationReason
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


class _BoundedCapture:
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._buffer = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self._maximum_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self._truncated = True

    def snapshot(self) -> tuple[bytes, str, bool]:
        with self._lock:
            value = bytes(self._buffer)
            truncated = self._truncated
        return value, value.decode("utf-8", errors="replace"), truncated


def _validated_timeout(timeout_seconds: float) -> float:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "timeout_seconds must be a finite non-negative number"
        ) from exc
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_seconds must be a finite non-negative number")
    return timeout


def _validated_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes, bytearray)):
        raise ValueError("command must be an argument vector")
    argv = tuple(command)
    if not argv or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in argv
    ):
        raise ValueError("command must be a non-empty argument vector")
    return argv


def _drain_pipe(stream: BinaryIO, capture: _BoundedCapture) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            capture.append(chunk)
    finally:
        stream.close()


def _signal_process_group(
    process: subprocess.Popen[bytes],
    selected_signal: signal.Signals,
) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, selected_signal)
            return
        except ProcessLookupError:
            return
    if process.poll() is not None:
        return
    if selected_signal is signal.SIGTERM:
        process.terminate()
    else:
        process.kill()


def _process_group_exists(process_group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _enable_child_subreaper() -> bool:
    """Keep detached descendants attributable to this Linux runner process."""

    global _SUBREAPER_ENABLED
    if not sys.platform.startswith("linux"):
        return False
    with _SUBREAPER_LOCK:
        if _SUBREAPER_ENABLED:
            return True
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        except (AttributeError, OSError):
            return False
        if result != 0:
            return False
        _SUBREAPER_ENABLED = True
        return True


def _linux_descendants(parent_pid: int) -> set[int]:
    relationships: dict[int, int] = {}
    try:
        process_entries = tuple(Path("/proc").iterdir())
    except OSError:
        return set()
    for entry in process_entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_content = (entry / "stat").read_text(encoding="utf-8")
            tail = stat_content[stat_content.rfind(")") + 2 :].split()
            relationships[int(entry.name)] = int(tail[1])
        except (IndexError, OSError, ValueError):
            continue
    descendants: set[int] = set()
    frontier = {parent_pid}
    while frontier:
        children = {
            pid
            for pid, recorded_parent in relationships.items()
            if recorded_parent in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def _signal_pids(process_ids: set[int], selected_signal: signal.Signals) -> None:
    for process_id in process_ids:
        try:
            os.kill(process_id, selected_signal)
        except (ProcessLookupError, PermissionError):
            continue


def _reap_adopted_children(process_ids: set[int]) -> None:
    for process_id in process_ids:
        try:
            os.waitpid(process_id, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            continue


def _terminate_new_descendants(
    *,
    parent_pid: int,
    preexisting_descendants: set[int],
    grace_seconds: float,
) -> None:
    if not sys.platform.startswith("linux"):
        return
    deadline = time.monotonic() + grace_seconds
    observed: set[int] = set()
    while True:
        owned = _linux_descendants(parent_pid) - preexisting_descendants
        observed.update(owned)
        if not owned or time.monotonic() >= deadline:
            break
        _signal_pids(owned, signal.SIGTERM)
        time.sleep(_POLL_INTERVAL_SECONDS)
        _reap_adopted_children(owned)
    remaining = _linux_descendants(parent_pid) - preexisting_descendants
    observed.update(remaining)
    _signal_pids(remaining, signal.SIGKILL)
    kill_deadline = time.monotonic() + grace_seconds
    while remaining and time.monotonic() < kill_deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        _reap_adopted_children(remaining)
        remaining = _linux_descendants(parent_pid) - preexisting_descendants
        observed.update(remaining)
    _reap_adopted_children(observed)


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> None:
    _signal_process_group(process, signal.SIGTERM)
    grace_deadline = time.monotonic() + grace_seconds
    if os.name == "posix":
        while _process_group_exists(process.pid) and time.monotonic() < grace_deadline:
            process.poll()
            time.sleep(_POLL_INTERVAL_SECONDS)
    else:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class FactoryProcessRunner:
    """Run one non-interactive child under a shared attempt budget."""

    def __init__(
        self,
        *,
        cancellation_token: CancellationToken | None = None,
        deadline: Deadline | None = None,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        if max_capture_bytes < 0:
            raise ValueError("max_capture_bytes must be non-negative")
        self._cancellation_token = cancellation_token or CancellationToken()
        self._deadline = deadline
        self._max_capture_bytes = max_capture_bytes
        self._termination_grace_seconds = _validated_timeout(termination_grace_seconds)

    def _preflight_reason(
        self,
        invocation_deadline: Deadline,
    ) -> TerminationReason | None:
        if self._cancellation_token.cancelled:
            return "cancelled"
        if invocation_deadline.expired or (
            self._deadline is not None and self._deadline.expired
        ):
            return "timed_out"
        return None

    def _wait_interval(self, invocation_deadline: Deadline) -> float:
        remaining = invocation_deadline.remaining_seconds()
        if self._deadline is not None:
            remaining = min(remaining, self._deadline.remaining_seconds())
        return min(_POLL_INTERVAL_SECONDS, max(0.0, remaining))

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        argv = _validated_command(command)
        invocation_deadline = Deadline.from_timeout(timeout_seconds)
        preflight_reason = self._preflight_reason(invocation_deadline)
        if preflight_reason is not None:
            return FactoryProcessResult(
                command=argv,
                returncode=None,
                stdout="",
                stderr="",
                termination_reason=preflight_reason,
            )

        subreaper_enabled = _enable_child_subreaper()
        supervisor_pid = os.getpid()
        preexisting_descendants = (
            _linux_descendants(supervisor_pid) if subreaper_enabled else set()
        )
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_capture = _BoundedCapture(self._max_capture_bytes)
        stderr_capture = _BoundedCapture(self._max_capture_bytes)
        readers = (
            threading.Thread(
                target=_drain_pipe,
                args=(process.stdout, stdout_capture),
                daemon=True,
                name=f"factory-stdout-{process.pid}",
            ),
            threading.Thread(
                target=_drain_pipe,
                args=(process.stderr, stderr_capture),
                daemon=True,
                name=f"factory-stderr-{process.pid}",
            ),
        )
        for reader in readers:
            reader.start()

        termination_reason: TerminationReason = "exited"
        while process.poll() is None:
            if self._cancellation_token.cancelled:
                termination_reason = "cancelled"
                _terminate_process_group(
                    process,
                    self._termination_grace_seconds,
                )
                break
            wait_interval = self._wait_interval(invocation_deadline)
            if wait_interval <= 0:
                termination_reason = "timed_out"
                _terminate_process_group(
                    process,
                    self._termination_grace_seconds,
                )
                break
            self._cancellation_token._wait(wait_interval)

        if termination_reason == "exited":
            returncode: int | None = process.wait()
            if os.name == "posix" and _process_group_exists(process.pid):
                _terminate_process_group(
                    process,
                    self._termination_grace_seconds,
                )
        else:
            returncode = None
        if subreaper_enabled:
            _terminate_new_descendants(
                parent_pid=supervisor_pid,
                preexisting_descendants=preexisting_descendants,
                grace_seconds=self._termination_grace_seconds,
            )

        for reader in readers:
            reader.join(timeout=self._termination_grace_seconds)
        stdout_bytes, stdout, stdout_truncated = stdout_capture.snapshot()
        stderr_bytes, stderr, stderr_truncated = stderr_capture.snapshot()
        return FactoryProcessResult(
            command=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            termination_reason=termination_reason,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )


__all__ = [
    "CancellationToken",
    "Deadline",
    "FactoryProcessResult",
    "FactoryProcessRunner",
]

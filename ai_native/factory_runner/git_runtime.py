"""Bounded, cancellable runner-owned Git inspection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_native.factory_runner.process import Deadline, FactoryProcessRunner
from ai_native.factory_runner.process_policy import resolve_trusted_command


class FactoryGitError(RuntimeError):
    pass


class FactoryGitCancelled(FactoryGitError):
    pass


class FactoryGitTimedOut(FactoryGitError):
    pass


@dataclass(frozen=True, slots=True)
class FactoryGitRuntime:
    workspace: Path
    output_dir: Path
    environment: dict[str, str]
    process_runner: FactoryProcessRunner
    deadline: Deadline

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        accepted_returncodes: frozenset[int],
    ) -> bytes:
        command = resolve_trusted_command(
            ("git", *arguments),
            environment=self.environment,
            prohibited_roots=(self.workspace, self.output_dir),
        )
        result = self.process_runner.run(
            command,
            cwd=self.workspace,
            environment=self.environment,
            timeout_seconds=self.deadline.remaining_seconds(),
        )
        if result.termination_reason == "cancelled":
            raise FactoryGitCancelled("runner-owned Git inspection was cancelled")
        if result.termination_reason == "timed_out":
            raise FactoryGitTimedOut("runner-owned Git inspection timed out")
        if (
            result.termination_reason != "exited"
            or result.returncode not in accepted_returncodes
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise FactoryGitError("runner-owned Git inspection failed")
        return result.stdout_bytes

    def run(self, *arguments: str) -> bytes:
        return self._run(tuple(arguments), accepted_returncodes=frozenset({0}))

    def run_diff(self, *arguments: str) -> bytes:
        """Run Git diff plumbing, where exit one means differences were found."""

        return self._run(tuple(arguments), accepted_returncodes=frozenset({0, 1}))


__all__ = [
    "FactoryGitCancelled",
    "FactoryGitError",
    "FactoryGitRuntime",
    "FactoryGitTimedOut",
]

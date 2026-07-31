"""Bounded, cancellable runner-owned Git operations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from ai_native.factory_runner.process import Deadline, FactoryProcessRunner
from ai_native.factory_runner.process_policy import resolve_trusted_command


_MAX_TRANSACTIONAL_PATCH_BYTES = 16 * 1024 * 1024
_ROLLBACK_TIMEOUT_SECONDS = 10.0
_CLEAN_STATUS_ARGUMENTS = (
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
    "--ignored=matching",
    "--no-renames",
)


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

    def _run_with(
        self,
        arguments: tuple[str, ...],
        *,
        accepted_returncodes: frozenset[int],
        process_runner: FactoryProcessRunner,
        timeout_seconds: float,
    ) -> bytes:
        command = resolve_trusted_command(
            ("git", *arguments),
            environment=self.environment,
            prohibited_roots=(self.workspace, self.output_dir),
        )
        result = process_runner.run(
            command,
            cwd=self.workspace,
            environment=self.environment,
            timeout_seconds=timeout_seconds,
        )
        if result.termination_reason == "cancelled":
            raise FactoryGitCancelled("runner-owned Git command was cancelled")
        if result.termination_reason == "timed_out":
            raise FactoryGitTimedOut("runner-owned Git command timed out")
        if (
            result.termination_reason != "exited"
            or result.returncode not in accepted_returncodes
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise FactoryGitError("runner-owned Git command failed")
        return result.stdout_bytes

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        accepted_returncodes: frozenset[int],
    ) -> bytes:
        return self._run_with(
            arguments,
            accepted_returncodes=accepted_returncodes,
            process_runner=self.process_runner,
            timeout_seconds=self.deadline.remaining_seconds(),
        )

    def _run_cleanup(self, *arguments: str) -> bytes:
        # Recovery must remain available after the attempt is cancelled or its
        # shared deadline expires, so exact cleanup commands get a fresh bound.
        cleanup_deadline = Deadline.from_timeout(_ROLLBACK_TIMEOUT_SECONDS)
        return self._run_with(
            tuple(arguments),
            accepted_returncodes=frozenset({0}),
            process_runner=FactoryProcessRunner(deadline=cleanup_deadline),
            timeout_seconds=cleanup_deadline.remaining_seconds(),
        )

    def _temporary_patch(self, patch: bytes) -> Path:
        temporary_root = Path(self.environment.get("TMPDIR", tempfile.gettempdir()))
        try:
            if temporary_root.is_symlink():
                raise FactoryGitError(
                    "runner-owned Git temporary directory must not be a symbolic link"
                )
            resolved_temporary_root = temporary_root.resolve(strict=True)
        except FactoryGitError:
            raise
        except (OSError, RuntimeError) as exc:
            raise FactoryGitError(
                "runner-owned Git temporary directory is unavailable"
            ) from exc
        if not resolved_temporary_root.is_dir():
            raise FactoryGitError("runner-owned Git temporary directory is unavailable")
        for prohibited_root in (self.workspace, self.output_dir):
            try:
                resolved_prohibited_root = prohibited_root.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise FactoryGitError(
                    "runner-owned Git boundary is unavailable"
                ) from exc
            if (
                resolved_temporary_root == resolved_prohibited_root
                or resolved_temporary_root.is_relative_to(resolved_prohibited_root)
            ):
                raise FactoryGitError(
                    "runner-owned Git temporary directory crosses a protected root"
                )

        descriptor: int | None = None
        patch_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix="factory-checkpoint-",
                suffix=".patch",
                dir=resolved_temporary_root,
            )
            patch_path = Path(raw_path)
            os.fchmod(descriptor, 0o600)
            view = memoryview(patch)
            written = 0
            while written < len(view):
                consumed = os.write(descriptor, view[written:])
                if consumed <= 0:
                    raise OSError("temporary checkpoint patch write made no progress")
                written += consumed
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            return patch_path
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if patch_path is not None:
                patch_path.unlink(missing_ok=True)
            raise FactoryGitError(
                "runner-owned Git patch could not be materialised"
            ) from exc

    def _rollback_clean_workspace(self) -> None:
        failures: list[FactoryGitError] = []
        for command in (
            ("reset", "--hard", "HEAD"),
            ("clean", "-ffdx"),
        ):
            try:
                self._run_cleanup(*command)
            except FactoryGitError as exc:
                failures.append(exc)
        try:
            dirty = self._run_cleanup(*_CLEAN_STATUS_ARGUMENTS)
        except FactoryGitError as exc:
            failures.append(exc)
            dirty = b"unknown"
        if failures or dirty:
            raise FactoryGitError(
                "runner-owned Git patch rollback could not restore the clean workspace"
            )

    def run(self, *arguments: str) -> bytes:
        return self._run(tuple(arguments), accepted_returncodes=frozenset({0}))

    def run_diff(self, *arguments: str) -> bytes:
        """Run Git diff plumbing, where exit one means differences were found."""

        return self._run(tuple(arguments), accepted_returncodes=frozenset({0, 1}))

    def apply_patch_transactionally(self, patch: bytes) -> None:
        """Apply a patch to an exclusively owned clean worktree or recover it."""

        if not isinstance(patch, bytes):
            raise TypeError("transactional Git patch must be bytes")
        if len(patch) > _MAX_TRANSACTIONAL_PATCH_BYTES:
            raise FactoryGitError("transactional Git patch exceeds the byte limit")
        if self.run(*_CLEAN_STATUS_ARGUMENTS):
            raise FactoryGitError("transactional Git patch requires a clean workspace")
        if not patch:
            return

        patch_path = self._temporary_patch(patch)
        applied = False
        try:
            patch_argument = str(patch_path)
            self.run(
                "apply",
                "--check",
                "--whitespace=nowarn",
                "--",
                patch_argument,
            )
            try:
                self.run(
                    "apply",
                    "--whitespace=nowarn",
                    "--",
                    patch_argument,
                )
            except FactoryGitError as apply_error:
                try:
                    self._rollback_clean_workspace()
                except FactoryGitError as rollback_error:
                    raise rollback_error from apply_error
                raise
            applied = True
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = FactoryGitError("runner-owned Git patch cleanup failed")
                if applied:
                    try:
                        self._rollback_clean_workspace()
                    except FactoryGitError as rollback_error:
                        raise rollback_error from cleanup_error
                raise cleanup_error from exc


__all__ = [
    "FactoryGitCancelled",
    "FactoryGitError",
    "FactoryGitRuntime",
    "FactoryGitTimedOut",
]

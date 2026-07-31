"""Bounded, cancellable runner-owned Git operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from ai_native.factory_runner.process import Deadline, FactoryProcessRunner
from ai_native.factory_runner.process_policy import resolve_trusted_command


_MAX_TRANSACTIONAL_PATCH_BYTES = 16 * 1024 * 1024
_MAX_WORKTREE_METADATA_ENTRIES = 100_000
_ROLLBACK_TIMEOUT_SECONDS = 10.0
_NUMSTAT_COUNTERS = frozenset({b"-"})
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
class _WorktreeMetadata:
    relative_path: str
    file_type: int
    permissions: int
    access_time_ns: int
    modification_time_ns: int


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

    def _capture_worktree_metadata(self) -> tuple[_WorktreeMetadata, ...]:
        entries: list[_WorktreeMetadata] = []
        paths = [self.workspace]
        walk_errors: list[OSError] = []
        try:
            for root, directories, filenames in os.walk(
                self.workspace,
                topdown=True,
                onerror=walk_errors.append,
                followlinks=False,
            ):
                if Path(root) == self.workspace:
                    directories[:] = [name for name in directories if name != ".git"]
                    filenames = [name for name in filenames if name != ".git"]
                paths.extend(Path(root) / name for name in (*directories, *filenames))
                if len(paths) > _MAX_WORKTREE_METADATA_ENTRIES:
                    raise FactoryGitError(
                        "transactional Git worktree exceeds the metadata entry limit"
                    )
            if walk_errors:
                raise FactoryGitError(
                    "transactional Git worktree metadata is unavailable"
                )
            for path in sorted(paths, key=lambda item: item.as_posix()):
                metadata = path.lstat()
                relative = (
                    "."
                    if path == self.workspace
                    else path.relative_to(self.workspace).as_posix()
                )
                entries.append(
                    _WorktreeMetadata(
                        relative_path=relative,
                        file_type=stat.S_IFMT(metadata.st_mode),
                        permissions=stat.S_IMODE(metadata.st_mode),
                        access_time_ns=metadata.st_atime_ns,
                        modification_time_ns=metadata.st_mtime_ns,
                    )
                )
        except FactoryGitError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise FactoryGitError(
                "transactional Git worktree metadata is unavailable"
            ) from exc
        return tuple(entries)

    def _restore_worktree_metadata(
        self,
        snapshot: tuple[_WorktreeMetadata, ...],
    ) -> None:
        files = tuple(entry for entry in snapshot if entry.file_type != stat.S_IFDIR)
        directories = tuple(
            sorted(
                (entry for entry in snapshot if entry.file_type == stat.S_IFDIR),
                key=lambda entry: (
                    -len(PurePosixPath(entry.relative_path).parts),
                    entry.relative_path,
                ),
            )
        )
        try:
            for entry in (*files, *directories):
                path = (
                    self.workspace
                    if entry.relative_path == "."
                    else self.workspace / entry.relative_path
                )
                metadata = path.lstat()
                if stat.S_IFMT(metadata.st_mode) != entry.file_type:
                    raise FactoryGitError(
                        "transactional Git rollback changed a worktree entry type"
                    )
                if entry.file_type != stat.S_IFLNK:
                    os.chmod(
                        path,
                        entry.permissions,
                        follow_symlinks=False,
                    )
                os.utime(
                    path,
                    ns=(entry.access_time_ns, entry.modification_time_ns),
                    follow_symlinks=False,
                )
        except FactoryGitError:
            raise
        except (NotImplementedError, OSError, ValueError) as exc:
            raise FactoryGitError(
                "transactional Git rollback could not restore worktree metadata"
            ) from exc

    def _rollback_clean_workspace(
        self,
        *,
        metadata_snapshot: tuple[_WorktreeMetadata, ...],
    ) -> None:
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
        try:
            self._restore_worktree_metadata(metadata_snapshot)
        except FactoryGitError as exc:
            failures.append(exc)
        if failures or dirty:
            raise FactoryGitError(
                "runner-owned Git patch rollback could not restore the clean workspace"
            )

    @staticmethod
    def _patch_paths_from_numstat(content: bytes) -> tuple[str, ...]:
        if not isinstance(content, bytes) or not content.endswith(b"\0"):
            raise FactoryGitError("runner-owned Git patch path output is invalid")
        records = content.split(b"\0")
        records.pop()
        paths: list[str] = []
        for record in records:
            try:
                added, deleted, raw_path = record.split(b"\t", 2)
            except ValueError as exc:
                raise FactoryGitError(
                    "runner-owned Git patch path output is invalid"
                ) from exc
            if (
                not added
                or not deleted
                or (
                    added not in _NUMSTAT_COUNTERS
                    and not all(character in b"0123456789" for character in added)
                )
                or (
                    deleted not in _NUMSTAT_COUNTERS
                    and not all(character in b"0123456789" for character in deleted)
                )
            ):
                raise FactoryGitError(
                    "runner-owned Git patch path counters are invalid"
                )
            if not raw_path:
                raise FactoryGitError(
                    "runner-owned Git checkpoint patches may not encode renames or copies"
                )
            try:
                path = raw_path.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise FactoryGitError(
                    "runner-owned Git patch path is not portable UTF-8"
                ) from exc
            pure_path = PurePosixPath(path)
            if (
                not path
                or "\\" in path
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in path
                )
                or pure_path.is_absolute()
                or pure_path.as_posix() != path
                or any(part in {"", ".", "..", ".git"} for part in pure_path.parts)
            ):
                raise FactoryGitError("runner-owned Git patch path is unsafe")
            paths.append(path)
        if not paths or len(paths) != len(set(paths)):
            raise FactoryGitError(
                "runner-owned Git patch paths must be non-empty and unique"
            )
        return tuple(paths)

    def inspect_patch_paths(self, patch: bytes) -> tuple[str, ...]:
        """Return exact no-rename patch paths without mutating the worktree."""

        if not isinstance(patch, bytes):
            raise TypeError("transactional Git patch must be bytes")
        if not patch:
            raise FactoryGitError("transactional Git patch must not be empty")
        if len(patch) > _MAX_TRANSACTIONAL_PATCH_BYTES:
            raise FactoryGitError("transactional Git patch exceeds the byte limit")
        if self.run(*_CLEAN_STATUS_ARGUMENTS):
            raise FactoryGitError("patch inspection requires a clean workspace")

        patch_path = self._temporary_patch(patch)
        try:
            patch_argument = str(patch_path)
            self.run(
                "apply",
                "--check",
                "--whitespace=nowarn",
                "--",
                patch_argument,
            )
            summary = self.run(
                "apply",
                "--summary",
                "--",
                patch_argument,
            )
            if any(
                line.startswith((b" rename ", b" copy "))
                for line in summary.splitlines()
            ):
                raise FactoryGitError(
                    "runner-owned Git checkpoint patches may not encode renames or copies"
                )
            numstat = self.run(
                "apply",
                "--numstat",
                "-z",
                "--whitespace=nowarn",
                "--",
                patch_argument,
            )
            return self._patch_paths_from_numstat(numstat)
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError as exc:
                raise FactoryGitError("runner-owned Git patch cleanup failed") from exc

    def run(self, *arguments: str) -> bytes:
        return self._run(tuple(arguments), accepted_returncodes=frozenset({0}))

    def run_diff(self, *arguments: str) -> bytes:
        """Run Git diff plumbing, where exit one means differences were found."""

        return self._run(tuple(arguments), accepted_returncodes=frozenset({0, 1}))

    def apply_patch_transactionally(
        self,
        patch: bytes,
        *,
        postcondition: Callable[[], None] | None = None,
    ) -> None:
        """Apply a patch to an exclusively owned clean worktree or recover it."""

        if not isinstance(patch, bytes):
            raise TypeError("transactional Git patch must be bytes")
        if postcondition is not None and not callable(postcondition):
            raise TypeError("transactional Git postcondition must be callable or null")
        if len(patch) > _MAX_TRANSACTIONAL_PATCH_BYTES:
            raise FactoryGitError("transactional Git patch exceeds the byte limit")
        if self.run(*_CLEAN_STATUS_ARGUMENTS):
            raise FactoryGitError("transactional Git patch requires a clean workspace")
        if not patch:
            return

        patch_path = self._temporary_patch(patch)
        applied = False
        metadata_snapshot: tuple[_WorktreeMetadata, ...] | None = None
        try:
            patch_argument = str(patch_path)
            self.run(
                "apply",
                "--check",
                "--whitespace=nowarn",
                "--",
                patch_argument,
            )
            metadata_snapshot = self._capture_worktree_metadata()
            try:
                self.run(
                    "apply",
                    "--whitespace=nowarn",
                    "--",
                    patch_argument,
                )
            except FactoryGitError as apply_error:
                try:
                    self._rollback_clean_workspace(
                        metadata_snapshot=metadata_snapshot,
                    )
                except FactoryGitError as rollback_error:
                    raise rollback_error from apply_error
                raise
            applied = True
            if postcondition is not None:
                try:
                    postcondition()
                except Exception as postcondition_error:
                    try:
                        self._rollback_clean_workspace(
                            metadata_snapshot=metadata_snapshot,
                        )
                    except FactoryGitError as rollback_error:
                        raise rollback_error from postcondition_error
                    applied = False
                    raise
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = FactoryGitError("runner-owned Git patch cleanup failed")
                if applied:
                    if metadata_snapshot is None:
                        raise cleanup_error from exc
                    try:
                        self._rollback_clean_workspace(
                            metadata_snapshot=metadata_snapshot,
                        )
                    except FactoryGitError as rollback_error:
                        raise rollback_error from cleanup_error
                raise cleanup_error from exc


__all__ = [
    "FactoryGitCancelled",
    "FactoryGitError",
    "FactoryGitRuntime",
    "FactoryGitTimedOut",
]

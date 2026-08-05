"""Bounded, cancellable runner-owned Git operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from ai_native.factory_runner.process import (
    Deadline,
    FactoryProcessIsolationError,
    FactoryProcessRunner,
)
from ai_native.factory_runner.process_policy import resolve_trusted_command


_MAX_TRANSACTIONAL_PATCH_BYTES = 16 * 1024 * 1024
_MAX_PRIVATE_INDEX_BYTES = 16 * 1024 * 1024
_MAX_WORKTREE_METADATA_ENTRIES = 100_000
_PRIVATE_INDEX_COPY_CHUNK_BYTES = 1024 * 1024
_PRIVATE_INDEX_PREFIX = "factory-git-index-"
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


def _same_index_metadata(
    expected: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return (
        expected.st_dev,
        expected.st_ino,
        stat.S_IFMT(expected.st_mode),
        stat.S_IMODE(expected.st_mode),
        expected.st_nlink,
        expected.st_uid,
        expected.st_gid,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    ) == (
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
        stat.S_IMODE(observed.st_mode),
        observed.st_nlink,
        observed.st_uid,
        observed.st_gid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _validated_private_index_root(
    private_root: Path,
    *,
    protected_roots: tuple[Path, Path],
) -> Path:
    candidate_root = Path(private_root)
    try:
        lexical_root = Path(os.path.abspath(os.fspath(candidate_root)))
        if candidate_root.is_symlink():
            raise FactoryGitError("runner-owned Git private index root is unsafe")
        resolved_root = candidate_root.resolve(strict=True)
        root_metadata = candidate_root.stat(follow_symlinks=False)
    except FactoryGitError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise FactoryGitError(
            "runner-owned Git private index root is unavailable"
        ) from exc
    if (
        resolved_root != lexical_root
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or root_metadata.st_uid != os.geteuid()
    ):
        raise FactoryGitError("runner-owned Git private index root is unsafe")

    for protected_root in protected_roots:
        try:
            resolved_protected_root = protected_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FactoryGitError("runner-owned Git boundary is unavailable") from exc
        if (
            resolved_root == resolved_protected_root
            or resolved_root.is_relative_to(resolved_protected_root)
            or resolved_protected_root.is_relative_to(resolved_root)
        ):
            raise FactoryGitError(
                "runner-owned Git private index root crosses a protected root"
            )
    return resolved_root


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
    private_index_root: Path | None = None

    def with_ephemeral_private_indexes(
        self,
        *,
        private_root: Path,
    ) -> FactoryGitRuntime:
        """Use a fresh private index for each runner-owned Git subprocess."""

        if "GIT_INDEX_FILE" in self.environment or self.private_index_root is not None:
            raise FactoryGitError(
                "runner-owned Git environment already declares an index"
            )
        resolved_root = _validated_private_index_root(
            private_root,
            protected_roots=(self.workspace, self.output_dir),
        )
        return replace(self, private_index_root=resolved_root)

    def _materialize_private_index(self) -> tuple[Path, dict[str, str]]:
        if self.private_index_root is None:
            raise FactoryGitError("runner-owned Git private index root is unavailable")
        resolved_root = _validated_private_index_root(
            self.private_index_root,
            protected_roots=(self.workspace, self.output_dir),
        )
        try:
            config_count = int(self.environment.get("GIT_CONFIG_COUNT", "0"))
        except ValueError as exc:
            raise FactoryGitError("runner-owned Git configuration is invalid") from exc
        if (
            "GIT_INDEX_FILE" in self.environment
            or not 0 <= config_count <= 1024
            or f"GIT_CONFIG_KEY_{config_count}" in self.environment
            or f"GIT_CONFIG_VALUE_{config_count}" in self.environment
        ):
            raise FactoryGitError("runner-owned Git configuration is invalid")

        git_dir = self.workspace / ".git"
        source_index = git_dir / "index"
        index_lock = git_dir / "index.lock"
        try:
            if git_dir.is_symlink() or source_index.is_symlink():
                raise FactoryGitError("runner-owned Git repository index is unsafe")
            git_dir_metadata = git_dir.stat(follow_symlinks=False)
            source_metadata = source_index.stat(follow_symlinks=False)
            if index_lock.exists() or index_lock.is_symlink():
                raise FactoryGitError("runner-owned Git repository index is mutating")
        except FactoryGitError:
            raise
        except (OSError, RuntimeError) as exc:
            raise FactoryGitError(
                "runner-owned Git repository index is unavailable"
            ) from exc
        if not stat.S_ISDIR(git_dir_metadata.st_mode) or (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or source_metadata.st_size <= 0
            or source_metadata.st_size > _MAX_PRIVATE_INDEX_BYTES
            or stat.S_IMODE(source_metadata.st_mode) & 0o022
        ):
            raise FactoryGitError("runner-owned Git repository index is unsafe")

        source_descriptor: int | None = None
        destination_descriptor: int | None = None
        destination_index: Path | None = None
        copy_complete = False
        try:
            source_flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                source_flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                source_flags |= os.O_NOFOLLOW
            source_descriptor = os.open(source_index, source_flags)
            opened_source_metadata = os.fstat(source_descriptor)
            if not _same_index_metadata(source_metadata, opened_source_metadata):
                raise FactoryGitError(
                    "runner-owned Git repository index changed during private copy"
                )

            destination_descriptor, raw_destination = tempfile.mkstemp(
                prefix=_PRIVATE_INDEX_PREFIX,
                dir=resolved_root,
            )
            destination_index = Path(raw_destination)
            os.fchmod(destination_descriptor, 0o600)
            copied_bytes = 0
            while True:
                chunk = os.read(source_descriptor, _PRIVATE_INDEX_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied_bytes += len(chunk)
                if copied_bytes > source_metadata.st_size:
                    raise FactoryGitError(
                        "runner-owned Git repository index changed during private copy"
                    )
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    consumed = os.write(destination_descriptor, view[written:])
                    if consumed <= 0:
                        raise OSError("private index write made no progress")
                    written += consumed
            if copied_bytes != source_metadata.st_size:
                raise FactoryGitError(
                    "runner-owned Git repository index changed during private copy"
                )
            os.fsync(destination_descriptor)

            final_source_metadata = os.fstat(source_descriptor)
            path_source_metadata = source_index.stat(follow_symlinks=False)
            destination_metadata = os.fstat(destination_descriptor)
            if (
                not _same_index_metadata(source_metadata, final_source_metadata)
                or not _same_index_metadata(source_metadata, path_source_metadata)
                or index_lock.exists()
            ):
                raise FactoryGitError(
                    "runner-owned Git repository index changed during private copy"
                )
            if (
                not stat.S_ISREG(destination_metadata.st_mode)
                or stat.S_IMODE(destination_metadata.st_mode) != 0o600
                or destination_metadata.st_nlink != 1
                or destination_metadata.st_size != source_metadata.st_size
            ):
                raise FactoryGitError("runner-owned Git private index copy is unsafe")
            copy_complete = True
        except FactoryGitError:
            raise
        except OSError as exc:
            raise FactoryGitError(
                "runner-owned Git private index could not be materialised"
            ) from exc
        finally:
            cleanup_errors: list[OSError] = []
            for descriptor in (source_descriptor, destination_descriptor):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_errors.append(exc)
            if destination_index is not None and (not copy_complete or cleanup_errors):
                try:
                    destination_index.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise FactoryGitError(
                    "runner-owned Git private index cleanup failed"
                ) from cleanup_errors[0]

        assert destination_index is not None
        isolated_environment = dict(self.environment)
        isolated_environment["GIT_INDEX_FILE"] = str(destination_index)
        isolated_environment["GIT_CONFIG_COUNT"] = str(config_count + 1)
        isolated_environment[f"GIT_CONFIG_KEY_{config_count}"] = "core.sharedRepository"
        isolated_environment[f"GIT_CONFIG_VALUE_{config_count}"] = "0600"
        return destination_index, isolated_environment

    @staticmethod
    def _remove_private_index(index_path: Path) -> None:
        unsafe = False
        index_found = False
        cleanup_errors: list[OSError] = []
        for candidate in (Path(f"{index_path}.lock"), index_path):
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                cleanup_errors.append(exc)
            else:
                if candidate == index_path:
                    index_found = True
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    unsafe = True
            try:
                candidate.unlink()
            except FileNotFoundError:
                unsafe = True
            except OSError as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise FactoryGitError(
                "runner-owned Git private index cleanup failed"
            ) from cleanup_errors[0]
        if unsafe or not index_found:
            raise FactoryGitError("runner-owned Git private index cleanup was unsafe")

    def _run_with(
        self,
        arguments: tuple[str, ...],
        *,
        accepted_returncodes: frozenset[int],
        process_runner: FactoryProcessRunner,
        timeout_seconds: float,
    ) -> bytes:
        try:
            with process_runner.exclusive_operation():
                private_index: Path | None = None
                environment = self.environment
                if self.private_index_root is not None:
                    private_index, environment = self._materialize_private_index()
                try:
                    command = resolve_trusted_command(
                        ("git", *arguments),
                        environment=environment,
                        prohibited_roots=(self.workspace, self.output_dir),
                    )
                    result = process_runner.run(
                        command,
                        cwd=self.workspace,
                        environment=environment,
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    if private_index is not None:
                        self._remove_private_index(private_index)
        except FactoryProcessIsolationError as exc:
            raise FactoryGitError(
                "runner-owned Git process isolation is unavailable"
            ) from exc
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

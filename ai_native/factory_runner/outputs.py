"""Minimal content-addressed terminal output support for AN-02.

AN-03 extends this writer with append-only events, checkpoints, complete
manifests, redaction, recovery, and output-size budgets.  The AN-02 surface
still writes genuine schema-valid terminal documents and writes
``completion.json`` last.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import threading
from typing import Any, Literal
import uuid

from ai_native import __version__
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    RepositoryIdentity,
    RunIdentity,
    RunnerBuildIdentity,
)
from ai_native.factory_runner.contracts.run_result import RunOutcome, RunResult
from ai_native.factory_runner.contracts.terminal_output import (
    CompletionManifest,
    ProtocolManifest,
)
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.protocol import contract_document_digest
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner


JSON_MEDIA_TYPE = "application/json"
OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"
EMPTY_DIGEST = sha256_digest(b"")
_MAX_OUTPUT_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_SNAPSHOT_ENTRIES = 20_000
_MAX_PROTOCOL_MANIFEST_ARTIFACTS = 20_000


class OutputLimitError(FactoryPolicyViolation):
    """A producer attempted to exceed its admitted durable output budget."""


@dataclass(frozen=True, slots=True)
class _OutputSnapshotEntry:
    path: str
    kind: Literal["directory", "file"]
    mode: int
    content: bytes | None


@dataclass(frozen=True, slots=True)
class OutputTreeSnapshot:
    root: Path
    device: int
    inode: int
    entries: tuple[_OutputSnapshotEntry, ...]


class StagedArtifact:
    """Writer-owned append sink that is invisible until atomic finalization."""

    __slots__ = (
        "_byte_size",
        "_digest",
        "_finalization_reserve_bytes",
        "_lock",
        "_media_type",
        "_parent_fd",
        "_relative_path",
        "_staging_name",
        "_state",
        "_target_name",
        "_writer",
    )

    def __init__(
        self,
        *,
        writer: OutputWriter,
        parent_fd: int,
        staging_name: str,
        target_name: str,
        relative_path: str,
        media_type: str,
        finalization_reserve_bytes: int,
    ) -> None:
        self._writer = writer
        self._parent_fd = parent_fd
        self._staging_name = staging_name
        self._target_name = target_name
        self._relative_path = relative_path
        self._media_type = media_type
        self._finalization_reserve_bytes = finalization_reserve_bytes
        self._byte_size = 0
        self._digest = hashlib.sha256()
        self._state: Literal["open", "finalized", "aborted"] = "open"
        self._lock = threading.RLock()

    def _ensure_open(self) -> None:
        if self._state == "finalized":
            raise RuntimeError("staged artifact is already finalized")
        if self._state == "aborted":
            raise RuntimeError("staged artifact is already aborted")

    def _close_parent(self) -> None:
        if self._parent_fd >= 0:
            parent_fd = self._parent_fd
            self._parent_fd = -1
            try:
                os.close(parent_fd)
            except OSError:
                pass

    def append(self, content: bytes) -> None:
        """Append and durably flush bytes without publishing the final path."""

        with self._lock:
            self._ensure_open()
            self._writer._append_staged_artifact(self, content)

    def finalize(self) -> ArtifactReference:
        """Validate, atomically publish, and register the completed artifact."""

        with self._lock:
            self._ensure_open()
            reference = self._writer._finalize_staged_artifact(self)
            self._state = "finalized"
            self._close_parent()
            return reference

    def abort(self) -> None:
        """Discard an unpublished staging file without following replacements."""

        with self._lock:
            self._ensure_open()
            self._writer._abort_staged_artifact(self)
            self._state = "aborted"
            self._close_parent()

    def __del__(self) -> None:
        if getattr(self, "_state", None) != "open":
            return
        try:
            self.abort()
        except Exception:
            try:
                self._close_parent()
            except Exception:
                pass


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory(parent_fd: int, name: str, *, description: str) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{description} contains a symbolic link") from exc
        raise


def validate_output_root(path: Path) -> Path:
    """Create or admit one empty output directory without following symlinks."""

    if not path.is_absolute():
        raise ValueError("output directory must be absolute")
    if path == Path(path.anchor):
        raise ValueError("output directory may not be a filesystem root")
    if ".." in path.parts:
        raise ValueError("output directory must be normalised")

    components = path.parts[1:]
    current_fd = os.open(path.anchor, _directory_open_flags())
    try:
        for index, component in enumerate(components):
            final_component = index == len(components) - 1
            try:
                next_fd = _open_directory(
                    current_fd,
                    component,
                    description="output directory",
                )
            except FileNotFoundError:
                if not final_component:
                    raise ValueError("output directory parent must exist") from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(current_fd)
                next_fd = _open_directory(
                    current_fd,
                    component,
                    description="output directory",
                )
            os.close(current_fd)
            current_fd = next_fd

        if os.listdir(current_fd):
            raise ValueError("output directory must be empty")
    finally:
        os.close(current_fd)
    return path


def capture_output_tree(root: Path) -> OutputTreeSnapshot:
    """Capture bounded writer-owned output so child tampering can be repaired."""

    metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("output root identity is invalid")
    entries: list[_OutputSnapshotEntry] = []
    consumed = 0
    pending = sorted(root.iterdir(), key=lambda item: item.name, reverse=True)
    while pending:
        path = pending.pop()
        if len(entries) >= _MAX_OUTPUT_SNAPSHOT_ENTRIES:
            raise ValueError("output snapshot exceeds the entry limit")
        path_metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path_metadata.st_mode)
        if stat.S_ISDIR(path_metadata.st_mode):
            entries.append(_OutputSnapshotEntry(relative, "directory", mode, None))
            pending.extend(
                sorted(path.iterdir(), key=lambda item: item.name, reverse=True)
            )
        elif stat.S_ISREG(path_metadata.st_mode):
            if path_metadata.st_nlink != 1:
                raise ValueError("output snapshot contains a hard link alias")
            if path_metadata.st_size > _MAX_OUTPUT_SNAPSHOT_BYTES - consumed:
                raise ValueError("output snapshot exceeds the byte limit")
            content = path.read_bytes()
            consumed += len(content)
            entries.append(_OutputSnapshotEntry(relative, "file", mode, content))
        else:
            raise ValueError("output snapshot contains a link or special file")
    return OutputTreeSnapshot(
        root=root,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        entries=tuple(sorted(entries, key=lambda entry: entry.path)),
    )


def _remove_output_entry(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        for child in path.iterdir():
            _remove_output_entry(child)
        path.rmdir()
    else:
        path.unlink()


def restore_output_tree(snapshot: OutputTreeSnapshot) -> None:
    """Restore a previously captured output tree after a supervised child exits."""

    root_metadata = snapshot.root.lstat()
    if (
        snapshot.root.is_symlink()
        or root_metadata.st_dev != snapshot.device
        or root_metadata.st_ino != snapshot.inode
    ):
        raise ValueError("output root identity changed")
    for child in tuple(snapshot.root.iterdir()):
        _remove_output_entry(child)
    for entry in sorted(
        (item for item in snapshot.entries if item.kind == "directory"),
        key=lambda item: (item.path.count("/"), item.path),
    ):
        target = snapshot.root / entry.path
        target.mkdir(mode=entry.mode)
        target.chmod(entry.mode)
    for entry in (item for item in snapshot.entries if item.kind == "file"):
        assert entry.content is not None
        target = snapshot.root / entry.path
        target.write_bytes(entry.content)
        target.chmod(entry.mode)


def enforce_output_tree_unchanged(snapshot: OutputTreeSnapshot) -> None:
    """Repair and reject any child mutation of writer-owned protocol output."""

    try:
        current = capture_output_tree(snapshot.root)
    except (OSError, ValueError) as exc:
        try:
            restore_output_tree(snapshot)
        except (OSError, ValueError):
            pass
        raise ValueError("child process corrupted protocol output") from exc
    if current != snapshot:
        restore_output_tree(snapshot)
        raise ValueError("child process modified protocol output")


class OutputWriter:
    """Atomically write bounded artifacts beneath one validated root."""

    def __init__(
        self,
        root: Path,
        *,
        secret_scanner: SecretScanner | None = None,
        max_artifact_bytes: int | None = None,
        max_total_bytes: int | None = None,
        finalization_reserve_bytes: int = 0,
    ):
        for name, value in (
            ("max_artifact_bytes", max_artifact_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if (
            isinstance(finalization_reserve_bytes, bool)
            or not isinstance(finalization_reserve_bytes, int)
            or finalization_reserve_bytes < 0
            or (max_total_bytes is None and finalization_reserve_bytes != 0)
            or (
                max_total_bytes is not None
                and finalization_reserve_bytes > max_total_bytes
            )
        ):
            raise ValueError(
                "finalization_reserve_bytes must fit within the total limit"
            )
        if secret_scanner is not None and not isinstance(
            secret_scanner,
            SecretScanner,
        ):
            raise TypeError("secret_scanner must be a SecretScanner or null")
        self.root = root
        self._root_fd = os.open(root, _directory_open_flags())
        self._manifest: list[ArtifactReference] = []
        self._secret_scanner = secret_scanner or SecretScanner(SecretPolicy())
        self._max_artifact_bytes = max_artifact_bytes
        self._hard_max_total_bytes = max_total_bytes
        self._max_total_bytes = (
            max_total_bytes - finalization_reserve_bytes
            if max_total_bytes is not None
            else None
        )
        self._total_bytes = 0
        self._staged_total_bytes = 0
        self._finalizing = False
        self._sealed = False
        self._poisoned = False
        self._protocol_manifest_reference: ArtifactReference | None = None
        self._write_lock = threading.RLock()

    def __del__(self) -> None:
        root_fd = getattr(self, "_root_fd", -1)
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
            self._root_fd = -1

    @property
    def sealed(self) -> bool:
        with self._write_lock:
            return self._sealed

    def _ensure_writable(self) -> None:
        if self._poisoned:
            raise RuntimeError("output writer is poisoned by uncertain durability")
        if self._sealed:
            raise RuntimeError("output writer is finalized by completion.json")

    def _require_manifest_capacity(self, additions: int) -> None:
        if (
            isinstance(additions, bool)
            or not isinstance(additions, int)
            or additions < 0
        ):
            raise ValueError("manifest additions must be a non-negative integer")
        limit = (
            _MAX_PROTOCOL_MANIFEST_ARTIFACTS
            if self._finalizing
            else _MAX_PROTOCOL_MANIFEST_ARTIFACTS - 1
        )
        if additions > limit - len(self._manifest):
            raise OutputLimitError(
                "protocol manifest artifact count exceeds the manifest limit"
            )

    def _require_capacity(
        self,
        byte_size: int,
        *,
        artifact_size: int | None = None,
    ) -> None:
        selected_artifact_size = byte_size if artifact_size is None else artifact_size
        if (
            self._max_artifact_bytes is not None
            and selected_artifact_size > self._max_artifact_bytes
        ):
            raise OutputLimitError("artifact size exceeds the artifact limit")
        if (
            self._max_total_bytes is not None
            and byte_size
            > self._max_total_bytes - self._total_bytes - self._staged_total_bytes
        ):
            raise OutputLimitError("total output size exceeds the total limit")

    def begin_finalization(self) -> None:
        """Release the terminal reserve exactly when no producer work remains."""

        with self._write_lock:
            self._ensure_writable()
            if self._finalizing:
                return
            self._max_total_bytes = self._hard_max_total_bytes
            self._finalizing = True

    def preflight_external_artifacts(
        self,
        references: Sequence[ArtifactReference],
    ) -> tuple[ArtifactReference, ...]:
        """Check a complete local atomic bundle before it touches the output."""

        if isinstance(references, str | bytes | bytearray):
            raise TypeError("external artifact references must be a sequence")
        try:
            validated = tuple(
                ArtifactReference.model_validate(reference.model_dump(mode="json"))
                for reference in references
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("external artifact reference is invalid") from exc
        paths = tuple(reference.path for reference in validated)
        with self._write_lock:
            self._ensure_writable()
            self._require_manifest_capacity(len(validated))
            if len(paths) != len(set(paths)) or any(
                reference.path == recorded.path
                for reference in validated
                for recorded in self._manifest
            ):
                raise ValueError("external artifact path is already recorded")
            for reference in validated:
                self._require_capacity(
                    0,
                    artifact_size=reference.byte_size,
                )
            self._require_capacity(
                sum(reference.byte_size for reference in validated),
                artifact_size=0,
            )
        return validated

    @staticmethod
    def _external_bundle_directory(
        references: Sequence[ArtifactReference],
    ) -> PurePosixPath:
        if not references:
            raise ValueError("external bundle requires at least one artifact")
        parent_parts = [
            PurePosixPath(reference.path).parent.parts for reference in references
        ]
        common = list(parent_parts[0])
        for parts in parent_parts[1:]:
            shared: list[str] = []
            for left, right in zip(common, parts, strict=False):
                if left != right:
                    break
                shared.append(left)
            common = shared
        if not common:
            raise ValueError(
                "external bundle artifacts require one non-root common directory"
            )
        return PurePosixPath(*common)

    @staticmethod
    def _require_bundle_directory(metadata: os.stat_result) -> None:
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("external bundle directory may not be a symbolic link")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("external bundle path must be a directory")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("external bundle directory has an unsafe mode")

    @contextmanager
    def _bundle_parent_directory(
        self,
        bundle_fd: int,
        relative_path: PurePosixPath,
    ) -> Iterator[tuple[int, str]]:
        if not relative_path.parts or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ValueError("external bundle artifact path is invalid")
        current_fd = os.dup(bundle_fd)
        try:
            for component in relative_path.parts[:-1]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
                next_fd = _open_directory(
                    current_fd,
                    component,
                    description="external bundle path",
                )
                try:
                    self._require_bundle_directory(os.fstat(next_fd))
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd, relative_path.parts[-1]
        finally:
            os.close(current_fd)

    def _write_bundle_file(
        self,
        bundle_fd: int,
        relative_path: PurePosixPath,
        content: bytes,
    ) -> None:
        with self._bundle_parent_directory(bundle_fd, relative_path) as (
            parent_fd,
            target_name,
        ):
            descriptor = os.open(
                target_name,
                (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                ),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                before = os.fstat(descriptor)
                self._require_regular_staging_file(before)
                view = memoryview(content)
                written = 0
                while written < len(view):
                    consumed = os.write(descriptor, view[written:])
                    if consumed <= 0:
                        raise OSError("external bundle write made no progress")
                    written += consumed
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                self._require_regular_staging_file(after)
                if self._path_identity(before) != self._path_identity(
                    after
                ) or after.st_size != len(content):
                    raise ValueError("external bundle artifact changed while writing")
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)

    def _verify_bundle_file(
        self,
        bundle_fd: int,
        relative_path: PurePosixPath,
        reference: ArtifactReference,
    ) -> None:
        with self._bundle_parent_directory(bundle_fd, relative_path) as (
            parent_fd,
            target_name,
        ):
            path_metadata = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            self._require_regular_staging_file(path_metadata)
            descriptor = os.open(
                target_name,
                (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                ),
                dir_fd=parent_fd,
            )
            digest = hashlib.sha256()
            consumed = 0
            try:
                before = os.fstat(descriptor)
                self._require_regular_staging_file(before)
                if self._path_identity(before) != self._path_identity(path_metadata):
                    raise ValueError("external bundle artifact changed while opening")
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    consumed += len(chunk)
                    if consumed > reference.byte_size:
                        raise ValueError("external bundle artifact size mismatch")
                    digest.update(chunk)
                after = os.fstat(descriptor)
                self._require_regular_staging_file(after)
            finally:
                os.close(descriptor)
            if (
                self._path_identity(before) != self._path_identity(after)
                or consumed != reference.byte_size
                or f"sha256:{digest.hexdigest()}" != reference.digest
            ):
                raise ValueError("external bundle artifact size or digest mismatch")

    @classmethod
    def _remove_bundle_directory(cls, parent_fd: int, target_name: str) -> None:
        try:
            metadata = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            os.unlink(target_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return
        descriptor = _open_directory(
            parent_fd,
            target_name,
            description="external bundle staging",
        )
        try:
            for child_name in os.listdir(descriptor):
                child_metadata = os.stat(
                    child_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(child_metadata.st_mode) and not stat.S_ISLNK(
                    child_metadata.st_mode
                ):
                    cls._remove_bundle_directory(descriptor, child_name)
                else:
                    os.unlink(child_name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rmdir(target_name, dir_fd=parent_fd)
        os.fsync(parent_fd)

    def publish_external_bundle(
        self,
        references: Sequence[ArtifactReference],
        contents: Mapping[str, bytes],
    ) -> tuple[ArtifactReference, ...]:
        """Validate, atomically publish, and account for one immutable directory."""

        if isinstance(references, str | bytes | bytearray):
            raise TypeError("external bundle references must be a sequence")
        if not isinstance(contents, Mapping):
            raise TypeError("external bundle contents must be a mapping")

        with self._write_lock:
            self._ensure_writable()
            validated = self.preflight_external_artifacts(references)
            try:
                detached = dict(contents)
            except (TypeError, ValueError) as exc:
                raise ValueError("external bundle contents are invalid") from exc
            if any(
                not isinstance(path, str) or not isinstance(content, bytes)
                for path, content in detached.items()
            ):
                raise TypeError("external bundle contents must map paths to bytes")

            expected_paths = {reference.path for reference in validated}
            if set(detached) != expected_paths:
                raise ValueError(
                    "external bundle contents must exactly match its references"
                )
            for reference in validated:
                content = detached[reference.path]
                if (
                    len(content) != reference.byte_size
                    or sha256_digest(content) != reference.digest
                ):
                    raise ValueError(
                        "external bundle content size or digest does not match"
                    )
                self._secret_scanner.require_clean_chunks((content,))

            bundle_directory = self._external_bundle_directory(validated)
            total_size = sum(reference.byte_size for reference in validated)
            committed_total = self._total_bytes + total_size
            committed_manifest = [*self._manifest, *validated]
            relative_paths = {
                reference.path: PurePosixPath(reference.path).relative_to(
                    bundle_directory
                )
                for reference in validated
            }

            with self._parent_directory(bundle_directory.as_posix()) as (
                parent_fd,
                target_name,
            ):
                self._target_must_be_absent(parent_fd, target_name)
                staging_name = (
                    f".{target_name}.{uuid.uuid4().hex}.external-bundle-staging"
                )
                staging_fd = -1
                staging_created = False
                published = False
                try:
                    os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
                    staging_created = True
                    staging_fd = _open_directory(
                        parent_fd,
                        staging_name,
                        description="external bundle staging",
                    )
                    self._require_bundle_directory(os.fstat(staging_fd))
                    os.fsync(parent_fd)
                    for reference in sorted(validated, key=lambda item: item.path):
                        self._write_bundle_file(
                            staging_fd,
                            relative_paths[reference.path],
                            detached[reference.path],
                        )
                    for reference in sorted(validated, key=lambda item: item.path):
                        self._verify_bundle_file(
                            staging_fd,
                            relative_paths[reference.path],
                            reference,
                        )
                    os.fsync(staging_fd)
                    self._target_must_be_absent(parent_fd, target_name)
                    os.rename(
                        staging_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    published = True
                    self._total_bytes = committed_total
                    self._manifest = committed_manifest
                    os.fsync(parent_fd)
                except BaseException:
                    cleanup_failed = False
                    if published:
                        self._poisoned = True
                    if staging_fd >= 0:
                        try:
                            os.close(staging_fd)
                        except BaseException:
                            cleanup_failed = True
                        staging_fd = -1
                    if not published and staging_created:
                        try:
                            self._remove_bundle_directory(parent_fd, staging_name)
                        except BaseException:
                            cleanup_failed = True
                        target_state_uncertain = False
                        try:
                            os.stat(
                                target_name,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            target_present = False
                        except BaseException:
                            target_present = False
                            target_state_uncertain = True
                        else:
                            target_present = True
                        if cleanup_failed or target_present or target_state_uncertain:
                            self._staged_total_bytes += total_size
                            self._poisoned = True
                    elif cleanup_failed:
                        self._poisoned = True
                    raise
                finally:
                    if staging_fd >= 0:
                        try:
                            os.close(staging_fd)
                        except BaseException:
                            self._poisoned = True
                            raise
            return validated

    @contextmanager
    def _parent_directory(
        self,
        relative_path: str,
    ) -> Iterator[tuple[int, str]]:
        relative = Path(relative_path)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("artifact path must be normalised and relative")
        if not relative.parts:
            raise ValueError("artifact path must identify a file")

        current_fd = os.dup(self._root_fd)
        try:
            for component in relative.parts[:-1]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    try:
                        os.fsync(current_fd)
                    except BaseException:
                        self._poisoned = True
                        raise
                next_fd = _open_directory(
                    current_fd,
                    component,
                    description="artifact path",
                )
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd, relative.parts[-1]
        finally:
            os.close(current_fd)

    @contextmanager
    def _existing_parent_directory(
        self,
        relative_path: str,
    ) -> Iterator[tuple[int, str]]:
        relative = Path(relative_path)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("artifact path must be normalised and relative")
        if not relative.parts:
            raise ValueError("artifact path must identify a file")

        current_fd = os.dup(self._root_fd)
        try:
            for component in relative.parts[:-1]:
                next_fd = _open_directory(
                    current_fd,
                    component,
                    description="artifact path",
                )
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd, relative.parts[-1]
        finally:
            os.close(current_fd)

    @staticmethod
    def _path_identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _require_regular_staging_file(metadata: os.stat_result) -> None:
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("staging artifact may not be a symbolic link")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("staging artifact must be a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("staging artifact may not have a hard link alias")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("staging artifact has an unsafe file mode")

    @staticmethod
    def _target_must_be_absent(parent_fd: int, target_name: str) -> None:
        try:
            metadata = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("artifact target may not be a symbolic link")
        raise ValueError("artifact target already exists")

    def _require_staged_artifact(self, staged: StagedArtifact) -> None:
        if not isinstance(staged, StagedArtifact) or staged._writer is not self:
            raise ValueError("staged artifact is not owned by this output writer")
        staged._ensure_open()

    def _open_staging_file(
        self,
        staged: StagedArtifact,
        flags: int,
    ) -> tuple[int, os.stat_result]:
        try:
            path_metadata = os.stat(
                staged._staging_name,
                dir_fd=staged._parent_fd,
                follow_symlinks=False,
            )
            self._require_regular_staging_file(path_metadata)
            descriptor = os.open(
                staged._staging_name,
                flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=staged._parent_fd,
            )
        except FileNotFoundError as exc:
            raise ValueError("staging artifact is unavailable") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("staging artifact contains a symbolic link") from exc
            raise ValueError("staging artifact is unavailable") from exc

        try:
            opened_metadata = os.fstat(descriptor)
            self._require_regular_staging_file(opened_metadata)
            if self._path_identity(opened_metadata) != self._path_identity(
                path_metadata
            ):
                raise ValueError("staging artifact changed while being opened")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened_metadata

    def begin_staged_artifact(
        self,
        relative_path: str,
        *,
        media_type: str = OCTET_STREAM_MEDIA_TYPE,
        finalization_reserve_bytes: int = 0,
    ) -> StagedArtifact:
        """Create a hidden writer-owned staging file beside its final target."""

        if not isinstance(media_type, str) or not media_type or "\x00" in media_type:
            raise ValueError("staged artifact media type is invalid")
        if (
            isinstance(finalization_reserve_bytes, bool)
            or not isinstance(finalization_reserve_bytes, int)
            or finalization_reserve_bytes < 0
        ):
            raise ValueError(
                "staged finalization reserve must be a non-negative integer"
            )
        with self._write_lock:
            self._ensure_writable()
            self._require_capacity(
                0,
                artifact_size=finalization_reserve_bytes,
            )
            if any(item.path == relative_path for item in self._manifest):
                raise ValueError("artifact path is already recorded")
            with self._parent_directory(relative_path) as (parent_fd, target_name):
                self._target_must_be_absent(parent_fd, target_name)
                staging_name = f".{target_name}.{uuid.uuid4().hex}.staging"
                descriptor = -1
                staging_created = False
                staged_parent_fd = -1
                try:
                    descriptor = os.open(
                        staging_name,
                        (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                        ),
                        0o600,
                        dir_fd=parent_fd,
                    )
                    staging_created = True
                    metadata = os.fstat(descriptor)
                    self._require_regular_staging_file(metadata)
                    os.fsync(descriptor)
                    os.close(descriptor)
                    descriptor = -1
                    os.fsync(parent_fd)
                    staged_parent_fd = os.dup(parent_fd)
                    return StagedArtifact(
                        writer=self,
                        parent_fd=staged_parent_fd,
                        staging_name=staging_name,
                        target_name=target_name,
                        relative_path=relative_path,
                        media_type=media_type,
                        finalization_reserve_bytes=finalization_reserve_bytes,
                    )
                except BaseException:
                    cleanup_failed = False
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except BaseException:
                            cleanup_failed = True
                    if staged_parent_fd >= 0:
                        try:
                            os.close(staged_parent_fd)
                        except BaseException:
                            cleanup_failed = True
                    if staging_created:
                        try:
                            os.unlink(staging_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
                        except BaseException:
                            cleanup_failed = True
                        try:
                            os.fsync(parent_fd)
                        except BaseException:
                            cleanup_failed = True
                        staging_state_uncertain = False
                        try:
                            os.stat(
                                staging_name,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            staging_present = False
                        except BaseException:
                            staging_present = False
                            staging_state_uncertain = True
                        else:
                            staging_present = True
                        if cleanup_failed or staging_present or staging_state_uncertain:
                            self._poisoned = True
                    raise

    def _append_staged_artifact(
        self,
        staged: StagedArtifact,
        content: bytes,
    ) -> None:
        with self._write_lock:
            self._ensure_writable()
            self._require_staged_artifact(staged)
            if not isinstance(content, bytes):
                raise TypeError("staged artifact content must be bytes")
            resulting_size = staged._byte_size + len(content)
            self._require_capacity(
                len(content),
                artifact_size=(
                    resulting_size
                    + (0 if self._finalizing else staged._finalization_reserve_bytes)
                ),
            )
            self._secret_scanner.require_clean_chunks((content,))

            descriptor, before = self._open_staging_file(
                staged,
                os.O_WRONLY | os.O_APPEND,
            )
            try:
                if before.st_size != staged._byte_size:
                    raise ValueError("staging artifact size changed unexpectedly")
                view = memoryview(content)
                written = 0
                while written < len(view):
                    consumed = os.write(descriptor, view[written:])
                    if consumed <= 0:
                        raise OSError("staging artifact append made no progress")
                    written += consumed
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                self._require_regular_staging_file(after)
                if (
                    self._path_identity(after) != self._path_identity(before)
                    or after.st_size != resulting_size
                ):
                    raise ValueError("staging artifact changed during append")
            except BaseException:
                self._poisoned = True
                raise
            finally:
                try:
                    os.close(descriptor)
                except BaseException:
                    self._poisoned = True
                    raise

            staged._digest.update(content)
            staged._byte_size = resulting_size
            self._staged_total_bytes += len(content)

    def _finalize_staged_artifact(
        self,
        staged: StagedArtifact,
    ) -> ArtifactReference:
        with self._write_lock:
            self._ensure_writable()
            self._require_staged_artifact(staged)
            if any(item.path == staged._relative_path for item in self._manifest):
                raise ValueError("artifact path is already recorded")
            self._require_manifest_capacity(1)
            if staged._byte_size > self._staged_total_bytes:
                raise RuntimeError("staged output accounting is inconsistent")

            descriptor, before = self._open_staging_file(staged, os.O_RDONLY)
            digest = hashlib.sha256()
            consumed = 0

            def scanned_chunks() -> Iterator[bytes]:
                nonlocal consumed
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        return
                    consumed += len(chunk)
                    if consumed > staged._byte_size:
                        raise ValueError("staging artifact size changed unexpectedly")
                    digest.update(chunk)
                    yield chunk

            try:
                if before.st_size != staged._byte_size:
                    raise ValueError("staging artifact size changed unexpectedly")
                self._secret_scanner.require_clean_chunks(scanned_chunks())
                after = os.fstat(descriptor)
                self._require_regular_staging_file(after)
            finally:
                os.close(descriptor)

            expected_digest = staged._digest.hexdigest()
            if (
                consumed != staged._byte_size
                or digest.hexdigest() != expected_digest
                or self._path_identity(after) != self._path_identity(before)
            ):
                raise ValueError("staging artifact changed before finalization")
            try:
                path_metadata = os.stat(
                    staged._staging_name,
                    dir_fd=staged._parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise ValueError("staging artifact is unavailable") from exc
            self._require_regular_staging_file(path_metadata)
            if self._path_identity(path_metadata) != self._path_identity(before):
                raise ValueError("staging artifact changed before finalization")

            self._target_must_be_absent(
                staged._parent_fd,
                staged._target_name,
            )
            reference = ArtifactReference(
                path=staged._relative_path,
                media_type=staged._media_type,
                byte_size=staged._byte_size,
                digest=f"sha256:{expected_digest}",
            )
            try:
                os.replace(
                    staged._staging_name,
                    staged._target_name,
                    src_dir_fd=staged._parent_fd,
                    dst_dir_fd=staged._parent_fd,
                )
            except BaseException:
                state_uncertain = False
                try:
                    os.stat(
                        staged._target_name,
                        dir_fd=staged._parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except BaseException:
                    state_uncertain = True
                else:
                    state_uncertain = True
                try:
                    remaining = os.stat(
                        staged._staging_name,
                        dir_fd=staged._parent_fd,
                        follow_symlinks=False,
                    )
                    self._require_regular_staging_file(remaining)
                    if self._path_identity(remaining) != self._path_identity(before):
                        state_uncertain = True
                except BaseException:
                    state_uncertain = True
                if state_uncertain:
                    self._poisoned = True
                raise
            self._manifest.append(reference)
            self._staged_total_bytes -= staged._byte_size
            self._total_bytes += staged._byte_size
            try:
                published_metadata = os.stat(
                    staged._target_name,
                    dir_fd=staged._parent_fd,
                    follow_symlinks=False,
                )
                self._require_regular_staging_file(published_metadata)
                if self._path_identity(published_metadata) != self._path_identity(
                    before
                ):
                    raise ValueError("staging artifact changed during finalization")
                os.fsync(staged._parent_fd)
            except BaseException:
                self._poisoned = True
                raise
            return reference

    def _abort_staged_artifact(self, staged: StagedArtifact) -> None:
        with self._write_lock:
            self._require_staged_artifact(staged)
            try:
                os.unlink(
                    staged._staging_name,
                    dir_fd=staged._parent_fd,
                )
            except FileNotFoundError:
                pass
            except IsADirectoryError as exc:
                self._poisoned = True
                raise ValueError(
                    "staging artifact was replaced by a directory"
                ) from exc
            except BaseException:
                self._poisoned = True
                raise
            try:
                os.fsync(staged._parent_fd)
            except BaseException:
                self._poisoned = True
                raise
            if staged._byte_size > self._staged_total_bytes:
                self._poisoned = True
                raise RuntimeError("staged output accounting is inconsistent")
            self._staged_total_bytes -= staged._byte_size

    def write_bytes(
        self,
        relative_path: str,
        content: bytes,
        *,
        media_type: str = OCTET_STREAM_MEDIA_TYPE,
        record: bool = True,
    ) -> ArtifactReference:
        with self._write_lock:
            self._ensure_writable()
            if not isinstance(content, bytes):
                raise TypeError("artifact content must be bytes")
            content_size = len(content)
            self._require_capacity(content_size)
            if record:
                self._require_manifest_capacity(1)
            if self._secret_scanner is not None:
                self._secret_scanner.require_clean_chunks((content,))
            reference = ArtifactReference(
                path=relative_path,
                media_type=media_type,
                byte_size=len(content),
                digest=sha256_digest(content),
            )
            with self._parent_directory(relative_path) as (parent_fd, target_name):
                try:
                    metadata = os.stat(
                        target_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    metadata = None
                if metadata is not None:
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError("artifact target may not be a symbolic link")
                    raise ValueError("artifact target already exists")

                temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
                descriptor = os.open(
                    temporary_name,
                    (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    ),
                    0o600,
                    dir_fd=parent_fd,
                )
                published = False
                try:
                    handle = os.fdopen(descriptor, "wb")
                    descriptor = -1
                    with handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                        staging_metadata = os.fstat(handle.fileno())
                        self._require_regular_staging_file(staging_metadata)
                    self._require_regular_staging_file(
                        os.stat(
                            temporary_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    )
                    os.replace(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    published = True
                    self._total_bytes += content_size
                    if record:
                        self._manifest.append(reference)
                    try:
                        published_metadata = os.stat(
                            target_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        self._require_regular_staging_file(published_metadata)
                        if self._path_identity(
                            published_metadata
                        ) != self._path_identity(staging_metadata):
                            raise ValueError("artifact changed during publication")
                        os.fsync(parent_fd)
                    except BaseException:
                        self._poisoned = True
                        raise
                finally:
                    cleanup_failed = False
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except BaseException:
                            cleanup_failed = True
                    if not published:
                        try:
                            os.unlink(temporary_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
                        except BaseException:
                            cleanup_failed = True
                        try:
                            os.fsync(parent_fd)
                        except BaseException:
                            cleanup_failed = True
                        target_state_uncertain = False
                        try:
                            os.stat(
                                target_name,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            target_present = False
                        except BaseException:
                            target_present = False
                            target_state_uncertain = True
                        else:
                            target_present = True
                        if cleanup_failed or target_present or target_state_uncertain:
                            self._staged_total_bytes += content_size
                            self._poisoned = True
            return reference

    def register_existing_artifact(
        self,
        reference: ArtifactReference,
    ) -> ArtifactReference:
        """Verify and adopt one immutable artifact written by a local atomic sink."""

        try:
            validated = ArtifactReference.model_validate(
                reference.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("external artifact reference is invalid") from exc

        with self._write_lock:
            self._ensure_writable()
            if any(item.path == validated.path for item in self._manifest):
                raise ValueError("artifact path is already recorded")
            self._require_capacity(validated.byte_size)
            self._require_manifest_capacity(1)

            with self._existing_parent_directory(validated.path) as (
                parent_fd,
                target_name,
            ):
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(target_name, flags, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ValueError(
                            "external artifact contains a symbolic link"
                        ) from exc
                    raise ValueError("external artifact is unavailable") from exc
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("external artifact must be a regular file")
                    if before.st_nlink != 1:
                        raise ValueError(
                            "external artifact may not have a hard link alias"
                        )
                    if before.st_size != validated.byte_size:
                        raise ValueError("external artifact size mismatch")
                    digest = hashlib.sha256()
                    chunks: list[bytes] = []
                    consumed = 0
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        consumed += len(chunk)
                        if consumed > validated.byte_size:
                            raise ValueError("external artifact size mismatch")
                        digest.update(chunk)
                        chunks.append(chunk)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)

            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if identity_before != identity_after or consumed != validated.byte_size:
                raise ValueError("external artifact changed while being verified")
            actual_digest = f"sha256:{digest.hexdigest()}"
            if actual_digest != validated.digest:
                raise ValueError("external artifact digest mismatch")
            self._secret_scanner.require_clean_chunks(chunks)
            self._manifest.append(validated)
            self._total_bytes += validated.byte_size
            return validated

    def write_json(
        self,
        relative_path: str,
        payload: Mapping[str, Any] | Sequence[Any],
        *,
        record: bool = True,
    ) -> ArtifactReference:
        content = canonical_json_bytes(payload)
        return self.write_bytes(
            relative_path,
            content,
            media_type=JSON_MEDIA_TYPE,
            record=record,
        )

    @property
    def manifest_digest(self) -> str:
        with self._write_lock:
            payload = [
                reference.model_dump(mode="json")
                for reference in sorted(self._manifest, key=lambda item: item.path)
            ]
            return sha256_digest(canonical_json_bytes(payload))

    def write_events_placeholder(self) -> ArtifactReference:
        return self.write_bytes(
            "events.ndjson",
            b"",
            media_type="application/x-ndjson",
        )

    def write_protocol_manifest(
        self,
        *,
        event_stream: ArtifactReference,
    ) -> ArtifactReference:
        """Bind the finalized event artifact into the attempt protocol manifest."""

        with self._write_lock:
            self._ensure_writable()
            if event_stream.path != "events.ndjson":
                raise ValueError("protocol manifest requires the canonical event path")
            if event_stream.media_type != "application/x-ndjson":
                raise ValueError("protocol manifest requires an NDJSON event stream")
            if event_stream not in self._manifest:
                raise ValueError(
                    "protocol manifest requires a writer-owned event reference"
                )
            artifacts = tuple(sorted(self._manifest, key=lambda item: item.path))
            payload = {
                "protocol": "factory-runner-protocol/v1",
                "schema": "protocol-manifest/v1",
                "schema_version": 1,
                "event_stream": event_stream.model_dump(mode="json"),
                "artifacts": [
                    reference.model_dump(mode="json") for reference in artifacts
                ],
            }
            manifest = ProtocolManifest.model_validate(payload)
            reference = self.write_json(
                "protocol-manifest.json",
                manifest.model_dump(mode="json"),
                record=False,
            )
            self._protocol_manifest_reference = reference
            return reference

    def write_run_result(
        self,
        *,
        operation: Literal["author", "verify"],
        outcome: RunOutcome,
        reason_code: str,
        message: str,
        started_at: str,
        finished_at: str,
        identity: RunIdentity | None,
        repository: RepositoryIdentity | None,
        completed_stages: Sequence[str],
        latest_checkpoint: ArtifactReference | None = None,
        change_set: ArtifactReference | None = None,
        verification_evidence: ArtifactReference | None = None,
        event_stream_digest: str = EMPTY_DIGEST,
        protocol_manifest: ArtifactReference | None = None,
    ) -> tuple[RunResult, ArtifactReference]:
        manifest_reference = (
            protocol_manifest
            if protocol_manifest is not None
            else self._protocol_manifest_reference
        )
        if (
            manifest_reference is not None
            and manifest_reference != self._protocol_manifest_reference
        ):
            raise ValueError(
                "run result requires the writer-owned protocol manifest reference"
            )
        payload: dict[str, Any] = {
            "protocol": "factory-runner-protocol/v1",
            "schema": "run-result/v1",
            "schema_version": 1,
            "created_at": finished_at,
            "identity": (
                identity.model_dump(mode="json") if identity is not None else None
            ),
            "repository": (
                repository.model_dump(mode="json") if repository is not None else None
            ),
            "operation": operation,
            "outcome": outcome,
            "reason_code": reason_code,
            "message": message,
            "started_at": started_at,
            "finished_at": finished_at,
            "completed_stages": list(completed_stages),
            "latest_checkpoint": (
                latest_checkpoint.model_dump(mode="json")
                if latest_checkpoint is not None
                else None
            ),
            "change_set": (
                change_set.model_dump(mode="json") if change_set is not None else None
            ),
            "verification_evidence": (
                verification_evidence.model_dump(mode="json")
                if verification_evidence is not None
                else None
            ),
            "event_stream_digest": event_stream_digest,
            "output_manifest_digest": (
                manifest_reference.digest
                if manifest_reference is not None
                else self.manifest_digest
            ),
            "runner_build": RunnerBuildIdentity(
                version=__version__,
                image=None,
                source_commit=None,
            ).model_dump(mode="json"),
            "result_digest": EMPTY_DIGEST,
        }
        payload["result_digest"] = contract_document_digest(payload)
        result = RunResult.model_validate(payload)
        reference = self.write_json(
            "result/run-result.json",
            result.model_dump(mode="json"),
            record=False,
        )
        return result, reference

    def write_completion(
        self,
        *,
        result: RunResult,
        result_reference: ArtifactReference,
        protocol_manifest: ArtifactReference | None = None,
    ) -> None:
        manifest_reference = (
            protocol_manifest
            if protocol_manifest is not None
            else self._protocol_manifest_reference
        )
        if (
            manifest_reference is not None
            and manifest_reference != self._protocol_manifest_reference
        ):
            raise ValueError(
                "completion requires the writer-owned protocol manifest reference"
            )
        completion = {
            "protocol": "factory-runner-protocol/v1",
            "schema": "completion/v1",
            "schema_version": 1,
            "completed_at": result.finished_at,
            "outcome": result.outcome,
            "output_manifest_digest": result.output_manifest_digest,
            "protocol_manifest": (
                manifest_reference.model_dump(mode="json")
                if manifest_reference is not None
                else None
            ),
            "run_result": result_reference.model_dump(mode="json"),
        }
        validated_completion = CompletionManifest.model_validate(completion)
        with self._write_lock:
            self._ensure_writable()
            self.write_json(
                "completion.json",
                validated_completion.model_dump(mode="json"),
                record=False,
            )
            self._sealed = True


def sanitised_message(value: object, fallback: str) -> str:
    """Return a bounded stable diagnostic without leaking exception details."""

    if isinstance(value, str):
        compact = " ".join(value.split())
        if compact and len(compact) <= 512:
            return compact
    return fallback


__all__ = [
    "EMPTY_DIGEST",
    "JSON_MEDIA_TYPE",
    "OCTET_STREAM_MEDIA_TYPE",
    "OutputLimitError",
    "OutputWriter",
    "OutputTreeSnapshot",
    "StagedArtifact",
    "capture_output_tree",
    "enforce_output_tree_unchanged",
    "restore_output_tree",
    "sanitised_message",
    "utc_timestamp",
    "validate_output_root",
]

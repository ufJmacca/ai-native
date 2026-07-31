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
from pathlib import Path
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
from ai_native.factory_runner.protocol import contract_document_digest
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner


JSON_MEDIA_TYPE = "application/json"
OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"
EMPTY_DIGEST = sha256_digest(b"")
_MAX_OUTPUT_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_SNAPSHOT_ENTRIES = 20_000


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
    ) -> None:
        self._writer = writer
        self._parent_fd = parent_fd
        self._staging_name = staging_name
        self._target_name = target_name
        self._relative_path = relative_path
        self._media_type = media_type
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
    ):
        for name, value in (
            ("max_artifact_bytes", max_artifact_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")
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
        self._max_total_bytes = max_total_bytes
        self._total_bytes = 0
        self._sealed = False
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
        if self._sealed:
            raise RuntimeError("output writer is finalized by completion.json")

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
    ) -> StagedArtifact:
        """Create a hidden writer-owned staging file beside its final target."""

        if not isinstance(media_type, str) or not media_type or "\x00" in media_type:
            raise ValueError("staged artifact media type is invalid")
        with self._write_lock:
            self._ensure_writable()
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
                    )
                except Exception:
                    if descriptor >= 0:
                        os.close(descriptor)
                    if staged_parent_fd >= 0:
                        os.close(staged_parent_fd)
                    if staging_created:
                        try:
                            os.unlink(staging_name, dir_fd=parent_fd)
                        except (FileNotFoundError, IsADirectoryError):
                            pass
                        try:
                            os.fsync(parent_fd)
                        except OSError:
                            pass
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
            if (
                self._max_artifact_bytes is not None
                and resulting_size > self._max_artifact_bytes
            ):
                raise ValueError("artifact size exceeds the artifact limit")
            if (
                self._max_total_bytes is not None
                and resulting_size > self._max_total_bytes - self._total_bytes
            ):
                raise ValueError("total output size exceeds the total limit")
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
                if (
                    self._path_identity(after) != self._path_identity(before)
                    or after.st_size != resulting_size
                ):
                    raise ValueError("staging artifact changed during append")
            finally:
                os.close(descriptor)

            staged._digest.update(content)
            staged._byte_size = resulting_size

    def _finalize_staged_artifact(
        self,
        staged: StagedArtifact,
    ) -> ArtifactReference:
        with self._write_lock:
            self._ensure_writable()
            self._require_staged_artifact(staged)
            if any(item.path == staged._relative_path for item in self._manifest):
                raise ValueError("artifact path is already recorded")
            if (
                self._max_artifact_bytes is not None
                and staged._byte_size > self._max_artifact_bytes
            ):
                raise ValueError("artifact size exceeds the artifact limit")
            if (
                self._max_total_bytes is not None
                and staged._byte_size > self._max_total_bytes - self._total_bytes
            ):
                raise ValueError("total output size exceeds the total limit")

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
            os.replace(
                staged._staging_name,
                staged._target_name,
                src_dir_fd=staged._parent_fd,
                dst_dir_fd=staged._parent_fd,
            )
            os.fsync(staged._parent_fd)

            reference = ArtifactReference(
                path=staged._relative_path,
                media_type=staged._media_type,
                byte_size=staged._byte_size,
                digest=f"sha256:{expected_digest}",
            )
            self._manifest.append(reference)
            self._total_bytes += staged._byte_size
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
                raise ValueError(
                    "staging artifact was replaced by a directory"
                ) from exc
            os.fsync(staged._parent_fd)

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
            if (
                self._max_artifact_bytes is not None
                and content_size > self._max_artifact_bytes
            ):
                raise ValueError("artifact size exceeds the artifact limit")
            if self._secret_scanner is not None:
                self._secret_scanner.require_clean_chunks((content,))
            if (
                self._max_total_bytes is not None
                and content_size > self._max_total_bytes - self._total_bytes
            ):
                raise ValueError("total output size exceeds the total limit")
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
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    descriptor = -1
                    os.replace(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    os.fsync(parent_fd)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            self._total_bytes += content_size
            reference = ArtifactReference(
                path=relative_path,
                media_type=media_type,
                byte_size=len(content),
                digest=sha256_digest(content),
            )
            if record:
                self._manifest.append(reference)
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
            if (
                self._max_artifact_bytes is not None
                and validated.byte_size > self._max_artifact_bytes
            ):
                raise ValueError("artifact size exceeds the artifact limit")
            if (
                self._max_total_bytes is not None
                and validated.byte_size > self._max_total_bytes - self._total_bytes
            ):
                raise ValueError("total output size exceeds the total limit")

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
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
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
                "schema_version": 1,
                "event_stream": event_stream.model_dump(mode="json"),
                "artifacts": [
                    reference.model_dump(mode="json") for reference in artifacts
                ],
            }
            reference = self.write_json("protocol-manifest.json", payload)
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
            "schema_version": 1,
            "completed_at": result.finished_at,
            "outcome": result.outcome,
            "output_manifest_digest": result.output_manifest_digest,
            "run_result": result_reference.model_dump(mode="json"),
        }
        if manifest_reference is not None:
            completion["protocol_manifest"] = manifest_reference.model_dump(mode="json")
        with self._write_lock:
            self._ensure_writable()
            self.write_json("completion.json", completion, record=False)
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

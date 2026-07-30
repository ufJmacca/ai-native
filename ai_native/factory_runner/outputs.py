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
import os
from pathlib import Path
import stat
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

    def __init__(self, root: Path):
        self.root = root
        self._root_fd = os.open(root, _directory_open_flags())
        self._manifest: list[ArtifactReference] = []

    def __del__(self) -> None:
        root_fd = getattr(self, "_root_fd", -1)
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
            self._root_fd = -1

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

    def write_bytes(
        self,
        relative_path: str,
        content: bytes,
        *,
        media_type: str = OCTET_STREAM_MEDIA_TYPE,
        record: bool = True,
    ) -> ArtifactReference:
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
        reference = ArtifactReference(
            path=relative_path,
            media_type=media_type,
            byte_size=len(content),
            digest=sha256_digest(content),
        )
        if record:
            self._manifest.append(reference)
        return reference

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
        change_set: ArtifactReference | None = None,
        verification_evidence: ArtifactReference | None = None,
        event_stream_digest: str = EMPTY_DIGEST,
    ) -> tuple[RunResult, ArtifactReference]:
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
            "latest_checkpoint": None,
            "change_set": (
                change_set.model_dump(mode="json") if change_set is not None else None
            ),
            "verification_evidence": (
                verification_evidence.model_dump(mode="json")
                if verification_evidence is not None
                else None
            ),
            "event_stream_digest": event_stream_digest,
            "output_manifest_digest": self.manifest_digest,
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
    ) -> None:
        completion = {
            "protocol": "factory-runner-protocol/v1",
            "schema_version": 1,
            "completed_at": result.finished_at,
            "outcome": result.outcome,
            "output_manifest_digest": result.output_manifest_digest,
            "run_result": result_reference.model_dump(mode="json"),
        }
        self.write_json("completion.json", completion, record=False)


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
    "capture_output_tree",
    "enforce_output_tree_unchanged",
    "restore_output_tree",
    "sanitised_message",
    "utc_timestamp",
    "validate_output_root",
]

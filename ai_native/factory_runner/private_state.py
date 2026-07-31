"""Portable, bounded snapshots of runner-private author workflow state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.checkpoint_runtime import CheckpointStateObject
from ai_native.factory_runner.contracts.common import freeze_mapping
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner


PRIVATE_STATE_WORKFLOW_KEY = "private_author_state"
PRIVATE_ROOT_TOKEN = b"@{FACTORY_PRIVATE_ROOT}@"
PRIVATE_RUN_TOKEN = b"@{FACTORY_PRIVATE_RUN}@"
WORKSPACE_ROOT_TOKEN = b"@{FACTORY_WORKSPACE_ROOT}@"

_SCHEMA = "private-author-state/v1"
_TOKENS = frozenset(
    {
        PRIVATE_ROOT_TOKEN,
        PRIVATE_RUN_TOKEN,
        WORKSPACE_ROOT_TOKEN,
    }
)
_TOKEN_PATTERN = re.compile(rb"@\{FACTORY_[A-Z0-9_]{1,128}\}@")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_READ_CHUNK_BYTES = 1024 * 1024
_ALLOWED_FILE_MODES = frozenset({0o600, 0o640, 0o644})
_ALLOWED_DIRECTORY_MODES = frozenset({0o700, 0o750, 0o755})
_HARD_MAX_FILES = 4096
_HARD_MAX_FILE_BYTES = 16 * 1024 * 1024
_HARD_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 262_144


class PrivateStateError(FactoryPolicyViolation):
    """Private state cannot be durably represented or safely restored."""


@dataclass(frozen=True, slots=True)
class PrivateStateLimits:
    max_files: int = 1024
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        limits = (
            (self.max_files, _HARD_MAX_FILES),
            (self.max_file_bytes, _HARD_MAX_FILE_BYTES),
            (self.max_total_bytes, _HARD_MAX_TOTAL_BYTES),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
            for value, maximum in limits
        ):
            raise ValueError("private state limits must be positive and bounded")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("private state file limit may not exceed its total limit")


@dataclass(frozen=True, slots=True)
class PrivateStateSnapshot:
    descriptor: Mapping[str, Any]
    objects: tuple[CheckpointStateObject, ...]


def _safe_mode(metadata: os.stat_result, *, directory: bool) -> int:
    mode = stat.S_IMODE(metadata.st_mode)
    allowed = _ALLOWED_DIRECTORY_MODES if directory else _ALLOWED_FILE_MODES
    if mode not in allowed:
        raise PrivateStateError("private state entry has an unsafe mode")
    return mode


def _trusted_root(path: Path, *, description: str) -> Path:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            raise PrivateStateError(f"{description} must not be a symbolic link")
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except PrivateStateError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PrivateStateError(f"{description} is missing or inaccessible") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PrivateStateError(f"{description} must be a directory")
    return resolved


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _descendant(
    path: Path,
    *,
    root: Path,
    description: str,
    may_not_exist: bool,
) -> tuple[Path, Path]:
    lexical_root = _lexical_absolute(root)
    lexical_path = _lexical_absolute(path)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise PrivateStateError(
            f"{description} must remain beneath its private root"
        ) from exc
    if not relative.parts:
        raise PrivateStateError(
            f"{description} must be a strict private-root descendant"
        )
    current = lexical_root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise PrivateStateError(f"{description} traverses a symbolic link")
        if not current.exists():
            if not may_not_exist and index != len(relative.parts) - 1:
                raise PrivateStateError(f"{description} is missing")
            continue
        if index != len(relative.parts) - 1:
            if not current.is_dir():
                raise PrivateStateError(
                    f"{description} traverses a non-directory entry"
                )
            _safe_mode(current.stat(follow_symlinks=False), directory=True)
    return lexical_path, relative


def _relative_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or _CONTROL_PATTERN.search(value) is not None
    ):
        raise PrivateStateError("private state path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) > 64
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
    ):
        raise PrivateStateError("private state path is unsafe")
    return path


def _read_regular_file(
    path: Path,
    *,
    expected: os.stat_result,
    limits: PrivateStateLimits,
) -> tuple[bytes, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PrivateStateError(
            "private state regular file could not be opened"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != expected.st_dev
            or metadata.st_ino != expected.st_ino
        ):
            raise PrivateStateError("private state entry changed during snapshot")
        if metadata.st_nlink != 1:
            raise PrivateStateError("private state hard links are unsafe")
        mode = _safe_mode(metadata, directory=False)
        if metadata.st_size > limits.max_file_bytes:
            raise PrivateStateError("private state file exceeds its size limit")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limits.max_file_bytes:
                raise PrivateStateError("private state file exceeds its size limit")
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(content) != metadata.st_size:
        raise PrivateStateError("private state entry changed during snapshot")
    return content, mode


def _path_boundary(content: bytes, start: int, root: bytes) -> bool:
    end = start + len(root)
    if end == len(content):
        return True
    return content[end : end + 1] in b"/\x00\"' \t\r\n,:;)]}"


def _replace_bound_path(content: bytes, root: bytes, token: bytes) -> bytes:
    output = bytearray()
    cursor = 0
    while True:
        index = content.find(root, cursor)
        if index < 0:
            output.extend(content[cursor:])
            break
        output.extend(content[cursor:index])
        if _path_boundary(content, index, root):
            output.extend(token)
        else:
            output.extend(root)
        cursor = index + len(root)
    return bytes(output)


def _tokenise(
    content: bytes,
    *,
    private_root: Path,
    private_run_dir: Path,
    workspace_root: Path,
) -> bytes:
    collision = _TOKEN_PATTERN.search(content)
    if collision is not None:
        raise PrivateStateError("private state contains a reserved portability token")
    bindings = sorted(
        (
            (os.fsencode(private_run_dir), PRIVATE_RUN_TOKEN),
            (os.fsencode(private_root), PRIVATE_ROOT_TOKEN),
            (os.fsencode(workspace_root), WORKSPACE_ROOT_TOKEN),
        ),
        key=lambda entry: (-len(entry[0]), entry[1]),
    )
    tokenised = content
    for root, token in bindings:
        tokenised = _replace_bound_path(tokenised, root, token)
    return tokenised


def snapshot_private_run_directory(
    private_run_dir: Path,
    *,
    private_root: Path,
    workspace_root: Path,
    limits: PrivateStateLimits = PrivateStateLimits(),
    secret_scanner: SecretScanner | None = None,
) -> PrivateStateSnapshot:
    """Snapshot one private run directory without retaining host-specific roots."""

    if not isinstance(limits, PrivateStateLimits):
        raise TypeError("limits must be PrivateStateLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    root = _trusted_root(private_root, description="private state root")
    _safe_mode(root.stat(follow_symlinks=False), directory=True)
    workspace = _trusted_root(workspace_root, description="workspace root")
    if (
        root == workspace
        or root.is_relative_to(workspace)
        or workspace.is_relative_to(root)
    ):
        raise PrivateStateError("private and workspace roots must not overlap")
    lexical_run, _relative_run = _descendant(
        private_run_dir,
        root=root,
        description="private run directory",
        may_not_exist=False,
    )
    run = _trusted_root(lexical_run, description="private run directory")
    if not run.is_relative_to(root):
        raise PrivateStateError(
            "private run directory must remain beneath its private root"
        )
    root_mode = _safe_mode(run.stat(follow_symlinks=False), directory=True)

    directory_entries: list[dict[str, object]] = []
    file_entries: list[dict[str, object]] = []
    object_by_digest: dict[str, CheckpointStateObject] = {}
    total_bytes = 0
    entry_count = 0

    def visit(directory: Path, relative: PurePosixPath | None = None) -> None:
        nonlocal entry_count, total_bytes
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    entry_count += 1
                    if entry_count > limits.max_files:
                        raise PrivateStateError(
                            "private state entry count exceeds its limit"
                        )
                    entries.append(entry)
                entries.sort(key=lambda entry: entry.name)
        except OSError as exc:
            raise PrivateStateError(
                "private state directory could not be read"
            ) from exc
        for entry in entries:
            try:
                entry.name.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise PrivateStateError(
                    "private state path is not portable UTF-8"
                ) from exc
            relative_path = (
                PurePosixPath(entry.name) if relative is None else relative / entry.name
            )
            safe_path = _relative_path(relative_path.as_posix())
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PrivateStateError(
                    "private state entry could not be inspected"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PrivateStateError("private state symbolic links are unsafe")
            entry_path = directory / entry.name
            if stat.S_ISDIR(metadata.st_mode):
                mode = _safe_mode(metadata, directory=True)
                directory_entries.append({"path": safe_path.as_posix(), "mode": mode})
                visit(entry_path, safe_path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PrivateStateError(
                    "private state entries must be regular files or directories"
                )
            if len(file_entries) >= limits.max_files:
                raise PrivateStateError("private state file count exceeds its limit")
            raw_content, mode = _read_regular_file(
                entry_path,
                expected=metadata,
                limits=limits,
            )
            scanner.require_clean_chunks((raw_content,))
            content = _tokenise(
                raw_content,
                private_root=root,
                private_run_dir=run,
                workspace_root=workspace,
            )
            scanner.require_clean_chunks((content,))
            if len(content) > limits.max_file_bytes:
                raise PrivateStateError(
                    "private state file exceeds its size limit after tokenisation"
                )
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise PrivateStateError("private state total size exceeds its limit")
            state_object = CheckpointStateObject(content=content)
            object_by_digest.setdefault(state_object.digest, state_object)
            file_entries.append(
                {
                    "path": safe_path.as_posix(),
                    "object_digest": state_object.digest,
                    "byte_size": state_object.byte_size,
                    "mode": mode,
                }
            )

    visit(run)
    directory_entries.sort(key=lambda entry: str(entry["path"]))
    file_entries.sort(key=lambda entry: str(entry["path"]))
    descriptor = {
        "schema": _SCHEMA,
        "root_mode": root_mode,
        "directories": directory_entries,
        "files": file_entries,
        "total_bytes": total_bytes,
    }
    descriptor_bytes = canonical_json_bytes(descriptor)
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PrivateStateError("private state descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    return PrivateStateSnapshot(
        descriptor=freeze_mapping(descriptor),
        objects=tuple(object_by_digest[digest] for digest in sorted(object_by_digest)),
    )


def _strict_integer(value: object, *, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrivateStateError(f"private state {description} must be an integer")
    return value


def _sequence(value: object, *, description: str) -> tuple[object, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise PrivateStateError(f"private state {description} must be a sequence")
    return tuple(value)


def _descriptor_entries(
    descriptor: Mapping[str, Any],
    *,
    limits: PrivateStateLimits,
) -> tuple[
    int,
    tuple[tuple[PurePosixPath, int], ...],
    tuple[tuple[PurePosixPath, str, int, int], ...],
]:
    if (
        set(descriptor)
        != {
            "schema",
            "root_mode",
            "directories",
            "files",
            "total_bytes",
        }
        or descriptor.get("schema") != _SCHEMA
    ):
        raise PrivateStateError("private state descriptor schema is invalid")
    root_mode = _strict_integer(
        descriptor["root_mode"],
        description="root mode",
    )
    if root_mode not in _ALLOWED_DIRECTORY_MODES:
        raise PrivateStateError("private state root mode is unsafe")

    directories: list[tuple[PurePosixPath, int]] = []
    for raw in _sequence(
        descriptor["directories"],
        description="directories",
    ):
        if not isinstance(raw, Mapping) or set(raw) != {"path", "mode"}:
            raise PrivateStateError("private state directory descriptor is invalid")
        path = _relative_path(raw["path"])
        mode = _strict_integer(raw["mode"], description="directory mode")
        if mode not in _ALLOWED_DIRECTORY_MODES:
            raise PrivateStateError("private state directory mode is unsafe")
        directories.append((path, mode))

    files: list[tuple[PurePosixPath, str, int, int]] = []
    for raw in _sequence(descriptor["files"], description="files"):
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "object_digest",
            "byte_size",
            "mode",
        }:
            raise PrivateStateError("private state file descriptor is invalid")
        path = _relative_path(raw["path"])
        digest = raw["object_digest"]
        if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
            raise PrivateStateError("private state object digest is invalid")
        byte_size = _strict_integer(raw["byte_size"], description="byte size")
        if not 0 <= byte_size <= limits.max_file_bytes:
            raise PrivateStateError("private state file size exceeds its limit")
        mode = _strict_integer(raw["mode"], description="file mode")
        if mode not in _ALLOWED_FILE_MODES:
            raise PrivateStateError("private state file mode is unsafe")
        files.append((path, digest, byte_size, mode))

    directory_paths = tuple(path.as_posix() for path, _mode in directories)
    file_paths = tuple(path.as_posix() for path, *_rest in files)
    if (
        len(files) + len(directories) > limits.max_files
        or len(set(directory_paths)) != len(directory_paths)
        or len(set(file_paths)) != len(file_paths)
        or set(directory_paths).intersection(file_paths)
        or directory_paths != tuple(sorted(directory_paths))
        or file_paths != tuple(sorted(file_paths))
    ):
        raise PrivateStateError("private state descriptor paths are invalid")
    known_directories = set(directory_paths)
    for path in (*[entry[0] for entry in directories], *[entry[0] for entry in files]):
        parents = tuple(parent.as_posix() for parent in path.parents[:-1])
        if not set(parents).issubset(known_directories):
            raise PrivateStateError("private state descriptor omits a parent directory")
    if any(
        file_path.startswith(directory_or_file + "/")
        for directory_or_file in file_paths
        for file_path in (*directory_paths, *file_paths)
        if file_path != directory_or_file
    ):
        raise PrivateStateError("private state file path cannot contain descendants")

    total_bytes = _strict_integer(
        descriptor["total_bytes"],
        description="total bytes",
    )
    if (
        total_bytes != sum(entry[2] for entry in files)
        or not 0 <= total_bytes <= limits.max_total_bytes
    ):
        raise PrivateStateError("private state total size exceeds its limit")
    return root_mode, tuple(directories), tuple(files)


def _resolved_objects(objects: Mapping[str, bytes]) -> Mapping[str, bytes]:
    if not isinstance(objects, Mapping):
        raise TypeError("objects must be a checkpoint object mapping")
    by_digest: dict[str, bytes] = {}
    total_bytes = 0
    for path, content in objects.items():
        if not isinstance(path, str) or not isinstance(content, bytes):
            raise PrivateStateError("private state checkpoint objects are invalid")
        if len(content) > _HARD_MAX_FILE_BYTES:
            raise PrivateStateError("private state checkpoint object exceeds its limit")
        total_bytes += len(content)
        if total_bytes > _HARD_MAX_TOTAL_BYTES:
            raise PrivateStateError(
                "private state checkpoint objects exceed their total limit"
            )
        digest = sha256_digest(content)
        previous = by_digest.setdefault(digest, content)
        if previous != content:
            raise PrivateStateError("private state object digest is ambiguous")
    return MappingProxyType(by_digest)


def _rebind(
    content: bytes,
    *,
    private_root: Path,
    destination_run_dir: Path,
    workspace_root: Path,
) -> bytes:
    for match in _TOKEN_PATTERN.finditer(content):
        if match.group() not in _TOKENS:
            raise PrivateStateError("private state contains an unresolved token")
    rebound = content
    for token, path in (
        (PRIVATE_RUN_TOKEN, destination_run_dir),
        (PRIVATE_ROOT_TOKEN, private_root),
        (WORKSPACE_ROOT_TOKEN, workspace_root),
    ):
        rebound = rebound.replace(token, os.fsencode(path))
    if _TOKEN_PATTERN.search(rebound) is not None:
        raise PrivateStateError("private state contains an unresolved token")
    return rebound


def _write_file(path: Path, content: bytes, mode: int) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_private_run_directory(
    *,
    descriptor: Mapping[str, Any],
    objects: Mapping[str, bytes],
    destination_run_dir: Path,
    private_root: Path,
    workspace_root: Path,
    limits: PrivateStateLimits = PrivateStateLimits(),
    secret_scanner: SecretScanner | None = None,
) -> Path:
    """Validate, rebind, and atomically restore state below a fresh private root."""

    if not isinstance(limits, PrivateStateLimits):
        raise TypeError("limits must be PrivateStateLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(descriptor, Mapping):
        raise PrivateStateError("private state descriptor must be an object")
    root = _trusted_root(private_root, description="private state root")
    _safe_mode(root.stat(follow_symlinks=False), directory=True)
    workspace = _trusted_root(workspace_root, description="workspace root")
    if (
        root == workspace
        or root.is_relative_to(workspace)
        or workspace.is_relative_to(root)
    ):
        raise PrivateStateError("private and workspace roots must not overlap")
    target, target_relative = _descendant(
        destination_run_dir,
        root=root,
        description="private restore destination",
        may_not_exist=True,
    )
    if target.exists() or target.is_symlink():
        raise PrivateStateError("private restore destination already exists")

    root_mode, directories, files = _descriptor_entries(
        descriptor,
        limits=limits,
    )
    descriptor_bytes = canonical_json_bytes(descriptor)
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PrivateStateError("private state descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    by_digest = _resolved_objects(objects)
    restored_files: list[tuple[PurePosixPath, bytes, int]] = []
    restored_total = 0
    for path, digest, expected_size, mode in files:
        content = by_digest.get(digest)
        if (
            content is None
            or len(content) != expected_size
            or sha256_digest(content) != digest
        ):
            raise PrivateStateError("private state object digest or size is invalid")
        scanner.require_clean_chunks((content,))
        rebound = _rebind(
            content,
            private_root=root,
            destination_run_dir=target,
            workspace_root=workspace,
        )
        scanner.require_clean_chunks((rebound,))
        if len(rebound) > limits.max_file_bytes:
            raise PrivateStateError("restored private state file exceeds its limit")
        restored_total += len(rebound)
        if restored_total > limits.max_total_bytes:
            raise PrivateStateError("restored private state exceeds its total limit")
        restored_files.append((path, rebound, mode))

    staging = root / f".private-state-{uuid4().hex}.tmp"
    created_parents: list[Path] = []
    published = False
    try:
        staging.mkdir(mode=0o700)
        staging.chmod(root_mode)
        for path, mode in sorted(
            directories,
            key=lambda entry: (len(entry[0].parts), entry[0].as_posix()),
        ):
            destination = staging.joinpath(*path.parts)
            destination.mkdir(mode=mode)
            destination.chmod(mode)
        for path, content, mode in restored_files:
            destination = staging.joinpath(*path.parts)
            _write_file(destination, content, mode)
        for path, _mode in sorted(
            directories,
            key=lambda entry: (-len(entry[0].parts), entry[0].as_posix()),
        ):
            _fsync_directory(staging.joinpath(*path.parts))
        _fsync_directory(staging)

        parent = root
        for part in target_relative.parts[:-1]:
            parent = parent / part
            if parent.exists():
                if parent.is_symlink() or not parent.is_dir():
                    raise PrivateStateError(
                        "private restore destination parent is unsafe"
                    )
                _safe_mode(parent.stat(follow_symlinks=False), directory=True)
                continue
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            created_parents.append(parent)
        if target.exists() or target.is_symlink():
            raise PrivateStateError("private restore destination already exists")
        staging.rename(target)
        published = True
        _fsync_directory(target.parent)
    except PrivateStateError:
        raise
    except OSError as exc:
        raise PrivateStateError(
            "private state could not be restored atomically"
        ) from exc
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
            for parent in reversed(created_parents):
                try:
                    parent.rmdir()
                except OSError:
                    break
    return target.resolve(strict=True)


__all__ = [
    "PRIVATE_ROOT_TOKEN",
    "PRIVATE_RUN_TOKEN",
    "PRIVATE_STATE_WORKFLOW_KEY",
    "WORKSPACE_ROOT_TOKEN",
    "PrivateStateError",
    "PrivateStateLimits",
    "PrivateStateSnapshot",
    "restore_private_run_directory",
    "snapshot_private_run_directory",
]

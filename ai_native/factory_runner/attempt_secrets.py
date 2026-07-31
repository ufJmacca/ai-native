"""Construct an attempt-scoped scanner from admitted credential sources."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from types import MappingProxyType

from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner


_MAX_SECRET_BYTES = 64 * 1024
_MAX_SECRET_SOURCES = 256
_MAX_TOTAL_SECRET_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 8192
_FILE_SUFFIXES = (
    "TOKEN_FILE",
    "SECRET_FILE",
    "CREDENTIAL_FILE",
)
_DIRECT_SUFFIXES = (
    "TOKEN",
    "SECRET",
    "CREDENTIAL",
    "CREDENTIALS",
    "PASSWORD",
    "API_KEY",
    "ACCESS_KEY",
    "ACCESS_KEY_ID",
    "PRIVATE_KEY",
    "AUTHORIZATION",
)


class AttemptSecretSourceError(FactoryPolicyViolation):
    """An admitted credential source cannot be scanned safely."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"attempt secret source rejected: {reason}")


class AttemptSecretRuntimeError(RuntimeError):
    """Runner-owned credential staging failed for a local infrastructure reason."""


@dataclass(frozen=True, slots=True)
class AttemptSecretAdmission:
    """Credential bytes and environment values admitted as one immutable view."""

    scanner: SecretScanner
    environment: Mapping[str, str] = field(repr=False)
    file_values: Mapping[str, bytes] = field(repr=False)


def _matches_suffix(key: str, suffixes: tuple[str, ...]) -> bool:
    normalized = key.upper()
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}") for suffix in suffixes
    )


def _validated_environment(
    environment: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    try:
        items = tuple(environment.items())
    except Exception:
        raise AttemptSecretSourceError("invalid-environment") from None
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or "\0" in key
        or "\0" in value
        for key, value in items
    ):
        raise AttemptSecretSourceError("invalid-environment")
    return tuple(sorted(items, key=lambda item: (item[0].upper(), item[0])))


def _safe_close(descriptor: int | None) -> None:
    if descriptor is not None:
        with suppress(OSError):
            os.close(descriptor)


def _read_secret_file(configured_path: str) -> bytes:
    try:
        path = Path(configured_path)
    except (OSError, ValueError):
        raise AttemptSecretSourceError("unsafe-credential-file") from None
    if (
        not path.is_absolute()
        or path.anchor != os.sep
        or not path.name
        or ".." in path.parts
    ):
        raise AttemptSecretSourceError("unsafe-credential-file")

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_only:
        raise AttemptSecretSourceError("unsupported-no-follow-platform")
    common_flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    directory_flags = common_flags | directory_only
    file_flags = common_flags | getattr(os, "O_NONBLOCK", 0)

    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = os.open(os.sep, directory_flags)
        components = path.parts[1:]
        for component in components[:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                _safe_close(next_descriptor)
                raise AttemptSecretSourceError("unsafe-credential-file")
            _safe_close(directory_descriptor)
            directory_descriptor = next_descriptor

        filename = components[-1]
        before = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode):
            raise AttemptSecretSourceError("unsafe-credential-file")
        if stat.S_IMODE(before.st_mode) & 0o444 == 0:
            raise AttemptSecretSourceError("unreadable-credential-file")
        if before.st_size > _MAX_SECRET_BYTES:
            raise AttemptSecretSourceError("oversized-credential-file")

        file_descriptor = os.open(
            filename,
            file_flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise AttemptSecretSourceError("unstable-credential-file")

        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(file_descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > _MAX_SECRET_BYTES:
                raise AttemptSecretSourceError("oversized-credential-file")
            chunks.append(chunk)

        after = os.fstat(file_descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or consumed != opened.st_size
        ):
            raise AttemptSecretSourceError("unstable-credential-file")
    except AttemptSecretSourceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise AttemptSecretSourceError("unreadable-credential-file") from None
    finally:
        _safe_close(file_descriptor)
        _safe_close(directory_descriptor)

    content = b"".join(chunks).rstrip(b"\r\n")
    if not content:
        raise AttemptSecretSourceError("empty-credential-file")
    return content


def admit_attempt_secrets(
    environment: Mapping[str, str],
) -> AttemptSecretAdmission:
    """Read every attempt secret once and retain the exact admitted file bytes."""

    values: list[bytes] = []
    observed: set[bytes] = set()
    file_values: dict[str, bytes] = {}
    total_bytes = 0
    environment_items = _validated_environment(environment)
    for key, raw_value in environment_items:
        if _matches_suffix(key, _FILE_SUFFIXES):
            value = _read_secret_file(raw_value)
            file_values[key] = value
        elif _matches_suffix(key, _DIRECT_SUFFIXES):
            try:
                value = raw_value.encode("utf-8", errors="strict")
            except UnicodeError:
                raise AttemptSecretSourceError("invalid-direct-secret") from None
            if not value:
                continue
            if len(value) > _MAX_SECRET_BYTES:
                raise AttemptSecretSourceError("oversized-direct-secret")
        else:
            continue

        if value in observed:
            continue
        if len(values) >= _MAX_SECRET_SOURCES:
            raise AttemptSecretSourceError("too-many-secret-sources")
        total_bytes += len(value)
        if total_bytes > _MAX_TOTAL_SECRET_BYTES:
            raise AttemptSecretSourceError("aggregate-secret-limit")
        observed.add(value)
        values.append(value)

    exact_canaries = tuple(
        (f"attempt-secret-{index:04d}", value)
        for index, value in enumerate(values, start=1)
    )
    return AttemptSecretAdmission(
        scanner=SecretScanner(SecretPolicy(exact_canaries=exact_canaries)),
        environment=MappingProxyType(dict(environment_items)),
        file_values=MappingProxyType(dict(file_values)),
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def materialize_attempt_secret_files(
    admission: AttemptSecretAdmission,
    *,
    destination: Path,
) -> dict[str, str]:
    """Pin admitted credential-file bytes below a new runner-owned directory."""

    if not isinstance(admission, AttemptSecretAdmission):
        raise TypeError("admission must be an AttemptSecretAdmission")
    candidate = Path(destination)
    if (
        not candidate.is_absolute()
        or candidate == Path(candidate.anchor)
        or ".." in candidate.parts
        or not candidate.name
    ):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory")
    try:
        if candidate.is_symlink():
            raise AttemptSecretSourceError("unsafe-pinned-secret-directory")
        parent = candidate.parent.resolve(strict=True)
        parent_metadata = parent.stat(follow_symlinks=False)
    except AttemptSecretSourceError:
        raise
    except (OSError, RuntimeError):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory") from None
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory")
    if Path(os.path.abspath(candidate.parent)) != parent:
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory")

    parent_descriptor: int | None = None
    directory_descriptor: int | None = None
    created_names: list[str] = []
    created_directory = False
    successful = False
    try:
        parent_descriptor = os.open(parent, _directory_open_flags())
        os.mkdir(candidate.name, mode=0o700, dir_fd=parent_descriptor)
        created_directory = True
        directory_descriptor = os.open(
            candidate.name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AttemptSecretSourceError("unsafe-pinned-secret-directory")

        rewritten = dict(admission.environment)
        for index, (key, content) in enumerate(
            sorted(admission.file_values.items()),
            start=1,
        ):
            filename = f"credential-{index:04d}"
            descriptor = os.open(
                filename,
                (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                ),
                0o600,
                dir_fd=directory_descriptor,
            )
            created_names.append(filename)
            try:
                view = memoryview(content)
                written = 0
                while written < len(view):
                    consumed = os.write(descriptor, view[written:])
                    if consumed <= 0:
                        raise OSError("pinned credential write made no progress")
                    written += consumed
                os.fsync(descriptor)
                file_metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(file_metadata.st_mode)
                    or stat.S_IMODE(file_metadata.st_mode) != 0o600
                    or file_metadata.st_size != len(content)
                ):
                    raise AttemptSecretSourceError("unsafe-pinned-secret-file")
            finally:
                os.close(descriptor)
            rewritten[key] = str(candidate / filename)
        os.fsync(directory_descriptor)
        os.fsync(parent_descriptor)
        successful = True
        return rewritten
    except AttemptSecretSourceError:
        raise
    except (FileExistsError, OSError, RuntimeError, ValueError):
        raise AttemptSecretRuntimeError(
            "pinned secret materialization failed"
        ) from None
    finally:
        if directory_descriptor is not None:
            for filename in reversed(created_names):
                if successful:
                    # Successful materialization retains the files for the runner.
                    continue
                with suppress(OSError):
                    os.unlink(filename, dir_fd=directory_descriptor)
            _safe_close(directory_descriptor)
        if created_directory and parent_descriptor is not None and not successful:
            with suppress(OSError):
                os.rmdir(candidate.name, dir_fd=parent_descriptor)
        _safe_close(parent_descriptor)


def remove_materialized_attempt_secret_files(
    admission: AttemptSecretAdmission,
    *,
    destination: Path,
) -> None:
    """Remove one runner-owned pinned credential directory without following links."""

    if not isinstance(admission, AttemptSecretAdmission):
        raise TypeError("admission must be an AttemptSecretAdmission")
    candidate = Path(destination)
    if (
        not candidate.is_absolute()
        or candidate == Path(candidate.anchor)
        or ".." in candidate.parts
        or not candidate.name
    ):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory")
    try:
        parent = candidate.parent.resolve(strict=True)
        parent_metadata = parent.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory") from None
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or Path(os.path.abspath(candidate.parent)) != parent
    ):
        raise AttemptSecretSourceError("unsafe-pinned-secret-directory")

    parent_descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        parent_descriptor = os.open(parent, _directory_open_flags())
        directory_descriptor = os.open(
            candidate.name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        expected_names = {
            f"credential-{index:04d}"
            for index, _item in enumerate(
                sorted(admission.file_values.items()),
                start=1,
            )
        }
        if set(os.listdir(directory_descriptor)) != expected_names:
            raise AttemptSecretSourceError("unsafe-pinned-secret-directory")
        for filename in sorted(expected_names):
            os.unlink(filename, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        os.close(directory_descriptor)
        directory_descriptor = None
        os.rmdir(candidate.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except AttemptSecretSourceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise AttemptSecretRuntimeError("pinned secret removal failed") from None
    finally:
        _safe_close(directory_descriptor)
        _safe_close(parent_descriptor)


def build_attempt_secret_scanner(
    environment: Mapping[str, str],
) -> SecretScanner:
    """Build a scanner for builtin and attempt-specific credential canaries."""

    return admit_attempt_secrets(environment).scanner


__all__ = [
    "AttemptSecretAdmission",
    "AttemptSecretRuntimeError",
    "AttemptSecretSourceError",
    "admit_attempt_secrets",
    "build_attempt_secret_scanner",
    "materialize_attempt_secret_files",
    "remove_materialized_attempt_secret_files",
]

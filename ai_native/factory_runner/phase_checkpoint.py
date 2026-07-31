"""Portable checkpoint snapshots for completed TDD phase evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.checkpoint_runtime import CheckpointStateObject
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    freeze_mapping,
)
from ai_native.factory_runner.contracts.verification_evidence import EvidenceItem
from ai_native.factory_runner.outputs import OutputWriter
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner
from ai_native.factory_runner.verification import PhaseExecutionOutcome


PHASE_EVIDENCE_WORKFLOW_KEY = "phase_evidence"

_SCHEMA = "phase-evidence-state/v1"
_OUTCOME_SCHEMA = "phase-execution-outcomes/v1"
_AUTHOR_PHASES = ("red", "green", "refactor", "verification")
_VERIFY_PHASES = ("verification",)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 262_144
_HARD_MAX_ARTIFACTS = 4096
_HARD_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_HARD_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class PhaseEvidenceError(FactoryPolicyViolation):
    """Phase evidence cannot be checkpointed or restored without ambiguity."""


@dataclass(frozen=True, slots=True)
class PhaseEvidenceLimits:
    max_artifacts: int = 1024
    max_artifact_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        limits = (
            (self.max_artifacts, _HARD_MAX_ARTIFACTS),
            (self.max_artifact_bytes, _HARD_MAX_ARTIFACT_BYTES),
            (self.max_total_bytes, _HARD_MAX_TOTAL_BYTES),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
            for value, maximum in limits
        ):
            raise ValueError("phase evidence limits must be positive and bounded")
        if self.max_artifact_bytes > self.max_total_bytes:
            raise ValueError(
                "phase artifact limit may not exceed the phase total limit"
            )


@dataclass(frozen=True, slots=True)
class PhaseEvidenceSnapshot:
    descriptor: Mapping[str, Any]
    objects: tuple[CheckpointStateObject, ...]


def _path(value: str, *, evidence: bool) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or _CONTROL_PATTERN.search(value) is not None
    ):
        raise PhaseEvidenceError("phase evidence artifact path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
    ):
        raise PhaseEvidenceError("phase evidence artifact path is unsafe")
    if evidence and path.parts[:2] != ("evidence", "objects"):
        raise PhaseEvidenceError(
            "phase evidence artifacts must remain beneath evidence/objects"
        )
    return path


def _reference_tuple(item: EvidenceItem) -> tuple[ArtifactReference, ...]:
    return (item.stdout, item.stderr, *item.test_reports)


def _validated_outcomes(
    inputs: Any,
    outcomes: Sequence[PhaseExecutionOutcome],
) -> tuple[tuple[PhaseExecutionOutcome, ...], tuple[ArtifactReference, ...]]:
    if isinstance(outcomes, str | bytes | bytearray):
        raise PhaseEvidenceError("phase outcomes must be an ordered sequence")
    operation = getattr(getattr(inputs, "run_spec", None), "operation", None)
    policy = getattr(getattr(inputs, "run_spec", None), "policy", None)
    commands = tuple(
        tuple(command) for command in getattr(policy, "allowed_commands", ())
    )
    environment_keys = tuple(getattr(policy, "allowed_environment_keys", ()))
    expected_phases = (
        _AUTHOR_PHASES
        if operation == "author"
        else _VERIFY_PHASES
        if operation == "verify"
        else ()
    )
    copied = tuple(outcomes)
    phases = tuple(getattr(outcome, "phase", None) for outcome in copied)
    if not expected_phases or phases != expected_phases[: len(phases)]:
        raise PhaseEvidenceError(
            "phase evidence order is incompatible with the admitted operation"
        )
    if not commands and copied:
        raise PhaseEvidenceError(
            "phase evidence requires admitted deterministic commands"
        )

    references: list[ArtifactReference] = []
    reference_paths: set[str] = set()
    validated: list[PhaseExecutionOutcome] = []
    for outcome in copied:
        if not isinstance(outcome, PhaseExecutionOutcome):
            raise PhaseEvidenceError("phase outcome type is invalid")
        if (
            not isinstance(outcome.passed, bool)
            or not isinstance(outcome.cancelled, bool)
            or not isinstance(outcome.timed_out, bool)
            or outcome.cancelled
            and outcome.timed_out
            or not outcome.items
        ):
            raise PhaseEvidenceError("phase outcome status is invalid")
        try:
            items = tuple(
                EvidenceItem.model_validate(item.model_dump(mode="json"))
                for item in outcome.items
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise PhaseEvidenceError("phase evidence item is invalid") from exc
        if any(item.phase != outcome.phase for item in items) or len(items) > len(
            commands
        ):
            raise PhaseEvidenceError(
                "phase outcome contains an incompatible phase or command count"
            )
        if outcome.phase == "red" and len(items) != 1:
            raise PhaseEvidenceError("red phase must bind exactly one command")
        if (
            outcome.phase != "red"
            and outcome.passed
            and not outcome.cancelled
            and not outcome.timed_out
            and len(items) != len(commands)
        ):
            raise PhaseEvidenceError(
                "completed phase does not bind every admitted command"
            )

        for index, item in enumerate(items, start=1):
            if tuple(item.command) != commands[index - 1]:
                raise PhaseEvidenceError(
                    "phase evidence command differs from admitted authority"
                )
            if tuple(item.environment_keys) != environment_keys:
                raise PhaseEvidenceError(
                    "phase evidence environment differs from admitted authority"
                )
            if item.working_directory != ".":
                raise PhaseEvidenceError(
                    "phase evidence working directory is incompatible"
                )
            expected_prefix = f"evidence/objects/{outcome.phase}-command-{index:03d}"
            if (
                item.stdout.path != expected_prefix + ".stdout"
                or item.stderr.path != expected_prefix + ".stderr"
            ):
                raise PhaseEvidenceError(
                    "phase evidence stdout or stderr path is incompatible"
                )
            for reference in _reference_tuple(item):
                _path(reference.path, evidence=True)
                if reference.path in reference_paths:
                    raise PhaseEvidenceError(
                        "phase evidence contains a duplicate artifact path"
                    )
                reference_paths.add(reference.path)
                references.append(reference)

        calculated_passed = (
            items[0].failure_classification == "expected_behavioral_failure"
            if outcome.phase == "red"
            else all(item.actual_status == "passed" for item in items)
        )
        if outcome.passed != calculated_passed:
            raise PhaseEvidenceError(
                "phase outcome status differs from its evidence items"
            )
        validated.append(
            PhaseExecutionOutcome(
                phase=outcome.phase,
                passed=outcome.passed,
                cancelled=outcome.cancelled,
                timed_out=outcome.timed_out,
                items=items,
            )
        )
    return tuple(validated), tuple(references)


def _output_root(inputs: Any, writer: OutputWriter) -> Path:
    if not isinstance(writer, OutputWriter):
        raise TypeError("writer must be an OutputWriter")
    candidate = Path(writer.root)
    expected = Path(getattr(inputs, "output_dir", ""))
    try:
        if candidate.is_symlink() or expected.is_symlink():
            raise PhaseEvidenceError("phase evidence output root is a symbolic link")
        root = candidate.resolve(strict=True)
        expected_root = expected.resolve(strict=True)
        metadata = root.stat(follow_symlinks=False)
    except PhaseEvidenceError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PhaseEvidenceError("phase evidence output root is unavailable") from exc
    if root != expected_root or not stat.S_ISDIR(metadata.st_mode):
        raise PhaseEvidenceError(
            "phase evidence writer differs from the admitted output root"
        )
    return root


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _read_artifact(
    root_fd: int,
    reference: ArtifactReference,
    *,
    limits: PhaseEvidenceLimits,
) -> bytes:
    path = _path(reference.path, evidence=True)
    current_fd = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PhaseEvidenceError(
                        "phase evidence path traverses a link"
                    ) from exc
                raise PhaseEvidenceError(
                    "phase evidence artifact is unavailable"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        name = path.parts[-1]
        try:
            before = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        except OSError as exc:
            raise PhaseEvidenceError("phase evidence artifact is unavailable") from exc
        if not stat.S_ISREG(before.st_mode):
            raise PhaseEvidenceError(
                "phase evidence artifact must be a regular file, not a link"
            )
        if (
            before.st_size != reference.byte_size
            or before.st_size > limits.max_artifact_bytes
        ):
            raise PhaseEvidenceError(
                "phase evidence artifact size exceeds its limit or reference"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=current_fd)
        except OSError as exc:
            raise PhaseEvidenceError(
                "phase evidence artifact could not be opened without following links"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise PhaseEvidenceError(
                    "phase evidence artifact changed during validation"
                )
            chunks: list[bytes] = []
            consumed = 0
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > limits.max_artifact_bytes:
                    raise PhaseEvidenceError(
                        "phase evidence artifact exceeds its size limit"
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(current_fd)
    content = b"".join(chunks)
    if (
        (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or len(content) != reference.byte_size
        or sha256_digest(content) != reference.digest
    ):
        raise PhaseEvidenceError("phase evidence artifact digest or identity mismatch")
    return content


def _outcome_payload(outcome: PhaseExecutionOutcome) -> dict[str, Any]:
    return {
        "phase": outcome.phase,
        "passed": outcome.passed,
        "cancelled": outcome.cancelled,
        "timed_out": outcome.timed_out,
        "items": [item.model_dump(mode="json") for item in outcome.items],
    }


def snapshot_phase_evidence(
    inputs: Any,
    *,
    writer: OutputWriter,
    phase_outcomes: Sequence[PhaseExecutionOutcome],
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
) -> PhaseEvidenceSnapshot:
    """Detach completed phase evidence and every referenced output artifact."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    outcomes, references = _validated_outcomes(inputs, phase_outcomes)
    if len(references) > limits.max_artifacts:
        raise PhaseEvidenceError("phase evidence artifact count exceeds its limit")
    outcome_document = canonical_json_bytes(
        {
            "schema": _OUTCOME_SCHEMA,
            "outcomes": [_outcome_payload(outcome) for outcome in outcomes],
        }
    )
    if len(outcome_document) > limits.max_artifact_bytes:
        raise PhaseEvidenceError("phase outcome state exceeds its artifact size limit")
    scanner.require_clean_chunks((outcome_document,))
    outcome_object = CheckpointStateObject(
        content=outcome_document,
        media_type="application/json",
    )
    root = _output_root(inputs, writer)
    try:
        root_fd = os.open(root, _directory_flags())
    except OSError as exc:
        raise PhaseEvidenceError(
            "phase evidence output root could not be opened safely"
        ) from exc
    objects: dict[str, CheckpointStateObject] = {
        outcome_object.digest: outcome_object,
    }
    total_bytes = outcome_object.byte_size
    if total_bytes > limits.max_total_bytes:
        raise PhaseEvidenceError("phase outcome state exceeds its total size limit")
    try:
        for reference in references:
            content = _read_artifact(root_fd, reference, limits=limits)
            scanner.require_clean_chunks((content,))
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise PhaseEvidenceError(
                    "phase evidence artifacts exceed their total size limit"
                )
            state_object = CheckpointStateObject(
                content=content,
                media_type=reference.media_type,
            )
            objects.setdefault(state_object.digest, state_object)
    finally:
        os.close(root_fd)

    sorted_references = tuple(sorted(references, key=lambda item: item.path))
    descriptor = {
        "schema": _SCHEMA,
        "outcome_state": {
            "object_digest": outcome_object.digest,
            "byte_size": outcome_object.byte_size,
        },
        "artifacts": [
            reference.model_dump(mode="json") for reference in sorted_references
        ],
    }
    descriptor_bytes = canonical_json_bytes(descriptor)
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PhaseEvidenceError("phase evidence descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    return PhaseEvidenceSnapshot(
        descriptor=freeze_mapping(descriptor),
        objects=tuple(objects[digest] for digest in sorted(objects)),
    )


def _parse_outcome_document(
    content: bytes,
) -> tuple[PhaseExecutionOutcome, ...]:
    try:
        decoded = json.loads(content.decode("utf-8", errors="strict"))
        if (
            not isinstance(decoded, Mapping)
            or set(decoded) != {"schema", "outcomes"}
            or decoded.get("schema") != _OUTCOME_SCHEMA
            or canonical_json_bytes(decoded) != content
        ):
            raise PhaseEvidenceError(
                "phase outcome state is not canonical or schema-valid"
            )
        raw_outcomes = decoded["outcomes"]
    except PhaseEvidenceError:
        raise
    except (TypeError, UnicodeError, ValueError) as exc:
        raise PhaseEvidenceError("phase outcome state is invalid JSON") from exc
    if isinstance(raw_outcomes, str | bytes | bytearray) or not isinstance(
        raw_outcomes, Sequence
    ):
        raise PhaseEvidenceError("phase outcome state list is invalid")
    outcomes: list[PhaseExecutionOutcome] = []
    try:
        for raw in raw_outcomes:
            if not isinstance(raw, Mapping) or set(raw) != {
                "phase",
                "passed",
                "cancelled",
                "timed_out",
                "items",
            }:
                raise PhaseEvidenceError("phase evidence outcome descriptor is invalid")
            for field_name in ("passed", "cancelled", "timed_out"):
                if not isinstance(raw[field_name], bool):
                    raise PhaseEvidenceError("phase evidence outcome status is invalid")
            raw_items = raw["items"]
            if isinstance(raw_items, str | bytes | bytearray) or not isinstance(
                raw_items, Sequence
            ):
                raise PhaseEvidenceError("phase evidence item list is invalid")
            outcomes.append(
                PhaseExecutionOutcome(
                    phase=raw["phase"],
                    passed=raw["passed"],
                    cancelled=raw["cancelled"],
                    timed_out=raw["timed_out"],
                    items=tuple(
                        EvidenceItem.model_validate(item) for item in raw_items
                    ),
                )
            )
    except PhaseEvidenceError:
        raise
    except (TypeError, ValueError) as exc:
        raise PhaseEvidenceError("phase outcome state contract is invalid") from exc
    return tuple(outcomes)


def _descriptor_header(
    descriptor: Mapping[str, Any],
    *,
    limits: PhaseEvidenceLimits,
    scanner: SecretScanner,
) -> tuple[
    str,
    int,
    tuple[ArtifactReference, ...],
]:
    try:
        descriptor_bytes = canonical_json_bytes(descriptor)
    except ValueError as exc:
        raise PhaseEvidenceError(
            "phase evidence descriptor is not portable JSON"
        ) from exc
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PhaseEvidenceError("phase evidence descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    if set(descriptor) != {"schema", "outcome_state", "artifacts"} or (
        descriptor.get("schema") != _SCHEMA
    ):
        raise PhaseEvidenceError("phase evidence descriptor schema is invalid")
    outcome_state = descriptor["outcome_state"]
    raw_artifacts = descriptor["artifacts"]
    if (
        not isinstance(outcome_state, Mapping)
        or set(outcome_state) != {"object_digest", "byte_size"}
        or isinstance(raw_artifacts, str | bytes | bytearray)
        or not isinstance(raw_artifacts, Sequence)
    ):
        raise PhaseEvidenceError("phase evidence descriptor bindings are invalid")
    outcome_digest = outcome_state["object_digest"]
    outcome_size = outcome_state["byte_size"]
    if (
        not isinstance(outcome_digest, str)
        or _DIGEST_PATTERN.fullmatch(outcome_digest) is None
        or isinstance(outcome_size, bool)
        or not isinstance(outcome_size, int)
        or not 0 <= outcome_size <= limits.max_artifact_bytes
    ):
        raise PhaseEvidenceError("phase outcome state digest or size is invalid")
    try:
        artifacts = tuple(
            ArtifactReference.model_validate(reference) for reference in raw_artifacts
        )
    except (TypeError, ValueError) as exc:
        raise PhaseEvidenceError(
            "phase evidence artifact descriptor is invalid"
        ) from exc
    if len(artifacts) > limits.max_artifacts:
        raise PhaseEvidenceError("phase evidence artifact count exceeds its limit")
    paths = tuple(reference.path for reference in artifacts)
    if len(set(paths)) != len(paths):
        raise PhaseEvidenceError(
            "phase evidence descriptor contains a duplicate artifact path"
        )
    if paths != tuple(sorted(paths)):
        raise PhaseEvidenceError("phase evidence artifact descriptor order is invalid")
    for reference in artifacts:
        _path(reference.path, evidence=True)
    return outcome_digest, outcome_size, artifacts


def _validate_reference_consistency(
    inputs: Any,
    outcomes: Sequence[PhaseExecutionOutcome],
    artifacts: Sequence[ArtifactReference],
) -> tuple[PhaseExecutionOutcome, ...]:
    validated, used_references = _validated_outcomes(inputs, outcomes)
    declared = tuple(reference.model_dump(mode="json") for reference in artifacts)
    used = tuple(
        reference.model_dump(mode="json")
        for reference in sorted(used_references, key=lambda item: item.path)
    )
    if declared != used:
        raise PhaseEvidenceError(
            "phase evidence descriptor artifact references are inconsistent"
        )
    return validated


def _object_content(
    objects: Mapping[str, bytes],
    *,
    required: frozenset[str],
) -> Mapping[str, bytes]:
    if not isinstance(objects, Mapping):
        raise TypeError("objects must be a checkpoint object mapping")
    by_digest: dict[str, bytes] = {}
    hard_total = 0
    for path, content in objects.items():
        _path(path, evidence=False)
        if not isinstance(content, bytes):
            raise PhaseEvidenceError("phase checkpoint object must be bytes")
        if len(content) > _HARD_MAX_ARTIFACT_BYTES:
            raise PhaseEvidenceError("phase checkpoint object exceeds its size limit")
        hard_total += len(content)
        if hard_total > _HARD_MAX_TOTAL_BYTES:
            raise PhaseEvidenceError(
                "phase checkpoint objects exceed their total size limit"
            )
        digest = sha256_digest(content)
        if digest in required:
            previous = by_digest.setdefault(digest, content)
            if previous != content:
                raise PhaseEvidenceError("phase checkpoint object digest is ambiguous")
    return MappingProxyType(by_digest)


def _require_targets_absent(
    root: Path, references: Sequence[ArtifactReference]
) -> None:
    for reference in references:
        path = _path(reference.path, evidence=True)
        current = root
        for component in path.parts[:-1]:
            current = current / component
            if not current.exists() and not current.is_symlink():
                break
            if current.is_symlink() or not current.is_dir():
                raise PhaseEvidenceError(
                    "phase restore target traverses an unsafe path"
                )
        else:
            target = current / path.parts[-1]
            if target.exists() or target.is_symlink():
                raise PhaseEvidenceError("phase restore requires fresh artifact paths")


def restore_phase_evidence(
    inputs: Any,
    *,
    writer: OutputWriter,
    descriptor: Mapping[str, Any],
    objects: Mapping[str, bytes],
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
) -> tuple[PhaseExecutionOutcome, ...]:
    """Validate and restore exact phase outcomes into a fresh output writer."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(descriptor, Mapping):
        raise PhaseEvidenceError("phase evidence descriptor must be an object")
    outcome_digest, outcome_size, references = _descriptor_header(
        descriptor,
        limits=limits,
        scanner=scanner,
    )
    required = frozenset(
        (
            outcome_digest,
            *(reference.digest for reference in references),
        )
    )
    by_digest = _object_content(objects, required=required)
    outcome_content = by_digest.get(outcome_digest)
    if (
        outcome_content is None
        or len(outcome_content) != outcome_size
        or sha256_digest(outcome_content) != outcome_digest
    ):
        raise PhaseEvidenceError("phase outcome state object digest or size is invalid")
    scanner.require_clean_chunks((outcome_content,))
    outcomes = _validate_reference_consistency(
        inputs,
        _parse_outcome_document(outcome_content),
        references,
    )
    contents: dict[str, bytes] = {}
    total_bytes = len(outcome_content)
    if total_bytes > limits.max_total_bytes:
        raise PhaseEvidenceError("phase outcome state exceeds its total size limit")
    for reference in references:
        content = by_digest.get(reference.digest)
        if (
            content is None
            or len(content) != reference.byte_size
            or sha256_digest(content) != reference.digest
            or len(content) > limits.max_artifact_bytes
        ):
            raise PhaseEvidenceError("phase evidence object digest or size is invalid")
        scanner.require_clean_chunks((content,))
        total_bytes += len(content)
        if total_bytes > limits.max_total_bytes:
            raise PhaseEvidenceError(
                "phase evidence objects exceed their total size limit"
            )
        contents[reference.path] = content

    root = _output_root(inputs, writer)
    _require_targets_absent(root, references)
    for reference in references:
        restored = writer.write_bytes(
            reference.path,
            contents[reference.path],
            media_type=reference.media_type,
        )
        if restored != reference:
            raise PhaseEvidenceError(
                "restored phase artifact reference differs from its checkpoint"
            )
    return outcomes


__all__ = [
    "PHASE_EVIDENCE_WORKFLOW_KEY",
    "PhaseEvidenceError",
    "PhaseEvidenceLimits",
    "PhaseEvidenceSnapshot",
    "restore_phase_evidence",
    "snapshot_phase_evidence",
]

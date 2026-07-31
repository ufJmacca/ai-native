"""Portable checkpoint snapshots for completed TDD phase evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
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
from ai_native.factory_runner.verification import (
    PhaseEvidenceAuthority,
    PhaseExecutionOutcome,
    RedAlreadyGreenObservation,
)


PHASE_EVIDENCE_WORKFLOW_KEY = "phase_evidence"
ALREADY_GREEN_WORKFLOW_KEY = "already_green_observation"

_SCHEMA = "phase-evidence-state/v1"
_OUTCOME_SCHEMA = "phase-execution-outcomes/v1"
_ALREADY_GREEN_SCHEMA = "already-green-observation-state/v1"
_ALREADY_GREEN_DOCUMENT_SCHEMA = "already-green-observation/v1"
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


def _current_evidence_authority(inputs: Any) -> PhaseEvidenceAuthority:
    policy = getattr(getattr(inputs, "run_spec", None), "policy", None)
    try:
        return PhaseEvidenceAuthority(
            allowed_commands=tuple(
                tuple(command) for command in getattr(policy, "allowed_commands", ())
            ),
            allowed_environment_keys=tuple(
                getattr(policy, "allowed_environment_keys", ())
            ),
        )
    except (TypeError, ValueError) as exc:
        raise PhaseEvidenceError(
            "phase evidence producer authority is invalid"
        ) from exc


def _authority_payload(authority: PhaseEvidenceAuthority) -> dict[str, Any]:
    if not isinstance(authority, PhaseEvidenceAuthority):
        raise PhaseEvidenceError("phase evidence producer authority is invalid")
    return {
        "allowed_commands": [list(command) for command in authority.allowed_commands],
        "allowed_environment_keys": list(authority.allowed_environment_keys),
    }


def _parse_authority(value: Any) -> PhaseEvidenceAuthority:
    if not isinstance(value, Mapping) or set(value) != {
        "allowed_commands",
        "allowed_environment_keys",
    }:
        raise PhaseEvidenceError("phase evidence producer authority is invalid")
    raw_commands = value["allowed_commands"]
    raw_environment_keys = value["allowed_environment_keys"]
    if (
        isinstance(raw_commands, str | bytes | bytearray)
        or not isinstance(raw_commands, Sequence)
        or isinstance(raw_environment_keys, str | bytes | bytearray)
        or not isinstance(raw_environment_keys, Sequence)
    ):
        raise PhaseEvidenceError("phase evidence producer authority is invalid")
    commands: list[tuple[str, ...]] = []
    for raw_command in raw_commands:
        if isinstance(raw_command, str | bytes | bytearray) or not isinstance(
            raw_command, Sequence
        ):
            raise PhaseEvidenceError("phase evidence producer authority is invalid")
        commands.append(tuple(raw_command))
    try:
        return PhaseEvidenceAuthority(
            allowed_commands=tuple(commands),
            allowed_environment_keys=tuple(raw_environment_keys),
        )
    except (TypeError, ValueError) as exc:
        raise PhaseEvidenceError(
            "phase evidence producer authority is invalid"
        ) from exc


def _resolved_evidence_authority(
    current: PhaseEvidenceAuthority,
    persisted: PhaseEvidenceAuthority | None,
    *,
    enforce_current_policy: bool,
) -> PhaseEvidenceAuthority:
    authority = current if persisted is None else persisted
    if enforce_current_policy:
        if authority != current:
            raise PhaseEvidenceError(
                "phase evidence producer authority differs from current policy"
            )
    elif not set(current.allowed_commands).issubset(
        authority.allowed_commands
    ) or not set(current.allowed_environment_keys).issubset(
        authority.allowed_environment_keys
    ):
        raise PhaseEvidenceError(
            "current policy exceeds phase evidence producer authority"
        )
    return authority


def _validated_outcomes(
    inputs: Any,
    outcomes: Sequence[PhaseExecutionOutcome],
    *,
    enforce_current_policy: bool,
) -> tuple[tuple[PhaseExecutionOutcome, ...], tuple[ArtifactReference, ...]]:
    if isinstance(outcomes, str | bytes | bytearray):
        raise PhaseEvidenceError("phase outcomes must be an ordered sequence")
    operation = getattr(getattr(inputs, "run_spec", None), "operation", None)
    current_authority = _current_evidence_authority(inputs)
    expected_phases = (
        _AUTHOR_PHASES
        if operation == "author"
        else _VERIFY_PHASES
        if operation == "verify"
        else ()
    )
    copied = tuple(outcomes)
    phases = tuple(getattr(outcome, "phase", None) for outcome in copied)
    no_change_verification = operation == "author" and phases == ("verification",)
    if (
        not expected_phases
        or phases != expected_phases[: len(phases)]
        and not no_change_verification
    ):
        raise PhaseEvidenceError(
            "phase evidence order is incompatible with the admitted operation"
        )
    references: list[ArtifactReference] = []
    reference_paths: set[str] = set()
    validated: list[PhaseExecutionOutcome] = []
    for outcome in copied:
        if not isinstance(outcome, PhaseExecutionOutcome):
            raise PhaseEvidenceError("phase outcome type is invalid")
        authority = _resolved_evidence_authority(
            current_authority,
            outcome.evidence_authority,
            enforce_current_policy=enforce_current_policy,
        )
        commands = authority.allowed_commands
        environment_keys = authority.allowed_environment_keys
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
            if index > len(commands) or tuple(item.command) != commands[index - 1]:
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
                evidence_authority=authority,
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
    if outcome.evidence_authority is None:
        raise PhaseEvidenceError("phase outcome omits its producer authority")
    return {
        "phase": outcome.phase,
        "passed": outcome.passed,
        "cancelled": outcome.cancelled,
        "timed_out": outcome.timed_out,
        "items": [item.model_dump(mode="json") for item in outcome.items],
        "evidence_authority": _authority_payload(outcome.evidence_authority),
    }


def _validated_already_green(
    inputs: Any,
    observation: RedAlreadyGreenObservation,
    *,
    enforce_current_policy: bool,
) -> tuple[RedAlreadyGreenObservation, tuple[ArtifactReference, ...]]:
    if not isinstance(observation, RedAlreadyGreenObservation):
        raise PhaseEvidenceError("already-green observation type is invalid")
    run_spec = getattr(inputs, "run_spec", None)
    authority = _resolved_evidence_authority(
        _current_evidence_authority(inputs),
        observation.evidence_authority,
        enforce_current_policy=enforce_current_policy,
    )
    commands = authority.allowed_commands
    environment_keys = authority.allowed_environment_keys
    if (
        getattr(run_spec, "operation", None) != "author"
        or (
            not commands
            or observation.command != commands[0]
            or observation.environment_keys != environment_keys
        )
        or not isinstance(observation.started_at, str)
        or not observation.started_at
        or not isinstance(observation.finished_at, str)
        or not observation.finished_at
        or isinstance(observation.duration_seconds, bool)
        or not isinstance(observation.duration_seconds, int | float)
        or not 0 <= observation.duration_seconds < float("inf")
    ):
        raise PhaseEvidenceError("already-green observation authority is invalid")
    expected_prefix = "evidence/objects/red-command-001"
    if (
        observation.stdout.path != expected_prefix + ".stdout"
        or observation.stderr.path != expected_prefix + ".stderr"
    ):
        raise PhaseEvidenceError("already-green artifact paths are invalid")
    references = (observation.stdout, observation.stderr)
    for reference in references:
        _path(reference.path, evidence=True)
    return replace(observation, evidence_authority=authority), references


def _already_green_payload(
    observation: RedAlreadyGreenObservation,
) -> dict[str, Any]:
    if observation.evidence_authority is None:
        raise PhaseEvidenceError(
            "already-green observation omits its producer authority"
        )
    return {
        "schema": _ALREADY_GREEN_DOCUMENT_SCHEMA,
        "command": list(observation.command),
        "environment_keys": list(observation.environment_keys),
        "evidence_authority": _authority_payload(observation.evidence_authority),
        "started_at": observation.started_at,
        "finished_at": observation.finished_at,
        "duration_seconds": observation.duration_seconds,
        "exit_code": 0,
        "termination_reason": "exited",
        "repository_files_changed": False,
        "stdout": observation.stdout.model_dump(mode="json"),
        "stderr": observation.stderr.model_dump(mode="json"),
    }


def snapshot_already_green_observation(
    inputs: Any,
    *,
    writer: OutputWriter,
    observation: RedAlreadyGreenObservation,
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
    enforce_current_policy: bool = True,
) -> PhaseEvidenceSnapshot:
    """Detach the exact successful Red probe used to justify no-change."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(enforce_current_policy, bool):
        raise TypeError("enforce_current_policy must be a boolean")
    validated, references = _validated_already_green(
        inputs,
        observation,
        enforce_current_policy=enforce_current_policy,
    )
    document = canonical_json_bytes(_already_green_payload(validated))
    if len(document) > limits.max_artifact_bytes:
        raise PhaseEvidenceError("already-green state exceeds its artifact limit")
    scanner.require_clean_chunks((document,))
    state_object = CheckpointStateObject(
        content=document,
        media_type="application/json",
    )
    root = _output_root(inputs, writer)
    try:
        root_fd = os.open(root, _directory_flags())
    except OSError as exc:
        raise PhaseEvidenceError(
            "already-green output root could not be opened safely"
        ) from exc
    objects: dict[str, CheckpointStateObject] = {
        state_object.digest: state_object,
    }
    total_bytes = state_object.byte_size
    try:
        for reference in references:
            content = _read_artifact(root_fd, reference, limits=limits)
            scanner.require_clean_chunks((content,))
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise PhaseEvidenceError(
                    "already-green objects exceed their total limit"
                )
            detached = CheckpointStateObject(
                content=content,
                media_type=reference.media_type,
            )
            objects.setdefault(detached.digest, detached)
    finally:
        os.close(root_fd)
    descriptor = {
        "schema": _ALREADY_GREEN_SCHEMA,
        "observation_state": {
            "object_digest": state_object.digest,
            "byte_size": state_object.byte_size,
        },
        "artifacts": [
            reference.model_dump(mode="json")
            for reference in sorted(references, key=lambda item: item.path)
        ],
    }
    descriptor_bytes = canonical_json_bytes(descriptor)
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PhaseEvidenceError("already-green descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    return PhaseEvidenceSnapshot(
        descriptor=freeze_mapping(descriptor),
        objects=tuple(objects[digest] for digest in sorted(objects)),
    )


def snapshot_phase_evidence(
    inputs: Any,
    *,
    writer: OutputWriter,
    phase_outcomes: Sequence[PhaseExecutionOutcome],
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
    enforce_current_policy: bool = True,
) -> PhaseEvidenceSnapshot:
    """Detach completed phase evidence and every referenced output artifact."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(enforce_current_policy, bool):
        raise TypeError("enforce_current_policy must be a boolean")
    outcomes, references = _validated_outcomes(
        inputs,
        phase_outcomes,
        enforce_current_policy=enforce_current_policy,
    )
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
            if not isinstance(raw, Mapping) or frozenset(raw) not in {
                frozenset(
                    {
                        "phase",
                        "passed",
                        "cancelled",
                        "timed_out",
                        "items",
                    }
                ),
                frozenset(
                    {
                        "phase",
                        "passed",
                        "cancelled",
                        "timed_out",
                        "items",
                        "evidence_authority",
                    }
                ),
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
                    evidence_authority=(
                        _parse_authority(raw["evidence_authority"])
                        if "evidence_authority" in raw
                        else None
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
    *,
    enforce_current_policy: bool,
) -> tuple[PhaseExecutionOutcome, ...]:
    validated, used_references = _validated_outcomes(
        inputs,
        outcomes,
        enforce_current_policy=enforce_current_policy,
    )
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


def restore_already_green_observation(
    inputs: Any,
    *,
    writer: OutputWriter,
    descriptor: Mapping[str, Any],
    objects: Mapping[str, bytes],
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
    enforce_current_policy: bool = True,
) -> RedAlreadyGreenObservation:
    """Restore and validate the successful Red probe from a portable checkpoint."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(enforce_current_policy, bool):
        raise TypeError("enforce_current_policy must be a boolean")
    if not isinstance(descriptor, Mapping):
        raise PhaseEvidenceError("already-green descriptor must be an object")
    try:
        descriptor_bytes = canonical_json_bytes(descriptor)
    except ValueError as exc:
        raise PhaseEvidenceError(
            "already-green descriptor is not portable JSON"
        ) from exc
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise PhaseEvidenceError("already-green descriptor exceeds its size limit")
    scanner.require_clean_chunks((descriptor_bytes,))
    if set(descriptor) != {"schema", "observation_state", "artifacts"} or (
        descriptor.get("schema") != _ALREADY_GREEN_SCHEMA
    ):
        raise PhaseEvidenceError("already-green descriptor schema is invalid")
    state_binding = descriptor["observation_state"]
    raw_artifacts = descriptor["artifacts"]
    if (
        not isinstance(state_binding, Mapping)
        or set(state_binding) != {"object_digest", "byte_size"}
        or isinstance(raw_artifacts, str | bytes | bytearray)
        or not isinstance(raw_artifacts, Sequence)
    ):
        raise PhaseEvidenceError("already-green descriptor bindings are invalid")
    state_digest = state_binding["object_digest"]
    state_size = state_binding["byte_size"]
    if (
        not isinstance(state_digest, str)
        or _DIGEST_PATTERN.fullmatch(state_digest) is None
        or isinstance(state_size, bool)
        or not isinstance(state_size, int)
        or not 0 <= state_size <= limits.max_artifact_bytes
    ):
        raise PhaseEvidenceError("already-green state binding is invalid")
    try:
        references = tuple(
            ArtifactReference.model_validate(reference) for reference in raw_artifacts
        )
    except (TypeError, ValueError) as exc:
        raise PhaseEvidenceError(
            "already-green artifact descriptor is invalid"
        ) from exc
    if len(references) != 2 or tuple(
        reference.path for reference in references
    ) != tuple(sorted(reference.path for reference in references)):
        raise PhaseEvidenceError("already-green artifact bindings are invalid")
    required = frozenset(
        (state_digest, *(reference.digest for reference in references))
    )
    by_digest = _object_content(objects, required=required)
    document = by_digest.get(state_digest)
    if (
        document is None
        or len(document) != state_size
        or sha256_digest(document) != state_digest
    ):
        raise PhaseEvidenceError("already-green state object is invalid")
    scanner.require_clean_chunks((document,))
    try:
        decoded = json.loads(document.decode("utf-8", errors="strict"))
        if (
            not isinstance(decoded, Mapping)
            or frozenset(decoded)
            not in {
                frozenset(
                    {
                        "schema",
                        "command",
                        "environment_keys",
                        "started_at",
                        "finished_at",
                        "duration_seconds",
                        "exit_code",
                        "termination_reason",
                        "repository_files_changed",
                        "stdout",
                        "stderr",
                    }
                ),
                frozenset(
                    {
                        "schema",
                        "command",
                        "environment_keys",
                        "evidence_authority",
                        "started_at",
                        "finished_at",
                        "duration_seconds",
                        "exit_code",
                        "termination_reason",
                        "repository_files_changed",
                        "stdout",
                        "stderr",
                    }
                ),
            }
            or decoded.get("schema") != _ALREADY_GREEN_DOCUMENT_SCHEMA
            or decoded.get("exit_code") != 0
            or decoded.get("termination_reason") != "exited"
            or decoded.get("repository_files_changed") is not False
            or canonical_json_bytes(decoded) != document
        ):
            raise PhaseEvidenceError("already-green state document is invalid")
        command = decoded["command"]
        environment_keys = decoded["environment_keys"]
        if (
            isinstance(command, str | bytes | bytearray)
            or not isinstance(command, Sequence)
            or isinstance(environment_keys, str | bytes | bytearray)
            or not isinstance(environment_keys, Sequence)
            or any(
                not isinstance(value, str) for value in (*command, *environment_keys)
            )
        ):
            raise PhaseEvidenceError("already-green command binding is invalid")
        observation = RedAlreadyGreenObservation(
            command=tuple(command),
            environment_keys=tuple(environment_keys),
            started_at=decoded["started_at"],
            finished_at=decoded["finished_at"],
            duration_seconds=decoded["duration_seconds"],
            stdout=ArtifactReference.model_validate(decoded["stdout"]),
            stderr=ArtifactReference.model_validate(decoded["stderr"]),
            evidence_authority=(
                _parse_authority(decoded["evidence_authority"])
                if "evidence_authority" in decoded
                else None
            ),
        )
    except PhaseEvidenceError:
        raise
    except (TypeError, UnicodeError, ValueError) as exc:
        raise PhaseEvidenceError("already-green state document is invalid") from exc
    observation, used_references = _validated_already_green(
        inputs,
        observation,
        enforce_current_policy=enforce_current_policy,
    )
    if tuple(
        reference.model_dump(mode="json")
        for reference in sorted(used_references, key=lambda item: item.path)
    ) != tuple(reference.model_dump(mode="json") for reference in references):
        raise PhaseEvidenceError("already-green artifact references are inconsistent")

    contents: dict[str, bytes] = {}
    total_bytes = len(document)
    for reference in references:
        content = by_digest.get(reference.digest)
        if (
            content is None
            or len(content) != reference.byte_size
            or sha256_digest(content) != reference.digest
            or len(content) > limits.max_artifact_bytes
        ):
            raise PhaseEvidenceError("already-green artifact object is invalid")
        scanner.require_clean_chunks((content,))
        total_bytes += len(content)
        if total_bytes > limits.max_total_bytes:
            raise PhaseEvidenceError("already-green objects exceed their total limit")
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
                "restored already-green artifact differs from its checkpoint"
            )
    return observation


def restore_phase_evidence(
    inputs: Any,
    *,
    writer: OutputWriter,
    descriptor: Mapping[str, Any],
    objects: Mapping[str, bytes],
    secret_scanner: SecretScanner | None = None,
    limits: PhaseEvidenceLimits = PhaseEvidenceLimits(),
    enforce_current_policy: bool = True,
) -> tuple[PhaseExecutionOutcome, ...]:
    """Validate and restore exact phase outcomes into a fresh output writer."""

    if not isinstance(limits, PhaseEvidenceLimits):
        raise TypeError("limits must be PhaseEvidenceLimits")
    scanner = secret_scanner or SecretScanner(SecretPolicy())
    if not isinstance(scanner, SecretScanner):
        raise TypeError("secret_scanner must be a SecretScanner or null")
    if not isinstance(enforce_current_policy, bool):
        raise TypeError("enforce_current_policy must be a boolean")
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
        enforce_current_policy=enforce_current_policy,
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
    "ALREADY_GREEN_WORKFLOW_KEY",
    "PHASE_EVIDENCE_WORKFLOW_KEY",
    "PhaseEvidenceError",
    "PhaseEvidenceLimits",
    "PhaseEvidenceSnapshot",
    "restore_already_green_observation",
    "restore_phase_evidence",
    "snapshot_already_green_observation",
    "snapshot_phase_evidence",
]

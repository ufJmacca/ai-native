"""Durable, immutable checkpoint persistence and resume restoration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from types import MappingProxyType
from uuid import uuid4

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.errors import (
    ContractErrorCode,
    ContractValidationError,
)
from ai_native.factory_runner.git_runtime import (
    FactoryGitCancelled,
    FactoryGitError,
    FactoryGitRuntime,
    FactoryGitTimedOut,
)
from ai_native.factory_runner.negotiation import (
    CheckpointCompatibilityResult,
    validate_checkpoint_compatibility,
)
from ai_native.factory_runner.protocol import (
    validate_contract,
    verify_contract_digest,
)


_MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class CheckpointError(RuntimeError):
    """A checkpoint failure with a stable machine-readable reason code."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: ContractErrorCode | str = (
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE
        ),
    ) -> None:
        self.reason_code = ContractErrorCode(reason_code).value
        self.message = message
        super().__init__(message)


class CheckpointCancelled(CheckpointError):
    """Checkpoint restore observed the attempt cancellation token."""


class CheckpointTimedOut(CheckpointError):
    """Checkpoint restore exhausted the admitted attempt deadline."""


@dataclass(frozen=True, slots=True)
class LoadedCheckpoint:
    """A verified checkpoint and detached immutable object payloads."""

    checkpoint: Checkpoint
    checkpoint_path: Path
    checkpoint_reference: ArtifactReference
    objects: Mapping[str, bytes]
    compatibility: CheckpointCompatibilityResult

    @property
    def workspace_patch(self) -> bytes | None:
        digest = self.checkpoint.workspace_patch_digest
        if digest is None:
            return None
        for reference in self.checkpoint.artifact_manifest:
            if reference.digest == digest:
                return self.objects[reference.path]
        raise CheckpointError("checkpoint workspace patch is missing from the manifest")


class CheckpointManager:
    """Read and write checkpoint bundles beneath one trusted filesystem root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _resolved_root(self) -> Path:
        try:
            if self.root.is_symlink():
                raise CheckpointError("checkpoint root must not be a symbolic link")
            resolved = self.root.resolve(strict=True)
        except CheckpointError:
            raise
        except (OSError, RuntimeError) as exc:
            raise CheckpointError("checkpoint root is missing or inaccessible") from exc
        if not resolved.is_dir():
            raise CheckpointError("checkpoint root must be a directory")
        return resolved

    def _relative_path(self, path: str | Path) -> Path:
        root = self._resolved_root()
        candidate = Path(path)
        try:
            relative = (
                candidate.relative_to(root) if candidate.is_absolute() else candidate
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CheckpointError("checkpoint path escapes its trusted root") from exc
        if (
            not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise CheckpointError("checkpoint path must be a normalised relative path")
        return relative

    def _safe_read(
        self,
        path: str | Path,
        *,
        expected_size: int | None = None,
        expected_digest: str | None = None,
    ) -> tuple[Path, bytes]:
        root = self._resolved_root()
        relative = self._relative_path(path)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise CheckpointError("checkpoint path traverses a symbolic link")
        try:
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CheckpointError("checkpoint object is missing") from exc
        if not resolved.is_relative_to(root):
            raise CheckpointError("checkpoint object escapes its trusted root")

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            raise CheckpointError("checkpoint object is missing") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CheckpointError("checkpoint object must be a regular file")
            if expected_size is not None and metadata.st_size != expected_size:
                raise CheckpointError("checkpoint object size or digest mismatch")
            if metadata.st_size > _MAX_CHECKPOINT_BYTES:
                raise CheckpointError("checkpoint object exceeds the size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_CHECKPOINT_BYTES:
                    raise CheckpointError("checkpoint object exceeds the size limit")
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
        if expected_size is not None and len(content) != expected_size:
            raise CheckpointError("checkpoint object size or digest mismatch")
        if expected_digest is not None and sha256_digest(content) != expected_digest:
            raise CheckpointError("checkpoint object digest mismatch")
        return resolved, content

    @staticmethod
    def _validated_checkpoint(checkpoint: Checkpoint) -> Checkpoint:
        try:
            validated = Checkpoint.model_validate(checkpoint.model_dump(mode="json"))
            verify_contract_digest(validated)
        except Exception as exc:
            raise CheckpointError(
                "checkpoint contract or self digest is invalid"
            ) from exc
        return validated

    @staticmethod
    def _validated_objects(
        checkpoint: Checkpoint,
        objects: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        references = {
            reference.path: reference for reference in checkpoint.artifact_manifest
        }
        if set(objects) != set(references):
            raise CheckpointError("checkpoint is missing a manifest object")
        manifest_digests = {reference.digest for reference in references.values()}
        if manifest_digests != set(checkpoint.object_digests):
            raise CheckpointError("checkpoint object digests do not match its manifest")
        if (
            checkpoint.workspace_patch_digest is not None
            and checkpoint.workspace_patch_digest not in manifest_digests
        ):
            raise CheckpointError(
                "checkpoint workspace patch is missing from its manifest"
            )

        detached: dict[str, bytes] = {}
        for path, reference in references.items():
            content = objects[path]
            if not isinstance(content, bytes):
                raise CheckpointError("checkpoint objects must be bytes")
            if (
                len(content) != reference.byte_size
                or sha256_digest(content) != reference.digest
            ):
                raise CheckpointError("checkpoint object digest mismatch")
            detached[path] = bytes(content)
        return detached

    @staticmethod
    def _write_file(path: Path, content: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def write_safe_boundary(
        self,
        *,
        checkpoint: Checkpoint,
        objects: Mapping[str, bytes],
    ) -> ArtifactReference:
        """Atomically publish one complete, immutable checkpoint sequence."""

        validated = self._validated_checkpoint(checkpoint)
        detached = self._validated_objects(validated, objects)
        root = self._resolved_root()
        sequence_root = PurePosixPath("checkpoints", str(validated.sequence))
        checkpoint_relative = sequence_root / "checkpoint.json"
        for reference in validated.artifact_manifest:
            try:
                PurePosixPath(reference.path).relative_to(sequence_root)
            except ValueError as exc:
                raise CheckpointError(
                    "checkpoint manifest paths must remain inside its sequence"
                ) from exc
            if PurePosixPath(reference.path) == checkpoint_relative:
                raise CheckpointError(
                    "checkpoint manifest may not replace checkpoint.json"
                )

        target = root.joinpath(*sequence_root.parts)
        if target.exists() or target.is_symlink():
            raise CheckpointError("checkpoint sequence already exists and is immutable")
        parent = target.parent
        if parent.is_symlink():
            raise CheckpointError("checkpoint output traverses a symbolic link")
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise CheckpointError("checkpoint output directory is unsafe")

        staging = parent / f".{validated.sequence}.{uuid4().hex}.tmp"
        document = canonical_json_bytes(validated)
        try:
            staging.mkdir(mode=0o700)
            for path in sorted(detached):
                relative = PurePosixPath(path).relative_to(sequence_root)
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._write_file(destination, detached[path])
            self._write_file(staging / "checkpoint.json", document)
            directory_descriptor = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            if target.exists() or target.is_symlink():
                raise CheckpointError(
                    "checkpoint sequence already exists and is immutable"
                )
            staging.rename(target)
            parent_descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except CheckpointError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise CheckpointError("checkpoint could not be written atomically") from exc

        reference_path = checkpoint_relative.as_posix()
        return ArtifactReference(
            path=reference_path,
            media_type="application/json",
            byte_size=len(document),
            digest=sha256_digest(document),
        )

    def load_for_resume(
        self,
        path: str | Path,
        *,
        expected_digest: str,
        run_spec: RunSpec,
        supported_capabilities: Sequence[str],
        runner_version: str | None = None,
    ) -> LoadedCheckpoint:
        """Load and verify a checkpoint and every object before returning it."""

        checkpoint_path, document = self._safe_read(path)
        try:
            model = validate_contract(document, expected_schema="checkpoint/v1")
            if not isinstance(model, Checkpoint):
                raise TypeError("checkpoint document has the wrong contract type")
            verify_contract_digest(model)
            if model.checkpoint_digest != expected_digest:
                raise ContractValidationError(
                    ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
                    "checkpoint does not match the expected digest",
                )
            compatibility = validate_checkpoint_compatibility(
                model,
                run_spec,
                supported_capabilities=supported_capabilities,
                runner_version=runner_version,
            )
        except Exception as exc:
            raise CheckpointError("checkpoint contract is incompatible") from exc

        references = {
            reference.path: reference for reference in model.artifact_manifest
        }
        if {reference.digest for reference in references.values()} != set(
            model.object_digests
        ):
            raise CheckpointError("checkpoint object digests do not match its manifest")
        if (
            model.workspace_patch_digest is not None
            and model.workspace_patch_digest
            not in {reference.digest for reference in references.values()}
        ):
            raise CheckpointError(
                "checkpoint workspace patch is missing from its manifest"
            )

        objects: dict[str, bytes] = {}
        for object_path, reference in references.items():
            _, content = self._safe_read(
                object_path,
                expected_size=reference.byte_size,
                expected_digest=reference.digest,
            )
            objects[object_path] = content
        relative_checkpoint = checkpoint_path.relative_to(self._resolved_root())
        checkpoint_reference = ArtifactReference(
            path=relative_checkpoint.as_posix(),
            media_type="application/json",
            byte_size=len(document),
            digest=sha256_digest(document),
        )
        return LoadedCheckpoint(
            checkpoint=model,
            checkpoint_path=checkpoint_path,
            checkpoint_reference=checkpoint_reference,
            objects=MappingProxyType(objects),
            compatibility=compatibility,
        )

    def restore_transactionally(
        self,
        loaded: LoadedCheckpoint,
        *,
        git_runtime: FactoryGitRuntime | None = None,
        workspace: Path | None = None,
        patch_validator: Callable[[bytes], None] | None = None,
        postcondition: Callable[[], None] | None = None,
    ) -> None:
        """Apply the complete workspace patch or leave the worktree unchanged."""

        if not isinstance(loaded, LoadedCheckpoint):
            raise CheckpointError("restore requires a verified checkpoint")
        if patch_validator is not None and not callable(patch_validator):
            raise CheckpointError("checkpoint patch validator must be callable")
        if postcondition is not None and not callable(postcondition):
            raise CheckpointError("checkpoint restore postcondition must be callable")
        patch = loaded.workspace_patch
        if patch is None:
            if postcondition is not None:
                try:
                    postcondition()
                except FactoryGitCancelled as exc:
                    raise CheckpointCancelled(
                        "checkpoint restore postcondition was cancelled"
                    ) from exc
                except FactoryGitTimedOut as exc:
                    raise CheckpointTimedOut(
                        "checkpoint restore postcondition timed out"
                    ) from exc
                except Exception as exc:
                    raise CheckpointError(
                        "checkpoint restore postcondition failed"
                    ) from exc
            return
        if not isinstance(git_runtime, FactoryGitRuntime):
            raise CheckpointError("restore requires a runner-owned Git runtime")
        if workspace is not None:
            try:
                requested_workspace = Path(workspace).resolve(strict=True)
                runtime_workspace = git_runtime.workspace.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise CheckpointError("restore workspace is missing") from exc
            if requested_workspace != runtime_workspace:
                raise CheckpointError(
                    "restore workspace differs from its runner-owned Git runtime"
                )
        try:
            if patch_validator is not None:
                patch_validator(patch)
            git_runtime.apply_patch_transactionally(
                patch,
                postcondition=postcondition,
            )
        except FactoryGitCancelled as exc:
            raise CheckpointCancelled("checkpoint patch restore was cancelled") from exc
        except FactoryGitTimedOut as exc:
            raise CheckpointTimedOut("checkpoint patch restore timed out") from exc
        except FactoryGitError as exc:
            raise CheckpointError("checkpoint patch restore failed") from exc
        except Exception as exc:
            raise CheckpointError(
                "checkpoint patch validation or restore postcondition failed"
            ) from exc


__all__ = [
    "CheckpointCancelled",
    "CheckpointError",
    "CheckpointManager",
    "CheckpointTimedOut",
    "LoadedCheckpoint",
]

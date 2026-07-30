from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Protocol

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.factory_runner.outputs import (
    capture_output_tree,
    enforce_output_tree_unchanged,
)


_COMMAND_ENVIRONMENT_KEY = "AINATIVE_FACTORY_AGENT_COMMAND_JSON"
_MAX_PROTECTED_BYTES = 16 * 1024 * 1024
_MAX_PROTECTED_ENTRIES = 20_000


class FactoryWorkflowError(AdapterError):
    """A safe-to-report failure at the factory workflow boundary."""


class FactoryWorkflowPolicyViolation(FactoryWorkflowError):
    """A gateway child attempted to mutate runner-owned protocol output."""


class _ProcessResult(Protocol):
    returncode: int | None
    termination_reason: str


class _ProcessRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> _ProcessResult: ...


def _validated_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes, bytearray)):
        raise FactoryWorkflowError(
            "factory gateway command must be a non-empty JSON argument vector"
        )
    try:
        argv = tuple(command)
    except TypeError:
        raise FactoryWorkflowError(
            "factory gateway command must be a non-empty JSON argument vector"
        ) from None
    if not argv or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in argv
    ):
        raise FactoryWorkflowError(
            "factory gateway command must be a non-empty JSON argument vector"
        )
    return argv


def load_gateway_command(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Load the explicitly configured gateway command without shell parsing."""

    try:
        encoded_command = environment[_COMMAND_ENVIRONMENT_KEY]
    except (KeyError, TypeError):
        raise FactoryWorkflowError(
            "factory gateway command is not configured"
        ) from None
    if not isinstance(encoded_command, str) or not encoded_command:
        raise FactoryWorkflowError("factory gateway command is not configured")
    try:
        decoded_command = json.loads(encoded_command)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise FactoryWorkflowError("factory gateway command is invalid") from None
    if not isinstance(decoded_command, list):
        raise FactoryWorkflowError(
            "factory gateway command must be a JSON argument vector"
        )
    return _validated_command(decoded_command)


def _validated_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    try:
        copied = dict(environment)
    except (TypeError, ValueError):
        raise FactoryWorkflowError("factory gateway environment is invalid") from None
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in key
        or "\x00" in value
        for key, value in copied.items()
    ):
        raise FactoryWorkflowError("factory gateway environment is invalid")
    return copied


def _validated_timeout(timeout_seconds: float) -> float:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        raise FactoryWorkflowError("factory gateway timeout is invalid") from None
    if not math.isfinite(timeout) or timeout < 0:
        raise FactoryWorkflowError("factory gateway timeout is invalid")
    return timeout


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _protected_root_digest(
    root: Path,
    *,
    mutable_roots: Sequence[Path],
) -> str:
    digest = hashlib.sha256()
    consumed = 0
    entries = 0
    candidates = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    for path in candidates:
        if any(_is_within(path, mutable_root) for mutable_root in mutable_roots):
            continue
        entries += 1
        if entries > _MAX_PROTECTED_ENTRIES:
            raise FactoryWorkflowError("runner-owned state exceeds safety limits")
        try:
            metadata = path.lstat()
            relative = "." if path == root else path.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise FactoryWorkflowError("runner-owned state is unreadable") from exc
        mode = stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode)
        digest.update(f"{relative}\0{mode:o}\0".encode())
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > _MAX_PROTECTED_BYTES - consumed:
                raise FactoryWorkflowError("runner-owned state exceeds safety limits")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise FactoryWorkflowError("runner-owned state is unreadable") from exc
            consumed += len(content)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        elif stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path).encode(
                    "utf-8",
                    errors="surrogateescape",
                )
            except OSError as exc:
                raise FactoryWorkflowError("runner-owned state is unreadable") from exc
            digest.update(target)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise FactoryWorkflowError("runner-owned state has a special file")
    return digest.hexdigest()


def _validate_mutable_root(root: Path) -> None:
    consumed = 0
    entries = 0
    if not root.exists():
        return
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        entries += 1
        if entries > _MAX_PROTECTED_ENTRIES:
            raise FactoryWorkflowError("agent-writable state exceeds safety limits")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise FactoryWorkflowError("agent-writable state is unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FactoryWorkflowError("agent-writable state contains a symbolic link")
        if stat.S_ISREG(metadata.st_mode):
            consumed += metadata.st_size
            if consumed > _MAX_PROTECTED_BYTES:
                raise FactoryWorkflowError("agent-writable state exceeds safety limits")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise FactoryWorkflowError("agent-writable state has a special file")


class FactoryGatewayAdapter:
    """Adapt AI Native's file-based agent contract to a bounded process."""

    def __init__(
        self,
        command: Sequence[str],
        process_runner: _ProcessRunner,
        environment: Mapping[str, str],
        model_profile: str,
        temp_root: Path,
        timeout_seconds: float,
        protected_roots: Sequence[Path] = (),
        mutable_roots: Sequence[Path] = (),
        output_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(model_profile, str)
            or not model_profile
            or "\x00" in model_profile
        ):
            raise FactoryWorkflowError("factory gateway model profile is invalid")
        self.command = _validated_command(command)
        self.process_runner = process_runner
        self.environment = _validated_environment(environment)
        self.model_profile = model_profile
        self.temp_root = Path(temp_root)
        self.timeout_seconds = _validated_timeout(timeout_seconds)
        self.protected_roots = tuple(Path(root) for root in protected_roots)
        self.mutable_roots = tuple(Path(root) for root in mutable_roots)
        self.output_root = Path(output_root) if output_root is not None else None
        if any(
            not any(_is_within(mutable_root, root) for root in self.protected_roots)
            for mutable_root in self.mutable_roots
        ):
            raise FactoryWorkflowError(
                "agent-writable roots must be contained by protected roots"
            )

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        if image_paths:
            raise FactoryWorkflowError("factory gateway does not support image inputs")

        try:
            self.temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="ai-native-factory-gateway-",
                dir=self.temp_root,
            ) as temporary_directory:
                prompt_path = Path(temporary_directory) / "prompt.txt"
                output_path = Path(temporary_directory) / "output.txt"
                prompt_path.write_text(prompt, encoding="utf-8")

                child_environment = dict(self.environment)
                child_environment["AINATIVE_PROMPT_FILE"] = str(prompt_path)
                child_environment["AINATIVE_OUTPUT_FILE"] = str(output_path)
                child_environment["AINATIVE_MODEL_PROFILE"] = self.model_profile
                child_environment.pop("AINATIVE_IMAGE_PATHS", None)
                if schema_path is None:
                    child_environment.pop("AINATIVE_SCHEMA_FILE", None)
                else:
                    child_environment["AINATIVE_SCHEMA_FILE"] = str(schema_path)

                protected_before = tuple(
                    _protected_root_digest(
                        root,
                        mutable_roots=self.mutable_roots,
                    )
                    for root in self.protected_roots
                )
                output_snapshot = (
                    capture_output_tree(self.output_root)
                    if self.output_root is not None
                    else None
                )
                try:
                    result = self.process_runner.run(
                        self.command,
                        cwd=Path(cwd),
                        environment=child_environment,
                        timeout_seconds=self.timeout_seconds,
                    )
                finally:
                    if output_snapshot is not None:
                        try:
                            enforce_output_tree_unchanged(output_snapshot)
                        except (OSError, ValueError) as exc:
                            raise FactoryWorkflowPolicyViolation(
                                "factory gateway modified protocol output"
                            ) from exc
                protected_after = tuple(
                    _protected_root_digest(
                        root,
                        mutable_roots=self.mutable_roots,
                    )
                    for root in self.protected_roots
                )
                for mutable_root in self.mutable_roots:
                    _validate_mutable_root(mutable_root)
                if protected_after != protected_before:
                    raise FactoryWorkflowError(
                        "factory gateway attempted to modify runner-owned state"
                    )
                self._raise_for_process_failure(result)
                text = self._read_output(output_path)
        except FactoryWorkflowError:
            raise
        except Exception:
            raise FactoryWorkflowError("factory gateway execution failed") from None

        payload = None
        if schema_path is not None:
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                raise FactoryWorkflowError(
                    "factory gateway produced invalid JSON"
                ) from None

        return AgentResult(
            text=text,
            json_data=payload,
            stdout="",
            stderr="",
            command=[],
            returncode=0,
        )

    def review(
        self,
        cwd: Path,
        prompt: str,
        base_branch: str | None = None,
    ) -> AgentResult:
        del base_branch
        return self.run(prompt, cwd)

    @staticmethod
    def _raise_for_process_failure(result: _ProcessResult) -> None:
        if result.termination_reason == "timed_out":
            raise FactoryWorkflowError("factory gateway timed out")
        if result.termination_reason == "cancelled":
            raise FactoryWorkflowError("factory gateway was cancelled")
        if result.termination_reason != "exited":
            raise FactoryWorkflowError("factory gateway terminated unexpectedly")
        if result.returncode is None:
            raise FactoryWorkflowError("factory gateway terminated unexpectedly")
        if result.returncode != 0:
            raise FactoryWorkflowError(
                f"factory gateway failed with exit code {result.returncode}"
            )

    @staticmethod
    def _read_output(output_path: Path) -> str:
        if (
            not output_path.exists()
            or output_path.is_symlink()
            or not output_path.is_file()
        ):
            raise FactoryWorkflowError("factory gateway did not produce output")
        try:
            text = output_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise FactoryWorkflowError(
                "factory gateway output could not be read"
            ) from None
        if not text:
            raise FactoryWorkflowError("factory gateway did not produce output")
        return text


__all__ = [
    "FactoryGatewayAdapter",
    "FactoryWorkflowError",
    "FactoryWorkflowPolicyViolation",
    "load_gateway_command",
]

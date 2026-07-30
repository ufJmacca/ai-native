"""Read-only admission for non-interactive factory-runner invocations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Literal, TypeVar, cast

from ai_native.factory_runner.contracts.change_set import ChangeSet
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.context_bundle import ContextBundle
from ai_native.factory_runner.contracts.run_spec import RunSpec
from ai_native.factory_runner.contracts.verification_evidence import (
    VerificationEvidence,
)
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
from ai_native.factory_runner.negotiation import negotiate_protocol
from ai_native.factory_runner.policy import DEFAULT_FACTORY_MODE_CAPABILITIES
from ai_native.factory_runner.process_policy import (
    FactoryPolicyViolation,
    validate_declared_command,
)
from ai_native.factory_runner.protocol import (
    validate_contract,
    verify_contract_digest,
)

RunnerOperation = Literal["author", "verify"]
ContractModel = TypeVar(
    "ContractModel",
    RunSpec,
    ContextBundle,
    ChangeSet,
    VerificationEvidence,
)

_MAX_CONTRACT_BYTES = 16 * 1024 * 1024
_MAX_POLICY_PATH_ENTRIES = 100_000
_GATEWAY_ONLY_ENVIRONMENT_KEYS = frozenset({"ATTEMPT_GATEWAY_TOKEN_FILE"})
_RUNNER_BOOTSTRAP_ENVIRONMENT_KEYS = frozenset(
    {
        "AINATIVE_FACTORY_AGENT_COMMAND_JSON",
        *_GATEWAY_ONLY_ENVIRONMENT_KEYS,
    }
)
_SUPPORTED_CAPABILITIES = ("author", "verify")
_SUPPORTED_NETWORK_PROFILES = frozenset(
    {
        "model-gateway-only",
        "none",
        "offline",
    }
)
_SUPPORTED_MODEL_PROFILES = frozenset(
    {
        "default-model",
        "deterministic-fixture",
    }
)
_PROHIBITED_ENVIRONMENT_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SSH_AUTH_SOCK",
    }
)


class FactoryAdmissionError(RuntimeError):
    """Admission failure carrying a protocol-stable machine-readable code."""

    def __init__(
        self,
        reason_code: ContractErrorCode | str,
        message: str | None = None,
    ) -> None:
        self.reason_code = ContractErrorCode(reason_code).value
        self.message = message or "factory invocation failed admission"
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ValidatedInputs:
    """Fully admitted immutable inputs consumed by the factory coordinator."""

    run_spec: RunSpec
    context_bundle: ContextBundle
    change_set: ChangeSet | None
    run_spec_path: Path
    output_dir: Path
    workspace: Path
    context_digest: str
    environment: Mapping[str, str]


def _fail(
    reason_code: ContractErrorCode | str,
    message: str,
) -> FactoryAdmissionError:
    return FactoryAdmissionError(reason_code, message)


def _resolved_input_file(path: Path, description: str) -> Path:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            raise _fail(
                ContractErrorCode.INVALID_INPUT,
                f"{description} must not be a symbolic link",
            )
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except FactoryAdmissionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            f"{description} is not an accessible regular file",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            f"{description} must be a regular file",
        )
    if metadata.st_size > _MAX_CONTRACT_BYTES:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            f"{description} exceeds the admission size limit",
        )
    return resolved


def _read_contract(
    path: Path,
    *,
    expected_schema: str,
    expected_type: type[ContractModel],
    description: str,
) -> tuple[Path, ContractModel]:
    resolved = _resolved_input_file(path, description)
    try:
        content = resolved.read_bytes()
        validated = validate_contract(content, expected_schema=expected_schema)
    except ContractValidationError as exc:
        raise _fail(exc.code, f"{description} failed contract validation") from exc
    except OSError as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            f"{description} could not be read",
        ) from exc
    if not isinstance(validated, expected_type):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            f"{description} has the wrong contract type",
        )
    return resolved, cast(ContractModel, validated)


def _resolved_workspace(path: str) -> Path:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            raise _fail(
                ContractErrorCode.INVALID_INPUT,
                "workspace must not be a symbolic link",
            )
        resolved = candidate.resolve(strict=True)
    except FactoryAdmissionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace is not an accessible directory",
        ) from exc
    if not resolved.is_dir():
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace must be a directory",
        )
    return resolved


def _resolved_output(path: Path) -> Path:
    candidate = Path(path)
    if candidate.exists() and candidate.is_symlink():
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "output directory must not be a symbolic link",
        )
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "output directory path cannot be resolved",
        ) from exc
    if resolved.exists() and not resolved.is_dir():
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "output path must be a directory",
        )
    parent = resolved if resolved.exists() else resolved.parent
    if not parent.exists() or not parent.is_dir():
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "output directory parent must exist",
        )
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second or first.is_relative_to(second) or second.is_relative_to(first)
    )


def _audit_environment_fallback(environment: Mapping[str, str]) -> None:
    present = _PROHIBITED_ENVIRONMENT_KEYS.intersection(environment)
    if present:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "host environment contains prohibited broad credentials",
        )


def _audit_environment(environment: Mapping[str, str]) -> Mapping[str, str]:
    copied: dict[str, str] = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
        ):
            raise _fail(
                ContractErrorCode.INVALID_INPUT,
                "environment must contain only valid string entries",
            )
        copied[key] = value
    gateway_token_path = copied.get("ATTEMPT_GATEWAY_TOKEN_FILE")
    if gateway_token_path is not None and not Path(gateway_token_path).is_absolute():
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "gateway token file path must be absolute",
        )

    try:
        process_policy = importlib.import_module(
            "ai_native.factory_runner.process_policy"
        )
    except ModuleNotFoundError as exc:
        if exc.name != "ai_native.factory_runner.process_policy":
            raise
        _audit_environment_fallback(copied)
    else:
        audit = getattr(process_policy, "audit_host_environment", None)
        if audit is None:
            _audit_environment_fallback(copied)
        else:
            try:
                audited = audit(copied)
            except FactoryAdmissionError:
                raise
            except Exception as exc:
                code = getattr(exc, "reason_code", ContractErrorCode.POLICY_DENIED)
                try:
                    stable_code = ContractErrorCode(code)
                except ValueError:
                    stable_code = ContractErrorCode.POLICY_DENIED
                raise _fail(
                    stable_code,
                    "host environment failed factory policy",
                ) from exc
            if isinstance(audited, Mapping):
                copied = dict(audited)
    return MappingProxyType(copied)


def _validate_capabilities_and_profiles(run_spec: RunSpec) -> None:
    try:
        negotiate_protocol(
            protocol=run_spec.protocol,
            required_capabilities=run_spec.capabilities.required,
            optional_capabilities=run_spec.capabilities.optional,
            supported_capabilities=_SUPPORTED_CAPABILITIES,
        )
    except ContractValidationError as exc:
        raise _fail(exc.code, "protocol capability negotiation failed") from exc

    policy = run_spec.policy
    if policy.network_profile not in _SUPPORTED_NETWORK_PROFILES:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "network profile is not available in factory mode",
        )
    if policy.model_profile not in _SUPPORTED_MODEL_PROFILES:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "model profile is not available in factory mode",
        )
    if policy.credential_profile != "no-external-credentials":
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "credential profile is not available in factory mode",
        )
    if run_spec.outputs.stream_events_to_stdout:
        raise _fail(
            ContractErrorCode.UNSUPPORTED_CAPABILITY,
            "stdout event streaming is not available until AN-03",
        )
    if run_spec.operation == "author" and not policy.allowed_commands:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "authoring requires at least one declared deterministic verification",
        )
    if _RUNNER_BOOTSTRAP_ENVIRONMENT_KEYS.intersection(policy.allowed_environment_keys):
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "runner bootstrap keys may not be granted to project commands",
        )
    try:
        for command in policy.allowed_commands:
            validate_declared_command(command, policy.allowed_commands)
    except FactoryPolicyViolation as exc:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "declared command exceeds factory authority",
        ) from exc
    prohibited_stages = tuple(
        stage
        for stage in policy.allowed_stages
        if not DEFAULT_FACTORY_MODE_CAPABILITIES.permits_stage(stage)
    )
    if prohibited_stages:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "run spec grants a stage prohibited in factory mode",
        )


def _validate_context_relationships(
    run_spec: RunSpec,
    context_bundle: ContextBundle,
) -> None:
    if context_bundle.identity != run_spec.identity:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "context identity does not match the run spec",
        )
    if context_bundle.repository != run_spec.repository:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "context repository does not match the run spec",
        )
    revision = context_bundle.work_item_revision
    if (
        revision.outcome != run_spec.task.outcome
        or revision.acceptance_criteria != run_spec.task.acceptance_criteria
    ):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "context work-item revision does not match the run spec task",
        )


def _verify_regular_object(
    path: Path,
    *,
    expected_size: int,
    expected_digest: str,
) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "a referenced context object is unavailable",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise _fail(
                ContractErrorCode.DIGEST_MISMATCH,
                "a referenced context object has the wrong size",
            )
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            bytes_read += len(chunk)
        actual_digest = f"sha256:{digest.hexdigest()}"
        if bytes_read != expected_size or actual_digest != expected_digest:
            raise _fail(
                ContractErrorCode.DIGEST_MISMATCH,
                "a referenced context object has the wrong digest",
            )
    finally:
        os.close(descriptor)


def _validate_artifact_reference(
    input_root: Path,
    reference: ArtifactReference,
) -> None:
    try:
        resolved_root = input_root.resolve(strict=True)
        candidate = input_root / reference.path
        current = input_root
        for part in Path(reference.path).parts:
            current = current / part
            if current.is_symlink():
                raise _fail(
                    ContractErrorCode.DIGEST_MISMATCH,
                    "a referenced verification artifact traverses a symbolic link",
                )
        resolved = candidate.resolve(strict=True)
    except FactoryAdmissionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "a referenced verification artifact is unavailable",
        ) from exc
    if not resolved.is_relative_to(resolved_root):
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "a referenced verification artifact escapes the input root",
        )
    _verify_regular_object(
        resolved,
        expected_size=reference.byte_size,
        expected_digest=reference.digest,
    )


def _validate_context_objects(
    context_path: Path,
    context_bundle: ContextBundle,
) -> None:
    objects_dir = context_path.parent / "objects"
    if objects_dir.is_symlink() or not objects_dir.is_dir():
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "context object directory is unavailable",
        )
    for entry in context_bundle.manifest_entries:
        digest_name = entry.digest.removeprefix("sha256:")
        object_path = objects_dir / digest_name
        _verify_regular_object(
            object_path,
            expected_size=entry.byte_size,
            expected_digest=entry.digest,
        )


def _load_context(run_spec: RunSpec) -> tuple[ContextBundle, Path]:
    context_path, context_model = _read_contract(
        Path(run_spec.context.manifest_path),
        expected_schema="context-bundle/v1",
        expected_type=ContextBundle,
        description="context bundle",
    )
    context_bundle = cast(ContextBundle, context_model)
    try:
        verify_contract_digest(context_bundle)
    except ContractValidationError as exc:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "context bundle self digest is invalid",
        ) from exc
    if context_bundle.bundle_digest != run_spec.context.expected_digest:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "context bundle does not match its expected digest",
        )
    _validate_context_relationships(run_spec, context_bundle)
    _validate_context_objects(context_path, context_bundle)
    return context_bundle, context_path


def _load_change_set(
    run_spec: RunSpec,
    context_bundle: ContextBundle,
    *,
    input_root: Path,
) -> ChangeSet | None:
    verification_input = run_spec.verification_input
    if verification_input is None:
        return None
    _, change_set_model = _read_contract(
        Path(verification_input.change_set_path),
        expected_schema="change-set/v1",
        expected_type=ChangeSet,
        description="verification change set",
    )
    change_set = cast(ChangeSet, change_set_model)
    try:
        verify_contract_digest(change_set)
    except ContractValidationError as exc:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "verification change set self digest is invalid",
        ) from exc
    if change_set.change_set_digest != verification_input.expected_digest:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "verification change set does not match its expected digest",
        )
    if (
        change_set.identity != run_spec.identity
        or change_set.repository != run_spec.repository
        or change_set.context_digest != context_bundle.bundle_digest
    ):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "verification change set does not match admitted run identity",
        )
    criteria = tuple(
        result.criterion for result in change_set.acceptance_criteria_results
    )
    if criteria != run_spec.task.acceptance_criteria:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "verification ChangeSet criteria do not match the run task",
        )
    for reference in (
        change_set.patch,
        *change_set.evidence_refs,
        *change_set.generated_artifacts,
    ):
        _validate_artifact_reference(input_root, reference)
    if len(change_set.evidence_refs) != 1:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "AN-02 verification requires exactly one authoring evidence document",
        )
    evidence_reference = change_set.evidence_refs[0]
    _, evidence_model = _read_contract(
        input_root / evidence_reference.path,
        expected_schema="verification-evidence/v1",
        expected_type=VerificationEvidence,
        description="authoring verification evidence",
    )
    evidence = cast(VerificationEvidence, evidence_model)
    try:
        verify_contract_digest(evidence)
    except ContractValidationError as exc:
        raise _fail(
            ContractErrorCode.DIGEST_MISMATCH,
            "authoring evidence self digest is invalid",
        ) from exc
    if (
        evidence.identity != change_set.identity
        or evidence.repository != change_set.repository
        or evidence.context_digest != change_set.context_digest
        or evidence.environment_kind != "authoring"
        or evidence.change_set_digest is not None
        or evidence.overall_status != "passed"
        or evidence.evidence_set_digest != change_set.evidence_set_digest
    ):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "authoring evidence does not bind the admitted ChangeSet",
        )
    for item in evidence.items:
        for reference in (
            item.stdout,
            item.stderr,
            *item.test_reports,
        ):
            _validate_artifact_reference(input_root, reference)
    return change_set


def admit_inputs(
    *,
    expected_operation: RunnerOperation,
    run_spec_path: Path,
    output_dir: Path,
    environment: Mapping[str, str],
) -> ValidatedInputs:
    """Validate all durable invocation inputs without mutating the filesystem."""

    if expected_operation not in {"author", "verify"}:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "CLI selected an unsupported operation",
        )
    resolved_run_spec, run_spec_model = _read_contract(
        Path(run_spec_path),
        expected_schema="run-spec/v1",
        expected_type=RunSpec,
        description="run spec",
    )
    run_spec = cast(RunSpec, run_spec_model)
    if run_spec.operation != expected_operation:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "CLI operation does not match the run spec",
        )

    audited_environment = _audit_environment(environment)
    if run_spec.resume.checkpoint_path is not None:
        raise _fail(
            ContractErrorCode.CHECKPOINT_INCOMPATIBLE,
            "checkpoint resume is not available until durable AN-03 checkpoints",
        )

    workspace = _resolved_workspace(run_spec.workspace.path)
    cli_output = _resolved_output(Path(output_dir))
    declared_output = _resolved_output(Path(run_spec.outputs.output_dir))
    if cli_output != declared_output:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "CLI output directory does not match the run spec",
        )
    if _paths_overlap(workspace, declared_output):
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "output directory overlaps the target workspace",
        )

    _validate_capabilities_and_profiles(run_spec)
    context_bundle, _ = _load_context(run_spec)
    change_set = _load_change_set(
        run_spec,
        context_bundle,
        input_root=resolved_run_spec.parent,
    )

    return ValidatedInputs(
        run_spec=run_spec,
        context_bundle=context_bundle,
        change_set=change_set,
        run_spec_path=resolved_run_spec,
        output_dir=declared_output,
        workspace=workspace,
        context_digest=context_bundle.bundle_digest,
        environment=audited_environment,
    )


def _git(
    workspace: Path,
    *arguments: str,
    git_runtime: FactoryGitRuntime,
) -> str:
    try:
        return git_runtime.run(*arguments).decode("utf-8", errors="strict").rstrip("\n")
    except (FactoryGitCancelled, FactoryGitTimedOut):
        raise
    except FactoryGitError as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace repository could not be inspected",
        ) from exc
    except UnicodeError as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace repository returned invalid text",
        ) from exc


def _git_bytes(
    workspace: Path,
    *arguments: str,
    git_runtime: FactoryGitRuntime,
) -> bytes:
    try:
        return git_runtime.run(*arguments)
    except (FactoryGitCancelled, FactoryGitTimedOut):
        raise
    except FactoryGitError as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace repository could not be inspected",
        ) from exc


def _working_tree_mode(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "prepared verification file is unavailable",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "prepared verification paths must be regular files",
        )
    return "100755" if metadata.st_mode & stat.S_IXUSR else "100644"


def _path_rule_covers(rule: str, path: str) -> bool:
    if rule == "**":
        return True
    if rule.endswith("/**"):
        root = rule.removesuffix("/**")
        return path == root or path.startswith(f"{root}/")
    return rule == path


def _path_is_allowed(run_spec: RunSpec, path: str) -> bool:
    policy = run_spec.policy
    return any(
        _path_rule_covers(rule, path) for rule in policy.allowed_paths
    ) and not any(_path_rule_covers(rule, path) for rule in policy.prohibited_paths)


def _reject_symlink_tree(root: Path, *, description: str) -> None:
    entries = 0
    pending = [root]
    while pending:
        candidate = pending.pop()
        entries += 1
        if entries > _MAX_POLICY_PATH_ENTRIES:
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                f"{description} exceeds the admission entry limit",
            )
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                f"{description} is unreadable",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                f"{description} contains a symbolic link",
            )
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = tuple(candidate.iterdir())
            except OSError as exc:
                raise _fail(
                    ContractErrorCode.POLICY_DENIED,
                    f"{description} is unreadable",
                ) from exc
            pending.extend(child for child in children if child.name != ".git")
        elif not stat.S_ISREG(metadata.st_mode):
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                f"{description} contains a special file",
            )


def _validate_allowed_path_topology(inputs: ValidatedInputs) -> None:
    for rule in inputs.run_spec.policy.allowed_paths:
        if rule == "**":
            root = inputs.workspace
        elif rule.endswith("/**"):
            root = inputs.workspace / rule.removesuffix("/**")
        else:
            root = inputs.workspace / rule

        current = inputs.workspace
        try:
            relative_parts = root.relative_to(inputs.workspace).parts
        except ValueError as exc:
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "allowed path escapes the workspace",
            ) from exc
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise _fail(
                    ContractErrorCode.POLICY_DENIED,
                    "allowed path traverses a symbolic link",
                )
            if not current.exists():
                break
        if rule == "**" or rule.endswith("/**"):
            _reject_symlink_tree(root, description="allowed path tree")


def _validate_git_metadata_topology(git_dir: Path) -> None:
    for relative_path in (
        "config",
        "config.worktree",
        "hooks",
        "index",
        "info",
        "logs/refs",
        "packed-refs",
        "refs",
        "worktrees",
    ):
        _reject_symlink_tree(
            git_dir / relative_path,
            description="Git security metadata",
        )


def _base_tree_mode(
    workspace: Path,
    path: str,
    *,
    git_runtime: FactoryGitRuntime,
) -> str:
    raw = _git_bytes(
        workspace,
        "ls-tree",
        "-z",
        "HEAD",
        "--",
        path,
        git_runtime=git_runtime,
    )
    if not raw:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "prepared verification base file is unavailable",
        )
    try:
        mode = raw.split(b" ", 1)[0].decode("ascii")
    except UnicodeError as exc:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "prepared verification base mode is invalid",
        ) from exc
    if mode not in {"100644", "100755"}:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "prepared verification base mode is unsupported",
        )
    return mode


def _validate_prepared_change_set(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
) -> None:
    change_set = inputs.change_set
    if change_set is None:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "verify workspace is missing its admitted ChangeSet",
        )

    status_output = _git(
        inputs.workspace,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--no-renames",
        git_runtime=git_runtime,
    )
    status_paths: dict[str, str] = {}
    for line in status_output.splitlines():
        if len(line) < 4 or line[2] != " ":
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "verify workspace has an unsupported Git status",
            )
        status_code = line[:2]
        path = line[3:]
        if status_code[0] != " ":
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "verify workspace may not contain staged changes",
            )
        status_paths[path] = status_code

    declared_paths = {changed.path for changed in change_set.changed_files}
    if set(status_paths) != declared_paths:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "verify workspace changes do not match the admitted ChangeSet",
        )

    for changed in change_set.changed_files:
        paths = (changed.path,) + (
            (changed.previous_path,) if changed.previous_path is not None else ()
        )
        if any(not _path_is_allowed(inputs.run_spec, path) for path in paths):
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "verify ChangeSet contains a path outside admitted authority",
            )
        if changed.operation != "modify" or status_paths[changed.path] != " M":
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "AN-02 clean verification supports tracked modifications only",
            )
        previous = _git_bytes(
            inputs.workspace,
            "show",
            f"HEAD:{changed.path}",
            git_runtime=git_runtime,
        )
        resulting_path = inputs.workspace / changed.path
        try:
            resulting = resulting_path.read_bytes()
        except OSError as exc:
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "prepared verification file could not be read",
            ) from exc
        previous_digest = f"sha256:{hashlib.sha256(previous).hexdigest()}"
        resulting_digest = f"sha256:{hashlib.sha256(resulting).hexdigest()}"
        if (
            previous_digest != changed.previous_blob_digest
            or resulting_digest != changed.resulting_blob_digest
            or _base_tree_mode(
                inputs.workspace,
                changed.path,
                git_runtime=git_runtime,
            )
            != changed.previous_mode
            or _working_tree_mode(resulting_path) != changed.resulting_mode
        ):
            raise _fail(
                ContractErrorCode.POLICY_DENIED,
                "verify workspace content does not match the admitted ChangeSet",
            )

    prepared_patch = _git_bytes(
        inputs.workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "HEAD",
        "--",
        git_runtime=git_runtime,
    )
    prepared_patch_digest = f"sha256:{hashlib.sha256(prepared_patch).hexdigest()}"
    if prepared_patch_digest != change_set.patch.digest:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "verify workspace diff does not match the admitted patch",
        )


def validate_workspace(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
) -> ValidatedInputs:
    """Verify exact repository root, base commit, and declared initial state."""

    if not isinstance(inputs, ValidatedInputs):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace validation requires admitted inputs",
        )
    workspace = inputs.workspace
    top_level = _git(
        workspace,
        "rev-parse",
        "--show-toplevel",
        git_runtime=git_runtime,
    )
    try:
        repository_root = Path(top_level).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "Git reported an invalid repository root",
        ) from exc
    if repository_root != workspace:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace path is not the exact repository root",
        )
    git_marker = workspace / ".git"
    if git_marker.is_symlink() or not git_marker.is_dir():
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "workspace must use repository metadata contained in its root",
        )
    try:
        expected_git_dir = git_marker.resolve(strict=True)
        absolute_git_dir = Path(
            _git(
                workspace,
                "rev-parse",
                "--path-format=absolute",
                "--absolute-git-dir",
                git_runtime=git_runtime,
            )
        ).resolve(strict=True)
        common_git_dir = Path(
            _git(
                workspace,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                git_runtime=git_runtime,
            )
        ).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "workspace repository metadata path is invalid",
        ) from exc
    if absolute_git_dir != expected_git_dir or common_git_dir != expected_git_dir:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "linked worktrees and external Git directories are not supported",
        )
    _validate_git_metadata_topology(expected_git_dir)
    try:
        local_config_lines = (
            (expected_git_dir / "config").read_text(encoding="utf-8").splitlines()
        )
    except (OSError, UnicodeError) as exc:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "repository-local Git configuration is unreadable",
        ) from exc
    if any(
        line.strip().casefold().startswith(("[include", "include.path"))
        for line in local_config_lines
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ):
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "external Git configuration includes are not supported",
        )
    alternates = expected_git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "external Git object alternates are not supported",
        )
    if (
        _git(
            workspace,
            "rev-parse",
            "--is-bare-repository",
            git_runtime=git_runtime,
        )
        != "false"
    ):
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace must be a non-bare repository",
        )
    if _git(workspace, "remote", git_runtime=git_runtime):
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "factory workspace must not contain configured Git remotes",
        )
    head = _git(
        workspace,
        "rev-parse",
        "--verify",
        "HEAD",
        git_runtime=git_runtime,
    )
    if head != inputs.run_spec.repository.base_commit_sha:
        raise _fail(
            ContractErrorCode.INVALID_INPUT,
            "workspace HEAD does not match the declared base commit",
        )

    status_output = _git(
        workspace,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--no-renames",
        git_runtime=git_runtime,
    )
    if inputs.run_spec.operation == "author" and status_output:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "author workspace must be clean",
        )
    _validate_allowed_path_topology(inputs)
    if inputs.run_spec.operation == "verify" and not status_output:
        raise _fail(
            ContractErrorCode.POLICY_DENIED,
            "verify workspace has no prepared changes",
        )
    if inputs.run_spec.operation == "verify":
        _validate_prepared_change_set(inputs, git_runtime=git_runtime)
    return inputs


__all__ = [
    "FactoryAdmissionError",
    "ValidatedInputs",
    "admit_inputs",
    "validate_workspace",
]

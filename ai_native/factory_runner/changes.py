"""Minimal genuine ChangeSet creation for successful AN-02 author runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from ai_native import __version__
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.contracts.change_set import (
    ChangeSet,
    ChangedFile,
    changed_file_manifest_digest,
)
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.verification_evidence import (
    VerificationEvidence,
)
from ai_native.factory_runner.git_runtime import FactoryGitRuntime
from ai_native.factory_runner.outputs import EMPTY_DIGEST, OutputWriter, utc_timestamp
from ai_native.factory_runner.protocol import contract_document_digest


class ChangePolicyError(RuntimeError):
    pass


_MAX_SECURITY_METADATA_BYTES = 16 * 1024 * 1024
_MAX_SECURITY_METADATA_ENTRIES = 20_000
_COMMON_SECURITY_PATHS = (
    "config",
    "hooks",
    "info",
    "logs/refs",
    "objects/info/alternates",
    "packed-refs",
    "refs",
    "worktrees",
)
_WORKTREE_SECURITY_PATHS = (
    "BISECT_LOG",
    "CHERRY_PICK_HEAD",
    "FETCH_HEAD",
    "HEAD",
    "MERGE_HEAD",
    "ORIG_HEAD",
    "REVERT_HEAD",
    "config.worktree",
    "index",
    "logs/refs",
    "rebase-apply",
    "rebase-merge",
    "refs",
)


@dataclass(frozen=True, slots=True)
class RepositorySecuritySnapshot:
    digest: str


def _git(
    git_runtime: FactoryGitRuntime,
    *arguments: str,
) -> bytes:
    return git_runtime.run(*arguments)


def _resolved_git_path(
    git_runtime: FactoryGitRuntime,
    argument: str,
) -> Path:
    raw = _git(
        git_runtime,
        "rev-parse",
        "--path-format=absolute",
        argument,
    )
    try:
        return Path(raw.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ChangePolicyError("repository metadata path is invalid") from exc


def _snapshot_entry(
    *,
    digest: Any,
    path: Path,
    label: str,
    budget: list[int],
) -> None:
    budget[1] += 1
    if budget[1] > _MAX_SECURITY_METADATA_ENTRIES:
        raise ChangePolicyError("repository security metadata has too many entries")

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(f"missing\0{label}\0".encode())
        return
    except OSError as exc:
        raise ChangePolicyError("repository security metadata is unreadable") from exc

    mode = stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode)
    digest.update(f"entry\0{label}\0{mode:o}\0".encode())
    if stat.S_ISLNK(metadata.st_mode):
        try:
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        except OSError as exc:
            raise ChangePolicyError(
                "repository security metadata link is unreadable"
            ) from exc
    elif stat.S_ISREG(metadata.st_mode):
        if metadata.st_size > _MAX_SECURITY_METADATA_BYTES - budget[0]:
            raise ChangePolicyError("repository security metadata exceeds size limit")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ChangePolicyError(
                "repository security metadata is unreadable"
            ) from exc
    elif stat.S_ISDIR(metadata.st_mode):
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ChangePolicyError(
                "repository security metadata directory is unreadable"
            ) from exc
        for child in children:
            _snapshot_entry(
                digest=digest,
                path=child,
                label=f"{label}/{child.name}",
                budget=budget,
            )
        return
    else:
        raise ChangePolicyError("repository security metadata has a special file")

    budget[0] += len(content)
    if budget[0] > _MAX_SECURITY_METADATA_BYTES:
        raise ChangePolicyError("repository security metadata exceeds size limit")
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _snapshot_git_marker(
    *,
    digest: Any,
    path: Path,
    budget: list[int],
) -> None:
    """Record the .git indirection itself without traversing object storage."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ChangePolicyError("workspace Git marker is unreadable") from exc
    mode = stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode)
    digest.update(f"git-marker\0{mode:o}\0".encode())
    budget[1] += 1
    if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _snapshot_entry(
            digest=digest,
            path=path,
            label="workspace/.git",
            budget=budget,
        )
    elif not stat.S_ISDIR(metadata.st_mode):
        raise ChangePolicyError("workspace Git marker has a special file type")


def capture_repository_security_snapshot(
    git_runtime: FactoryGitRuntime,
) -> RepositorySecuritySnapshot:
    """Bind security-relevant Git metadata across every author boundary."""

    workspace = git_runtime.workspace
    git_dir = _resolved_git_path(git_runtime, "--absolute-git-dir")
    common_dir = _resolved_git_path(git_runtime, "--git-common-dir")
    digest = hashlib.sha256()
    digest.update(f"git-dir\0{git_dir}\0common-dir\0{common_dir}\0".encode())
    budget = [0, 0]
    _snapshot_git_marker(
        digest=digest,
        path=workspace / ".git",
        budget=budget,
    )
    for relative_path in _COMMON_SECURITY_PATHS:
        _snapshot_entry(
            digest=digest,
            path=common_dir / relative_path,
            label=f"common/{relative_path}",
            budget=budget,
        )
    for relative_path in _WORKTREE_SECURITY_PATHS:
        _snapshot_entry(
            digest=digest,
            path=git_dir / relative_path,
            label=f"worktree/{relative_path}",
            budget=budget,
        )
    return RepositorySecuritySnapshot(digest=f"sha256:{digest.hexdigest()}")


def _path_rule_covers(rule: str, path: str) -> bool:
    if rule == "**":
        return True
    if rule.endswith("/**"):
        root = rule.removesuffix("/**")
        return path == root or path.startswith(f"{root}/")
    return rule == path


def _path_is_allowed(inputs: ValidatedInputs, path: str) -> bool:
    policy = inputs.run_spec.policy
    allowed = any(_path_rule_covers(rule, path) for rule in policy.allowed_paths)
    prohibited = any(_path_rule_covers(rule, path) for rule in policy.prohibited_paths)
    return (
        allowed and not prohibited and path != ".git" and not path.startswith(".git/")
    )


def _status_entries(
    git_runtime: FactoryGitRuntime,
) -> list[tuple[str, str]]:
    raw = _git(
        git_runtime,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    records = [record for record in raw.split(b"\0") if record]
    entries: list[tuple[str, str]] = []
    for record in records:
        decoded = record.decode("utf-8", errors="strict")
        if len(decoded) < 4 or decoded[2] != " ":
            raise ChangePolicyError("unsupported repository status")
        status_code = decoded[:2]
        path = decoded[3:]
        if status_code[0] not in {" ", "?"}:
            raise ChangePolicyError("factory authoring may not stage changes")
        if status_code == "??":
            raise ChangePolicyError(
                "AN-02 minimal ChangeSet supports tracked modifications only"
            )
        if status_code[1] != "M":
            raise ChangePolicyError(
                "AN-02 minimal ChangeSet supports tracked modifications only"
            )
        entries.append((status_code, path))
    return entries


def validate_author_boundary(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
    security_snapshot: RepositorySecuritySnapshot,
) -> None:
    """Fail closed when an author stage escapes repository authority."""

    if capture_repository_security_snapshot(git_runtime) != security_snapshot:
        raise ChangePolicyError("factory authoring changed Git security metadata")
    head = _git(git_runtime, "rev-parse", "HEAD").decode().strip()
    if head != inputs.run_spec.repository.base_commit_sha:
        raise ChangePolicyError("factory authoring changed repository HEAD")
    for _status, path in _status_entries(git_runtime):
        if not _path_is_allowed(inputs, path):
            raise ChangePolicyError("repository change is outside allowed paths")


def _restore_tracked_file(
    *,
    workspace: Path,
    path: str,
    content: bytes,
    mode: str,
) -> None:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ChangePolicyError("changed path is unsafe to restore")

    parent = workspace
    for part in relative.parts[:-1]:
        parent /= part
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise ChangePolicyError("changed path parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ChangePolicyError("changed path parent is unsafe to restore")

    target = parent / relative.name
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ChangePolicyError("changed file is unavailable for restore") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ChangePolicyError("changed file is unsafe to restore")

    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ChangePolicyError("changed file could not be restored") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ChangePolicyError("changed file is unsafe to restore")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ChangePolicyError("changed file could not be restored")
            view = view[written:]
        os.fchmod(descriptor, 0o755 if mode == "100755" else 0o644)
        os.fsync(descriptor)
    except OSError as exc:
        raise ChangePolicyError("changed file could not be restored") from exc
    finally:
        os.close(descriptor)


def restore_clean_author_workspace(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
    security_snapshot: RepositorySecuritySnapshot,
) -> None:
    """Restore admitted tracked files after a non-successful author decision."""

    if capture_repository_security_snapshot(git_runtime) != security_snapshot:
        raise ChangePolicyError("unsafe Git metadata change prevents workspace restore")
    entries = _status_entries(git_runtime)
    for _status, path in entries:
        _restore_tracked_file(
            workspace=inputs.workspace,
            path=path,
            content=_git(git_runtime, "show", f"HEAD:{path}"),
            mode=_previous_mode(git_runtime, path),
        )
    validate_author_boundary(
        inputs,
        git_runtime=git_runtime,
        security_snapshot=security_snapshot,
    )
    if _status_entries(git_runtime):
        raise ChangePolicyError("author workspace restore was incomplete")


def _file_mode(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ChangePolicyError("changed paths must be regular files")
    return "100755" if metadata.st_mode & stat.S_IXUSR else "100644"


def _previous_mode(
    git_runtime: FactoryGitRuntime,
    path: str,
) -> str:
    raw = _git(git_runtime, "ls-tree", "-z", "HEAD", "--", path)
    if not raw:
        raise ChangePolicyError("changed path is not present at the declared base")
    mode = raw.split(b" ", 1)[0].decode("ascii")
    if mode not in {"100644", "100755"}:
        raise ChangePolicyError("unsupported base file mode")
    return mode


def build_change_set(
    inputs: ValidatedInputs,
    *,
    writer: OutputWriter,
    git_runtime: FactoryGitRuntime,
    evidence: VerificationEvidence,
    evidence_reference: ArtifactReference,
) -> tuple[ChangeSet | None, ArtifactReference | None]:
    """Create a deterministic patch for tracked modifications, or no change."""

    evidence_passed = (
        evidence.overall_status == "passed"
        and bool(evidence.items)
        and all(item.actual_status == "passed" for item in evidence.items)
    )
    if not evidence_passed:
        raise ChangePolicyError("ChangeSet requires passing deterministic evidence")
    head = _git(git_runtime, "rev-parse", "HEAD").decode().strip()
    if head != inputs.run_spec.repository.base_commit_sha:
        raise ChangePolicyError("factory authoring changed repository HEAD")

    entries = _status_entries(git_runtime)
    if not entries:
        return None, None

    changed_files: list[ChangedFile] = []
    for _status, path in sorted(entries, key=lambda item: item[1]):
        if not _path_is_allowed(inputs, path):
            raise ChangePolicyError("repository change is outside allowed paths")
        current_path = inputs.workspace / path
        if current_path.is_symlink():
            raise ChangePolicyError("symbolic links are not supported in AN-02")
        previous = _git(git_runtime, "show", f"HEAD:{path}")
        resulting = current_path.read_bytes()
        changed_files.append(
            ChangedFile(
                path=path,
                operation="modify",
                previous_path=None,
                previous_blob_digest=sha256_digest(previous),
                resulting_blob_digest=sha256_digest(resulting),
                previous_mode=_previous_mode(
                    git_runtime,
                    path,
                ),
                resulting_mode=_file_mode(current_path),
                binary=b"\0" in previous or b"\0" in resulting,
                allowed_path_decision="allowed",
            )
        )

    patch = _git(
        git_runtime,
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
    )
    if not patch:
        raise ChangePolicyError("changed-file manifest has no corresponding patch")
    patch_reference = writer.write_bytes(
        "changeset/change.patch",
        patch,
        media_type="application/vnd.git.binary-patch",
    )
    runner_digest = sha256_digest(__version__.encode("utf-8"))
    payload: dict[str, Any] = {
        "protocol": "factory-runner-protocol/v1",
        "schema": "change-set/v1",
        "schema_version": 1,
        "created_at": utc_timestamp(),
        "identity": inputs.run_spec.identity.model_dump(mode="json"),
        "repository": inputs.run_spec.repository.model_dump(mode="json"),
        "change_set_id": (
            f"{inputs.run_spec.identity.run_id}:"
            f"{inputs.run_spec.identity.attempt_id}:changes"
        ),
        "runner_digest": runner_digest,
        "context_digest": inputs.context_digest,
        "patch": patch_reference.model_dump(mode="json"),
        "diff_digest": changed_file_manifest_digest(changed_files),
        "changed_files": [
            changed_file.model_dump(mode="json") for changed_file in changed_files
        ],
        "evidence_set_digest": evidence.evidence_set_digest,
        "evidence_refs": [evidence_reference.model_dump(mode="json")],
        "acceptance_criteria_results": [
            {
                "criterion": criterion,
                "status": "not_run",
            }
            for criterion in inputs.run_spec.task.acceptance_criteria
        ],
        "outcome_summary": "Prepared an uncommitted factory-authoring change set.",
        "assumptions": [],
        "residual_risks": [
            "AN-02 deterministic commands are not mapped to individual criteria.",
            "AN-03 will harden full add/delete/rename and binary patch handling.",
        ],
        "policy_observations": [
            "No commit, push, pull request, or publication operation was performed."
        ],
        "generated_artifacts": [],
        "change_set_digest": EMPTY_DIGEST,
    }
    payload["change_set_digest"] = contract_document_digest(payload)
    change_set = ChangeSet.model_validate(payload)
    reference = writer.write_json(
        "changeset/change-set.json",
        change_set.model_dump(mode="json"),
    )
    return change_set, reference


__all__ = [
    "ChangePolicyError",
    "RepositorySecuritySnapshot",
    "build_change_set",
    "capture_repository_security_snapshot",
    "restore_clean_author_workspace",
    "validate_author_boundary",
]

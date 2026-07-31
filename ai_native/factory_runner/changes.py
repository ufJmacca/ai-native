"""Minimal genuine ChangeSet creation for successful AN-02 author runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, cast

from ai_native.factory_runner.build_identity import (
    canonical_build_identity_bytes,
    load_build_identity,
)
from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.contracts.change_set import (
    ChangeOperation,
    ChangeSet,
    ChangedFile,
    RegularFileMode,
    changed_file_manifest_digest,
)
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.run_spec import RunPolicy
from ai_native.factory_runner.contracts.verification_evidence import (
    VerificationEvidence,
)
from ai_native.factory_runner.git_runtime import FactoryGitRuntime
from ai_native.factory_runner.outputs import EMPTY_DIGEST, OutputWriter, utc_timestamp
from ai_native.factory_runner.protocol import contract_document_digest
from ai_native.factory_runner.redaction import SecretScanner


class ChangePolicyError(RuntimeError):
    pass


_MAX_SECURITY_METADATA_BYTES = 16 * 1024 * 1024
_MAX_SECURITY_METADATA_ENTRIES = 20_000
_MAX_CHANGED_FILE_BYTES = 16 * 1024 * 1024
_MAX_CHANGED_BYTES = 64 * 1024 * 1024
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
    return _path_is_allowed_by_policy(policy, path)


def _path_is_allowed_by_policy(policy: RunPolicy, path: str) -> bool:
    allowed = any(_path_rule_covers(rule, path) for rule in policy.allowed_paths)
    prohibited = any(_path_rule_covers(rule, path) for rule in policy.prohibited_paths)
    return (
        allowed and not prohibited and path != ".git" and not path.startswith(".git/")
    )


def validate_checkpoint_patch_paths(
    policy: RunPolicy,
    *,
    patch: bytes,
    git_runtime: FactoryGitRuntime,
) -> tuple[str, ...]:
    """Reject a checkpoint patch that exceeds the resumed path authority."""

    if not isinstance(policy, RunPolicy):
        raise TypeError("checkpoint path validation requires a RunPolicy")
    if not isinstance(git_runtime, FactoryGitRuntime):
        raise TypeError(
            "checkpoint path validation requires a runner-owned Git runtime"
        )
    paths = git_runtime.inspect_patch_paths(patch)
    if any(not _path_is_allowed_by_policy(policy, path) for path in paths):
        raise ChangePolicyError(
            "checkpoint workspace patch is outside resumed path authority"
        )
    return paths


def _status_entries(
    git_runtime: FactoryGitRuntime,
) -> list[tuple[str, str]]:
    raw = _git(
        git_runtime,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
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
        if status_code not in {" M", " D", "??"}:
            raise ChangePolicyError("unsupported repository status")
        entries.append((status_code, path))
    return sorted(entries, key=lambda item: item[1].encode("utf-8"))


def validate_author_boundary(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
    security_snapshot: RepositorySecuritySnapshot,
    secret_scanner: SecretScanner | None = None,
) -> None:
    """Fail closed when an author stage escapes repository authority."""

    if capture_repository_security_snapshot(git_runtime) != security_snapshot:
        raise ChangePolicyError("factory authoring changed Git security metadata")
    head = _git(git_runtime, "rev-parse", "HEAD").decode().strip()
    if head != inputs.run_spec.repository.base_commit_sha:
        raise ChangePolicyError("factory authoring changed repository HEAD")
    entries = _status_entries(git_runtime)
    for _status, path in entries:
        if not _path_is_allowed(inputs, path):
            raise ChangePolicyError("repository change is outside allowed paths")
    _full_changed_file_manifest(
        inputs,
        git_runtime=git_runtime,
        entries=entries,
        secret_scanner=secret_scanner,
    )


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
        except FileNotFoundError:
            try:
                parent.mkdir(mode=0o700)
                metadata = parent.lstat()
            except OSError as exc:
                raise ChangePolicyError("changed path parent is unavailable") from exc
        except OSError as exc:
            raise ChangePolicyError("changed path parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ChangePolicyError("changed path parent is unsafe to restore")

    target = parent / relative.name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise ChangePolicyError("changed file is unavailable for restore") from exc
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ChangePolicyError("changed file is unsafe to restore")

    flags = os.O_WRONLY
    if metadata is None:
        flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise ChangePolicyError("changed file could not be restored") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (
                metadata is not None
                and (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            )
        ):
            raise ChangePolicyError("changed file is unsafe to restore")
        os.ftruncate(descriptor, 0)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ChangePolicyError("changed file could not be restored")
            view = view[written:]
        os.fchmod(descriptor, 0o755 if mode == "100755" else 0o644)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or after.st_nlink != 1:
            raise ChangePolicyError("changed file changed during restore")
    except OSError as exc:
        raise ChangePolicyError("changed file could not be restored") from exc
    finally:
        os.close(descriptor)


def _remove_untracked_file(workspace: Path, path: str) -> None:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ChangePolicyError("untracked path is unsafe to restore")
    target = workspace / relative
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ChangePolicyError("untracked path is unavailable for restore") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ChangePolicyError("untracked path is unsafe to restore")
    target.unlink()
    parent = target.parent
    while parent != workspace:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


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
    for status_code, path in entries:
        if status_code == "??":
            _remove_untracked_file(inputs.workspace, path)
        else:
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


def _previous_mode(
    git_runtime: FactoryGitRuntime,
    path: str,
) -> RegularFileMode:
    raw = _git(git_runtime, "ls-tree", "-z", "HEAD", "--", path)
    if not raw:
        raise ChangePolicyError("changed path is not present at the declared base")
    mode = raw.split(b" ", 1)[0].decode("ascii")
    if mode not in {"100644", "100755"}:
        raise ChangePolicyError("unsupported base file mode")
    return cast(RegularFileMode, mode)


@dataclass(frozen=True, slots=True)
class _FileState:
    path: str
    content: bytes
    mode: RegularFileMode

    @property
    def digest(self) -> str:
        return sha256_digest(self.content)

    @property
    def binary(self) -> bool:
        return b"\0" in self.content


def _read_resulting_file(workspace: Path, path: str) -> _FileState:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ChangePolicyError("changed path is not normalised")
    current = workspace
    for part in relative.parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ChangePolicyError("changed path parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ChangePolicyError("changed path parent is unsafe")

    target = workspace / relative
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ChangePolicyError("changed file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ChangePolicyError("changed paths must be regular files")
        if metadata.st_nlink != 1:
            raise ChangePolicyError("changed paths may not have hard link aliases")
        if metadata.st_size > _MAX_CHANGED_FILE_BYTES:
            raise ChangePolicyError("changed file exceeds the byte limit")
        permissions = stat.S_IMODE(metadata.st_mode)
        if permissions not in {0o644, 0o755}:
            raise ChangePolicyError("changed path has an unsupported file mode")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > _MAX_CHANGED_FILE_BYTES:
                raise ChangePolicyError("changed file exceeds the byte limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mode,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mode,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ):
            raise ChangePolicyError("changed file changed during validation")
    finally:
        os.close(descriptor)
    return _FileState(
        path=path,
        content=b"".join(chunks),
        mode="100755" if permissions == 0o755 else "100644",
    )


def _read_base_file(
    git_runtime: FactoryGitRuntime,
    path: str,
) -> _FileState:
    content = _git(git_runtime, "show", f"HEAD:{path}")
    if len(content) > _MAX_CHANGED_FILE_BYTES:
        raise ChangePolicyError("base file exceeds the byte limit")
    return _FileState(
        path=path,
        content=content,
        mode=_previous_mode(git_runtime, path),
    )


def _changed_file(
    *,
    path: str,
    operation: ChangeOperation,
    previous: _FileState | None,
    resulting: _FileState | None,
    previous_path: str | None = None,
) -> ChangedFile:
    return ChangedFile(
        path=path,
        operation=operation,
        previous_path=previous_path,
        previous_blob_digest=previous.digest if previous is not None else None,
        resulting_blob_digest=resulting.digest if resulting is not None else None,
        previous_mode=previous.mode if previous is not None else None,
        resulting_mode=resulting.mode if resulting is not None else None,
        binary=(
            (previous.binary if previous is not None else False)
            or (resulting.binary if resulting is not None else False)
        ),
        allowed_path_decision="allowed",
    )


def _full_changed_file_manifest(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
    entries: list[tuple[str, str]],
    secret_scanner: SecretScanner | None = None,
) -> list[ChangedFile]:
    deleted: dict[str, _FileState] = {}
    added: dict[str, _FileState] = {}
    modified: list[ChangedFile] = []
    total_bytes = 0

    for status_code, path in entries:
        if not _path_is_allowed(inputs, path):
            raise ChangePolicyError("repository change is outside allowed paths")
        if status_code == " D":
            state = _read_base_file(git_runtime, path)
            if secret_scanner is not None:
                secret_scanner.require_clean_chunks((state.content,))
            deleted[path] = state
            total_bytes += len(state.content)
        elif status_code == "??":
            state = _read_resulting_file(inputs.workspace, path)
            if secret_scanner is not None:
                secret_scanner.require_clean_chunks((state.content,))
            added[path] = state
            total_bytes += len(state.content)
        else:
            previous = _read_base_file(git_runtime, path)
            resulting = _read_resulting_file(inputs.workspace, path)
            if secret_scanner is not None:
                secret_scanner.require_clean_chunks((previous.content,))
                secret_scanner.require_clean_chunks((resulting.content,))
            total_bytes += len(previous.content) + len(resulting.content)
            modified.append(
                _changed_file(
                    path=path,
                    operation="modify",
                    previous=previous,
                    resulting=resulting,
                )
            )
        if total_bytes > _MAX_CHANGED_BYTES:
            raise ChangePolicyError("changed files exceed the aggregate byte limit")

    rename_pairs: list[tuple[str, str]] = []
    deleted_by_digest: dict[str, list[str]] = {}
    added_by_digest: dict[str, list[str]] = {}
    for path, state in deleted.items():
        deleted_by_digest.setdefault(state.digest, []).append(path)
    for path, state in added.items():
        added_by_digest.setdefault(state.digest, []).append(path)
    for digest in sorted(set(deleted_by_digest).intersection(added_by_digest)):
        sources = sorted(deleted_by_digest[digest], key=lambda value: value.encode())
        targets = sorted(added_by_digest[digest], key=lambda value: value.encode())
        rename_pairs.extend(zip(sources, targets, strict=False))

    changed_files = list(modified)
    paired_sources = {source for source, _target in rename_pairs}
    paired_targets = {target for _source, target in rename_pairs}
    for source, target in rename_pairs:
        if not _path_is_allowed(inputs, source) or not _path_is_allowed(inputs, target):
            raise ChangePolicyError("repository rename is outside allowed paths")
        changed_files.append(
            _changed_file(
                path=target,
                operation="rename",
                previous=deleted[source],
                resulting=added[target],
                previous_path=source,
            )
        )
    for path, previous in deleted.items():
        if path not in paired_sources:
            changed_files.append(
                _changed_file(
                    path=path,
                    operation="delete",
                    previous=previous,
                    resulting=None,
                )
            )
    for path, resulting in added.items():
        if path not in paired_targets:
            changed_files.append(
                _changed_file(
                    path=path,
                    operation="add",
                    previous=None,
                    resulting=resulting,
                )
            )
    return sorted(
        changed_files,
        key=lambda item: (
            item.path.encode("utf-8"),
            (item.previous_path or "").encode("utf-8"),
        ),
    )


def _deterministic_patch(
    git_runtime: FactoryGitRuntime,
    *,
    entries: list[tuple[str, str]],
) -> bytes:
    fragments: list[bytes] = []
    tracked_patch = _git(
        git_runtime,
        "diff",
        "--binary",
        "--full-index",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "HEAD",
        "--",
    )
    if tracked_patch:
        fragments.append(tracked_patch)
    for status_code, path in entries:
        if status_code != "??":
            continue
        patch = git_runtime.run_diff(
            "diff",
            "--no-index",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            "/dev/null",
            path,
        )
        if not patch:
            raise ChangePolicyError("untracked file has no corresponding patch")
        fragments.append(patch)
    return b"".join(fragments)


def capture_workspace_patch(
    inputs: ValidatedInputs,
    *,
    git_runtime: FactoryGitRuntime,
) -> bytes | None:
    """Capture the admitted workspace delta for an immutable safe boundary."""

    head = _git(git_runtime, "rev-parse", "HEAD").decode().strip()
    if head != inputs.run_spec.repository.base_commit_sha:
        raise ChangePolicyError("factory authoring changed repository HEAD")
    entries = _status_entries(git_runtime)
    if not entries:
        return None
    for _status, path in entries:
        if not _path_is_allowed(inputs, path):
            raise ChangePolicyError("repository change is outside allowed paths")
    patch = _deterministic_patch(git_runtime, entries=entries)
    if not patch:
        raise ChangePolicyError("workspace changes have no corresponding patch")
    return patch


def build_change_set(
    inputs: ValidatedInputs,
    *,
    writer: OutputWriter,
    git_runtime: FactoryGitRuntime,
    evidence: VerificationEvidence,
    evidence_reference: ArtifactReference,
    secret_scanner: SecretScanner | None = None,
) -> tuple[ChangeSet | None, ArtifactReference | None]:
    """Create a deterministic patch for tracked modifications, or no change."""

    evidence_passed = (
        evidence.overall_status == "passed"
        and bool(evidence.items)
        and all(
            (
                item.phase == "red"
                and item.actual_status == "failed"
                and item.failure_classification == "expected_behavioral_failure"
            )
            or (item.phase != "red" and item.actual_status == "passed")
            for item in evidence.items
        )
    )
    if not evidence_passed:
        raise ChangePolicyError("ChangeSet requires passing deterministic evidence")
    entries = _status_entries(git_runtime)
    if not entries:
        return None, None

    changed_files = _full_changed_file_manifest(
        inputs,
        git_runtime=git_runtime,
        entries=entries,
        secret_scanner=secret_scanner,
    )
    patch = capture_workspace_patch(inputs, git_runtime=git_runtime)
    if patch is None:
        raise ChangePolicyError("changed-file manifest has no corresponding patch")
    patch_reference = writer.write_bytes(
        "changeset/change.patch",
        patch,
        media_type="application/vnd.git.binary-patch",
    )
    runner_digest = sha256_digest(canonical_build_identity_bytes(load_build_identity()))
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
            "Deterministic commands are not mapped to individual criteria.",
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
    "capture_workspace_patch",
    "capture_repository_security_snapshot",
    "restore_clean_author_workspace",
    "validate_author_boundary",
    "validate_checkpoint_patch_paths",
]

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Literal

from ai_native.factory_runner.contracts.change_set import ChangeSet
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.run_result import RunResult
from ai_native.factory_runner.contracts.verification_evidence import (
    VerificationEvidence,
)
from ai_native.factory_runner.protocol import (
    changed_file_manifest_digest,
    contract_document_digest,
    sha256_digest,
    validate_contract,
    verify_contract_digest,
)


PROTOCOL = "factory-runner-protocol/v1"
CREATED_AT = "2026-07-31T00:00:00Z"
AUTHORED_APP = """def greeting(name: str) -> str:
    return f"Hello, {name}!"
"""
TASK_OUTCOME = "Implement the deterministic greeting change."
DEFAULT_ACCEPTANCE_CRITERION = "greeting('Codex') returns 'Hello, Codex!'"
TASK_NON_GOAL = "Commit or publish the change"
TASK_CONSTRAINT = "Use only declared stages and commands"
REPOSITORY_INSTRUCTION = "Keep the public greeting function in app.py."
TRUSTED_POLICY_SUMMARY = "The attempt may author and verify but may not publish."
APPROVED_REPOSITORY_MEMORY = "The greeting function accepts one name string."
DEPENDENCY_OUTPUT = "No upstream dependency changes are required."
OPERATOR_INPUT = "Preserve the greeting(name: str) signature."
FIRST_PROMPT_REQUIRED_TEXT = (
    f"# {TASK_OUTCOME}",
    "## Acceptance criteria",
    DEFAULT_ACCEPTANCE_CRITERION,
    "## Non-goals",
    TASK_NON_GOAL,
    "## Constraints",
    TASK_CONSTRAINT,
    "## Repository instructions",
    REPOSITORY_INSTRUCTION,
    "## Trusted policy summary",
    TRUSTED_POLICY_SUMMARY,
    "## Approved repository memory",
    APPROVED_REPOSITORY_MEMORY,
    "## Dependency outputs",
    DEPENDENCY_OUTPUT,
    "## Operator input",
    OPERATOR_INPUT,
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SEED_REPOSITORY = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "factory_runner"
    / "target-repository"
)
FAKE_AGENT = Path(__file__).with_name("_fake_agent.py")
AgentMode = Literal[
    "assert-first-prompt-context",
    "author",
    "author-add",
    "author-binary",
    "author-delete",
    "author-mode",
    "author-no-change",
    "author-pause-verify",
    "author-rename",
    "author-secret",
    "blocked",
    "fail-if-called",
    "mutate-git-config",
    "sleep",
]


@dataclass(frozen=True, slots=True)
class FactoryInvocation:
    operation: Literal["author", "verify"]
    workspace: Path
    input_dir: Path
    output_dir: Path
    run_spec_path: Path
    context_bundle_path: Path
    change_set_path: Path | None
    base_sha: str
    marker_path: Path

    def agent_command(
        self,
        mode: AgentMode,
    ) -> list[str]:
        command = [
            sys.executable,
            str(FAKE_AGENT),
            "--mode",
            mode,
            "--marker",
            str(self.marker_path),
        ]
        if mode == "assert-first-prompt-context":
            for value in FIRST_PROMPT_REQUIRED_TEXT:
                command.extend(("--required-first-prompt-text", value))
        return command


def _run_git(
    workspace: Path,
    *arguments: str,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=text,
    )


def git_output(workspace: Path, *arguments: str) -> str:
    completed = _run_git(workspace, *arguments)
    assert isinstance(completed.stdout, str)
    return completed.stdout.strip()


def git_status(workspace: Path) -> str:
    completed = _run_git(
        workspace,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert isinstance(completed.stdout, str)
    return completed.stdout.rstrip("\n")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _identity() -> dict[str, str]:
    return {
        "work_item_id": "work-item-an-02",
        "work_item_revision_id": "revision-an-02",
        "delivery_phase_id": "AN-02",
        "run_id": "run-an-02",
        "attempt_id": "attempt-an-02",
        "correlation_id": "correlation-an-02",
    }


def _repository(base_sha: str) -> dict[str, str]:
    return {
        "repository_id": "fixture-repository",
        "display_name": "fixture/target-repository",
        "base_commit_sha": base_sha,
    }


def _document_envelope(schema: str, base_sha: str) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "schema": schema,
        "schema_version": 1,
        "created_at": CREATED_AT,
        "identity": _identity(),
        "repository": _repository(base_sha),
    }


def _initialise_workspace(root: Path) -> tuple[Path, str]:
    workspace = root / "workspace"
    shutil.copytree(SEED_REPOSITORY, workspace)
    _run_git(workspace, "init", "-b", "main")
    _run_git(workspace, "config", "user.name", "Factory Runner Test")
    _run_git(
        workspace,
        "config",
        "user.email",
        "factory-runner-test@example.invalid",
    )
    _run_git(workspace, "add", ".")
    _run_git(workspace, "commit", "-m", "fixture base")
    return workspace, git_output(workspace, "rev-parse", "HEAD")


def _build_context_bundle(
    input_dir: Path,
    *,
    base_sha: str,
    acceptance_criteria: list[str],
) -> tuple[Path, str]:
    context_dir = input_dir / "context"
    work_item = (
        f"{TASK_OUTCOME}\n\n"
        + "\n".join(f"- {criterion}" for criterion in acceptance_criteria)
        + "\n"
    ).encode()
    work_item_digest = sha256_digest(work_item)
    object_path = context_dir / "objects" / work_item_digest.removeprefix("sha256:")
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(work_item)

    payload = _document_envelope("context-bundle/v1", base_sha)
    payload.update(
        {
            "context_bundle_id": "context-an-02",
            "manifest_entries": [
                {
                    "logical_path": "work-item/revision.md",
                    "media_type": "text/markdown",
                    "byte_size": len(work_item),
                    "digest": work_item_digest,
                    "classification": "work_item_revision",
                }
            ],
            "work_item_revision": {
                "outcome": TASK_OUTCOME,
                "acceptance_criteria": acceptance_criteria,
            },
            "repository_instructions": [REPOSITORY_INSTRUCTION],
            "trusted_policy_summary": [TRUSTED_POLICY_SUMMARY],
            "approved_repository_memory": [APPROVED_REPOSITORY_MEMORY],
            "dependency_outputs": [DEPENDENCY_OUTPUT],
            "operator_input": [OPERATOR_INPUT],
            "construction": {
                "builder": "an-02-integration-fixture/v1",
                "source_digests": [work_item_digest],
            },
            "bundle_digest": "sha256:" + ("0" * 64),
        }
    )
    payload["bundle_digest"] = contract_document_digest(payload)
    validated = validate_contract(payload, expected_schema="context-bundle/v1")
    verify_contract_digest(validated)

    context_bundle_path = context_dir / "context-bundle.json"
    _write_json(context_bundle_path, payload)
    return context_bundle_path, str(payload["bundle_digest"])


def _artifact_reference(
    path: str,
    content: bytes,
    *,
    media_type: str,
) -> dict[str, object]:
    return {
        "path": path,
        "media_type": media_type,
        "byte_size": len(content),
        "digest": sha256_digest(content),
    }


def _build_verification_change_set(
    invocation_root: Path,
    workspace: Path,
    input_dir: Path,
    *,
    base_sha: str,
    context_digest: str,
    acceptance_criteria: list[str],
) -> tuple[Path, str]:
    del invocation_root
    previous = (workspace / "app.py").read_bytes()
    (workspace / "app.py").write_text(AUTHORED_APP, encoding="utf-8")
    resulting = (workspace / "app.py").read_bytes()

    patch_completed = _run_git(
        workspace,
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
        text=False,
    )
    assert isinstance(patch_completed.stdout, bytes)
    patch = patch_completed.stdout
    assert patch

    verification_dir = input_dir / "verification"
    patch_path = verification_dir / "change.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(patch)
    stdout = b"prepared verification fixture passed\n"
    stderr = b""
    stdout_path = verification_dir / "authoring.stdout"
    stderr_path = verification_dir / "authoring.stderr"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    evidence_payload = _document_envelope("verification-evidence/v1", base_sha)
    evidence_payload.update(
        {
            "environment_kind": "authoring",
            "runner": {
                "version": "fixture",
                "image": None,
                "source_commit": None,
            },
            "context_digest": context_digest,
            "change_set_digest": None,
            "items": [
                {
                    "phase": "verification",
                    "command": ["fixture-author-verification"],
                    "working_directory": ".",
                    "environment_keys": [],
                    "started_at": CREATED_AT,
                    "finished_at": CREATED_AT,
                    "duration_seconds": 0,
                    "exit_code": 0,
                    "termination_reason": "exited",
                    "expected_status": "passed",
                    "actual_status": "passed",
                    "failure_classification": "none",
                    "stdout": _artifact_reference(
                        "verification/authoring.stdout",
                        stdout,
                        media_type="text/plain",
                    ),
                    "stderr": _artifact_reference(
                        "verification/authoring.stderr",
                        stderr,
                        media_type="text/plain",
                    ),
                    "test_reports": [],
                    "tool_versions": {"fixture": "1"},
                    "repository_files_changed": False,
                }
            ],
            "overall_status": "passed",
            "advisory_observations": [],
            "evidence_set_digest": "sha256:" + ("0" * 64),
        }
    )
    evidence_payload["evidence_set_digest"] = contract_document_digest(evidence_payload)
    evidence_path = verification_dir / "authoring-evidence.json"
    _write_json(evidence_path, evidence_payload)
    evidence = evidence_path.read_bytes()

    changed_file = {
        "path": "app.py",
        "operation": "modify",
        "previous_path": None,
        "previous_blob_digest": sha256_digest(previous),
        "resulting_blob_digest": sha256_digest(resulting),
        "previous_mode": "100644",
        "resulting_mode": "100644",
        "binary": False,
        "allowed_path_decision": "allowed",
    }
    payload = _document_envelope("change-set/v1", base_sha)
    payload.update(
        {
            "change_set_id": "change-set-an-02",
            "runner_digest": sha256_digest(b"an-02-fixture-runner"),
            "context_digest": context_digest,
            "patch": _artifact_reference(
                "verification/change.patch",
                patch,
                media_type="application/vnd.git.binary-patch",
            ),
            "diff_digest": changed_file_manifest_digest([changed_file]),
            "changed_files": [changed_file],
            "evidence_set_digest": evidence_payload["evidence_set_digest"],
            "evidence_refs": [
                _artifact_reference(
                    "verification/authoring-evidence.json",
                    evidence,
                    media_type="application/json",
                )
            ],
            "acceptance_criteria_results": [
                {"criterion": criterion, "status": "not_run"}
                for criterion in acceptance_criteria
            ],
            "outcome_summary": "Prepared the deterministic greeting change.",
            "assumptions": [],
            "residual_risks": [],
            "policy_observations": [],
            "generated_artifacts": [],
            "change_set_digest": "sha256:" + ("0" * 64),
        }
    )
    payload["change_set_digest"] = contract_document_digest(payload)
    validated = validate_contract(payload, expected_schema="change-set/v1")
    verify_contract_digest(validated)

    change_set_path = verification_dir / "change-set.json"
    _write_json(change_set_path, payload)
    return change_set_path, str(payload["change_set_digest"])


def build_invocation(
    root: Path,
    *,
    operation: Literal["author", "verify"],
    acceptance_criteria: list[str] | None = None,
    verification_passes: bool = True,
) -> FactoryInvocation:
    criteria = (
        [DEFAULT_ACCEPTANCE_CRITERION]
        if acceptance_criteria is None
        else acceptance_criteria
    )
    workspace, base_sha = _initialise_workspace(root)
    input_dir = root / "input"
    output_dir = root / "output"
    context_bundle_path, context_digest = _build_context_bundle(
        input_dir,
        base_sha=base_sha,
        acceptance_criteria=criteria,
    )

    verification_command = [
        sys.executable,
        "-B",
        "-c",
        (
            "from app import greeting; "
            + (
                "assert greeting('Codex') == 'Hello, Codex!'"
                if verification_passes
                else "assert greeting('Codex') == 'Goodbye, Codex!'"
            )
        ),
    ]
    change_set_path: Path | None = None
    verification_input: dict[str, str] | None = None
    if operation == "verify":
        change_set_path, change_set_digest = _build_verification_change_set(
            root,
            workspace,
            input_dir,
            base_sha=base_sha,
            context_digest=context_digest,
            acceptance_criteria=criteria,
        )
        verification_input = {
            "change_set_path": str(change_set_path.resolve()),
            "expected_digest": change_set_digest,
        }

    payload = _document_envelope("run-spec/v1", base_sha)
    payload.update(
        {
            "operation": operation,
            "workspace": {
                "path": str(workspace.resolve()),
                "initial_state": (
                    "clean_base" if operation == "author" else "prepared_verification"
                ),
            },
            "task": {
                "outcome": TASK_OUTCOME,
                "acceptance_criteria": criteria,
                "non_goals": [TASK_NON_GOAL],
                "constraints": [TASK_CONSTRAINT],
            },
            "policy": {
                "allowed_paths": ["app.py"],
                "prohibited_paths": [".git"],
                "allowed_stages": (
                    ["plan", "architecture", "prd", "slice", "loop", "verify"]
                    if operation == "author"
                    else ["verify"]
                ),
                "allowed_commands": [verification_command],
                "allowed_environment_keys": [],
                "network_profile": "model-gateway-only",
                "credential_profile": "no-external-credentials",
                "model_profile": "deterministic-fixture",
                "max_wall_seconds": 30,
                "max_agent_turns": 20,
                "max_model_tokens": 50_000,
            },
            "capabilities": {
                "required": [operation],
                "optional": [],
            },
            "context": {
                "manifest_path": str(context_bundle_path.resolve()),
                "expected_digest": context_digest,
            },
            "verification_input": verification_input,
            "resume": {
                "checkpoint_path": None,
                "expected_digest": None,
            },
            "outputs": {
                "output_dir": str(output_dir.resolve()),
                "stream_events_to_stdout": False,
            },
        }
    )
    validate_contract(payload, expected_schema="run-spec/v1")
    run_spec_path = input_dir / "run-spec.json"
    _write_json(run_spec_path, payload)

    return FactoryInvocation(
        operation=operation,
        workspace=workspace,
        input_dir=input_dir,
        output_dir=output_dir,
        run_spec_path=run_spec_path,
        context_bundle_path=context_bundle_path,
        change_set_path=change_set_path,
        base_sha=base_sha,
        marker_path=root / "agent-calls.log",
    )


def invoke_factory(
    invocation: FactoryInvocation,
    *,
    agent_mode: AgentMode,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=factory_environment(invocation, agent_mode=agent_mode),
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def factory_environment(
    invocation: FactoryInvocation,
    *,
    agent_mode: AgentMode,
) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SSH_AUTH_SOCK",
    ):
        environment.pop(key, None)
    environment["AINATIVE_FACTORY_AGENT_COMMAND_JSON"] = json.dumps(
        invocation.agent_command(agent_mode)
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def factory_command(invocation: FactoryInvocation) -> list[str]:
    command_name = "run" if invocation.operation == "author" else "verify"
    return [
        sys.executable,
        "-m",
        "ai_native.cli",
        "factory",
        command_name,
        "--run-spec",
        str(invocation.run_spec_path.resolve()),
        "--output-dir",
        str(invocation.output_dir.resolve()),
    ]


def load_valid_result(invocation: FactoryInvocation) -> RunResult:
    result_path = _safe_output_file(
        invocation.output_dir,
        "result/run-result.json",
    )
    validated = validate_contract(
        result_path.read_bytes(),
        expected_schema="run-result/v1",
    )
    assert isinstance(validated, RunResult)
    verify_contract_digest(validated)
    return validated


def _safe_output_file(output_dir: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    assert relative_path == relative.as_posix()
    assert relative.parts
    assert not relative.is_absolute()
    assert all(part not in {"", ".", ".."} for part in relative.parts)

    root_metadata = output_dir.lstat()
    assert stat.S_ISDIR(root_metadata.st_mode)
    root = output_dir.resolve(strict=True)
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        metadata = current.lstat()
        if index == len(relative.parts) - 1:
            assert stat.S_ISREG(metadata.st_mode)
        else:
            assert stat.S_ISDIR(metadata.st_mode)
    assert current.resolve(strict=True).is_relative_to(root)
    return current


def _read_valid_artifact_reference(
    invocation: FactoryInvocation,
    reference: ArtifactReference,
) -> bytes:
    artifact_path = _safe_output_file(invocation.output_dir, reference.path)
    metadata = artifact_path.stat()
    assert metadata.st_size == reference.byte_size
    content = artifact_path.read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    return content


def _load_valid_verification_reference(
    invocation: FactoryInvocation,
    reference: ArtifactReference,
    result: RunResult,
) -> VerificationEvidence:
    content = _read_valid_artifact_reference(invocation, reference)
    validated = validate_contract(
        content,
        expected_schema="verification-evidence/v1",
    )
    assert isinstance(validated, VerificationEvidence)
    verify_contract_digest(validated)
    assert validated.identity == result.identity
    assert validated.repository == result.repository
    for item in validated.items:
        _read_valid_artifact_reference(invocation, item.stdout)
        _read_valid_artifact_reference(invocation, item.stderr)
        for test_report in item.test_reports:
            _read_valid_artifact_reference(invocation, test_report)
    return validated


def load_valid_change_set(
    invocation: FactoryInvocation,
    result: RunResult,
) -> ChangeSet:
    assert result.operation == "author"
    assert result.change_set is not None
    content = _read_valid_artifact_reference(invocation, result.change_set)
    validated = validate_contract(content, expected_schema="change-set/v1")
    assert isinstance(validated, ChangeSet)
    verify_contract_digest(validated)
    assert validated.identity == result.identity
    assert validated.repository == result.repository

    _read_valid_artifact_reference(invocation, validated.patch)
    for generated_artifact in validated.generated_artifacts:
        _read_valid_artifact_reference(invocation, generated_artifact)

    assert len(validated.evidence_refs) == 1
    evidence = _load_valid_verification_reference(
        invocation,
        validated.evidence_refs[0],
        result,
    )
    assert evidence.environment_kind == "authoring"
    assert evidence.change_set_digest is None
    assert evidence.overall_status == "passed"
    assert evidence.context_digest == validated.context_digest
    assert evidence.evidence_set_digest == validated.evidence_set_digest
    return validated


def load_valid_verification_evidence(
    invocation: FactoryInvocation,
    result: RunResult,
) -> VerificationEvidence:
    assert result.operation == "verify"
    assert result.verification_evidence is not None
    evidence = _load_valid_verification_reference(
        invocation,
        result.verification_evidence,
        result,
    )
    assert evidence.environment_kind == "clean_verification"
    return evidence


def assert_valid_completion(
    invocation: FactoryInvocation,
    result: RunResult,
) -> None:
    completion_path = _safe_output_file(invocation.output_dir, "completion.json")
    completion = json.loads(completion_path.read_bytes())
    assert isinstance(completion, dict)
    assert completion["protocol"] == PROTOCOL
    assert completion["schema_version"] == 1
    assert completion["completed_at"] == result.finished_at
    assert completion["outcome"] == result.outcome
    assert completion["output_manifest_digest"] == result.output_manifest_digest

    result_reference = ArtifactReference.model_validate(completion["run_result"])
    assert result_reference.path == "result/run-result.json"
    assert result_reference.media_type == "application/json"
    referenced_result = validate_contract(
        _read_valid_artifact_reference(invocation, result_reference),
        expected_schema="run-result/v1",
    )
    assert isinstance(referenced_result, RunResult)
    verify_contract_digest(referenced_result)
    assert referenced_result == result


def prepare_clean_verification_from_author(
    root: Path,
    *,
    author_invocation: FactoryInvocation,
    author_result: RunResult,
) -> FactoryInvocation:
    """Stage one emitted author ChangeSet in a fresh verification checkout."""

    change_set = load_valid_change_set(author_invocation, author_result)
    workspace = root / "workspace"
    root.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            str(author_invocation.workspace),
            str(workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(workspace, "remote", "remove", "origin")
    patch = (
        author_invocation.output_dir / change_set.patch.path
    ).read_bytes()
    subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        cwd=workspace,
        input=patch,
        check=True,
        capture_output=True,
    )

    input_dir = root / "input"
    input_dir.mkdir()
    shutil.copytree(
        author_invocation.context_bundle_path.parent,
        input_dir / "context",
    )
    shutil.copytree(author_invocation.output_dir, input_dir, dirs_exist_ok=True)
    output_dir = root / "output"

    payload = json.loads(
        author_invocation.run_spec_path.read_text(encoding="utf-8")
    )
    payload["identity"]["attempt_id"] = "attempt-an-03-clean-verify"
    payload["operation"] = "verify"
    payload["workspace"] = {
        "path": str(workspace.resolve()),
        "initial_state": "prepared_verification",
    }
    payload["policy"]["allowed_stages"] = ["verify"]
    payload["capabilities"]["required"] = ["verify"]
    payload["context"]["manifest_path"] = str(
        (input_dir / "context" / "context-bundle.json").resolve()
    )
    assert author_result.change_set is not None
    change_set_path = input_dir / author_result.change_set.path
    payload["verification_input"] = {
        "change_set_path": str(change_set_path.resolve()),
        "expected_digest": change_set.change_set_digest,
    }
    payload["outputs"] = {
        "output_dir": str(output_dir.resolve()),
        "stream_events_to_stdout": False,
    }
    validate_contract(payload, expected_schema="run-spec/v1")
    run_spec_path = input_dir / "run-spec.json"
    _write_json(run_spec_path, payload)

    return FactoryInvocation(
        operation="verify",
        workspace=workspace,
        input_dir=input_dir,
        output_dir=output_dir,
        run_spec_path=run_spec_path,
        context_bundle_path=input_dir / "context" / "context-bundle.json",
        change_set_path=change_set_path,
        base_sha=author_invocation.base_sha,
        marker_path=root / "agent-calls.log",
    )

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal

from ai_native.factory_runner.contracts.run_result import RunResult
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
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SEED_REPOSITORY = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "factory_runner"
    / "target-repository"
)
FAKE_AGENT = Path(__file__).with_name("_fake_agent.py")


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
        mode: Literal["author", "blocked", "fail-if-called"],
    ) -> list[str]:
        return [
            sys.executable,
            str(FAKE_AGENT),
            "--mode",
            mode,
            "--marker",
            str(self.marker_path),
        ]


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
        "Implement the deterministic greeting change.\n\n"
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
                "outcome": "Implement the deterministic greeting change.",
                "acceptance_criteria": acceptance_criteria,
            },
            "repository_instructions": [],
            "trusted_policy_summary": [
                "The attempt may author and verify but may not publish."
            ],
            "approved_repository_memory": [],
            "dependency_outputs": [],
            "operator_input": [],
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
    evidence = b"prepared verification fixture\n"
    evidence_path = verification_dir / "authoring-evidence.json"
    evidence_path.write_bytes(evidence)

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
            "evidence_set_digest": sha256_digest(evidence),
            "evidence_refs": [
                _artifact_reference(
                    "verification/authoring-evidence.json",
                    evidence,
                    media_type="application/json",
                )
            ],
            "acceptance_criteria_results": [
                {
                    "criterion": "greeting('Codex') returns 'Hello, Codex!'",
                    "status": "passed",
                }
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
        ["greeting('Codex') returns 'Hello, Codex!'"]
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
                "outcome": "Implement the deterministic greeting change.",
                "acceptance_criteria": criteria,
                "non_goals": ["Commit or publish the change"],
                "constraints": ["Use only declared stages and commands"],
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
    agent_mode: Literal["author", "blocked", "fail-if-called"],
) -> subprocess.CompletedProcess[str]:
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
    command_name = "run" if invocation.operation == "author" else "verify"

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_native.cli",
            "factory",
            command_name,
            "--run-spec",
            str(invocation.run_spec_path.resolve()),
            "--output-dir",
            str(invocation.output_dir.resolve()),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def load_valid_result(invocation: FactoryInvocation) -> RunResult:
    result_path = invocation.output_dir / "result" / "run-result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    validated = validate_contract(payload, expected_schema="run-result/v1")
    assert isinstance(validated, RunResult)
    verify_contract_digest(validated)
    return validated

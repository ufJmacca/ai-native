from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Callable

from ai_native.factory_runner.protocol import (
    changed_file_manifest_digest,
    contract_document_digest,
    sha256_digest,
    validate_contract,
)


PROTOCOL = "factory-runner-protocol/v1"
CREATED_AT = "2026-07-31T00:00:00Z"
PLACEHOLDER_DIGEST = "sha256:" + ("0" * 64)
SUPPORTED_ENVIRONMENT = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


def _run_git(repository: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )
    return completed.stdout.strip()


def _run_git_bytes(repository: Path, *arguments: str) -> bytes:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact_reference(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": "application/octet-stream",
        "byte_size": len(content),
        "digest": sha256_digest(content),
    }


@dataclass
class AdmissionCase:
    root: Path
    workspace: Path
    output_dir: Path
    run_spec_path: Path
    context_bundle_path: Path
    context_objects_dir: Path
    base_commit_sha: str
    run_spec: dict[str, Any]
    context_bundle: dict[str, Any]

    @property
    def operation(self) -> str:
        return str(self.run_spec["operation"])

    @property
    def context_entries(self) -> list[dict[str, Any]]:
        return self.context_bundle["manifest_entries"]

    def context_object_path(self, entry_index: int) -> Path:
        digest = str(self.context_entries[entry_index]["digest"])
        return self.context_objects_dir / digest.removeprefix("sha256:")

    def write_run_spec(self) -> None:
        validate_contract(self.run_spec, expected_schema="run-spec/v1")
        _write_json(self.run_spec_path, self.run_spec)

    def write_context_bundle(self) -> None:
        validate_contract(
            self.context_bundle,
            expected_schema="context-bundle/v1",
        )
        _write_json(self.context_bundle_path, self.context_bundle)

    def rebind_context_bundle(self) -> None:
        self.context_bundle["bundle_digest"] = PLACEHOLDER_DIGEST
        self.context_bundle["bundle_digest"] = contract_document_digest(
            self.context_bundle
        )
        self.run_spec["context"]["expected_digest"] = self.context_bundle[
            "bundle_digest"
        ]
        self.write_context_bundle()
        self.write_run_spec()

    def set_workspace_path(self, path: Path) -> None:
        self.run_spec["workspace"]["path"] = str(path.resolve())
        self.write_run_spec()

    def set_output_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.output_dir = path.resolve()
        self.run_spec["outputs"]["output_dir"] = str(self.output_dir)
        self.write_run_spec()

    def set_declared_base(self, base_commit_sha: str) -> None:
        self.run_spec["repository"]["base_commit_sha"] = base_commit_sha
        self.context_bundle["repository"]["base_commit_sha"] = base_commit_sha
        self.rebind_context_bundle()

    def prepare_verification(self, *, apply_change: bool = True) -> None:
        source_path = self.workspace / "src" / "app.py"
        previous_content = source_path.read_bytes()
        resulting_content = b"prepared verification change\n"
        source_path.write_bytes(resulting_content)

        verification_dir = self.root / "input" / "verification"
        objects_dir = verification_dir / "objects"
        objects_dir.mkdir(parents=True, exist_ok=True)

        patch_content = _run_git_bytes(
            self.workspace,
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
        if not apply_change:
            source_path.write_bytes(previous_content)
        patch_digest = sha256_digest(patch_content)
        patch_path = objects_dir / patch_digest.removeprefix("sha256:")
        patch_path.write_bytes(patch_content)

        stdout_content = b"fixture verification passed\n"
        stderr_content = b""
        stdout_path = verification_dir / "authoring.stdout"
        stderr_path = verification_dir / "authoring.stderr"
        stdout_path.write_bytes(stdout_content)
        stderr_path.write_bytes(stderr_content)
        evidence = {
            "protocol": PROTOCOL,
            "schema": "verification-evidence/v1",
            "schema_version": 1,
            "created_at": CREATED_AT,
            "identity": deepcopy(self.run_spec["identity"]),
            "repository": deepcopy(self.run_spec["repository"]),
            "environment_kind": "authoring",
            "runner": {
                "version": "fixture",
                "image": None,
                "source_commit": None,
            },
            "context_digest": self.context_bundle["bundle_digest"],
            "change_set_digest": None,
            "items": [
                {
                    "phase": "verification",
                    "command": ["fixture-verify"],
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
                        stdout_content,
                    ),
                    "stderr": _artifact_reference(
                        "verification/authoring.stderr",
                        stderr_content,
                    ),
                    "test_reports": [],
                    "tool_versions": {"fixture": "1"},
                    "repository_files_changed": False,
                }
            ],
            "overall_status": "passed",
            "advisory_observations": [],
            "evidence_set_digest": PLACEHOLDER_DIGEST,
        }
        evidence["evidence_set_digest"] = contract_document_digest(evidence)
        evidence_path = verification_dir / "verification-evidence.json"
        _write_json(evidence_path, evidence)
        evidence_content = evidence_path.read_bytes()

        changed_files = [
            {
                "path": "src/app.py",
                "operation": "modify",
                "previous_path": None,
                "previous_blob_digest": sha256_digest(previous_content),
                "resulting_blob_digest": sha256_digest(resulting_content),
                "previous_mode": "100644",
                "resulting_mode": "100644",
                "binary": False,
                "allowed_path_decision": "allowed",
            }
        ]
        change_set = {
            "protocol": PROTOCOL,
            "schema": "change-set/v1",
            "schema_version": 1,
            "created_at": CREATED_AT,
            "identity": deepcopy(self.run_spec["identity"]),
            "repository": deepcopy(self.run_spec["repository"]),
            "change_set_id": "change-set-admission-fixture",
            "runner_digest": sha256_digest(b"fixture-runner"),
            "context_digest": self.context_bundle["bundle_digest"],
            "patch": {
                **_artifact_reference(
                    f"verification/objects/{patch_digest.removeprefix('sha256:')}",
                    patch_content,
                ),
                "media_type": "application/vnd.git.binary-patch",
            },
            "diff_digest": changed_file_manifest_digest(changed_files),
            "changed_files": changed_files,
            "evidence_set_digest": evidence["evidence_set_digest"],
            "evidence_refs": [
                {
                    **_artifact_reference(
                        "verification/verification-evidence.json",
                        evidence_content,
                    ),
                    "media_type": "application/json",
                }
            ],
            "acceptance_criteria_results": [
                {
                    "criterion": "The admission fixture remains deterministic.",
                    "status": "not_run",
                }
            ],
            "outcome_summary": "Prepared the deterministic verification change.",
            "assumptions": [],
            "residual_risks": [],
            "policy_observations": [],
            "generated_artifacts": [],
            "change_set_digest": PLACEHOLDER_DIGEST,
        }
        change_set["change_set_digest"] = contract_document_digest(change_set)
        validate_contract(change_set, expected_schema="change-set/v1")

        change_set_path = verification_dir / "change-set.json"
        _write_json(change_set_path, change_set)

        self.run_spec["operation"] = "verify"
        self.run_spec["workspace"]["initial_state"] = "prepared_verification"
        self.run_spec["policy"]["allowed_stages"] = ["verify"]
        self.run_spec["capabilities"] = {
            "required": ["verify"],
            "optional": ["structured-events"],
        }
        self.run_spec["verification_input"] = {
            "change_set_path": str(change_set_path.resolve()),
            "expected_digest": change_set["change_set_digest"],
        }
        self.write_run_spec()


def make_admission_case(root: Path) -> AdmissionCase:
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    output_dir = root / "output"
    input_dir = root / "input"
    context_dir = input_dir / "context"
    context_objects_dir = context_dir / "objects"
    workspace.mkdir()
    output_dir.mkdir()
    context_objects_dir.mkdir(parents=True)

    _run_git(workspace, "init", "--initial-branch=main")
    _run_git(workspace, "config", "user.email", "factory@example.invalid")
    _run_git(workspace, "config", "user.name", "Factory Admission Fixture")
    _run_git(workspace, "config", "core.hooksPath", os.devnull)

    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    (workspace / "src" / "app.py").write_text(
        "initial content\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_app.py").write_text(
        "def test_initial() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _run_git(workspace, "add", "src/app.py", "tests/test_app.py")
    _run_git(workspace, "commit", "-m", "fixture base")
    base_commit_sha = _run_git(workspace, "rev-parse", "HEAD")

    identity = {
        "work_item_id": "work-item-admission",
        "work_item_revision_id": "revision-admission",
        "delivery_phase_id": "phase-admission",
        "run_id": "run-admission",
        "attempt_id": "attempt-admission",
        "correlation_id": "correlation-admission",
    }
    repository = {
        "repository_id": "repository-admission",
        "display_name": "factory/admission-fixture",
        "base_commit_sha": base_commit_sha,
    }

    object_payloads = (
        (
            "work-item/revision.md",
            "work_item_revision",
            b"Implement deterministic admission checks.\n",
        ),
        (
            "repository/instructions.md",
            "repository_instruction",
            b"Do not publish changes.\n",
        ),
        (
            "supporting/notes.txt",
            "supporting_artifact",
            b"Admission fixture supporting material.\n",
        ),
    )
    manifest_entries: list[dict[str, Any]] = []
    source_digests: list[str] = []
    for logical_path, classification, content in object_payloads:
        digest = sha256_digest(content)
        (context_objects_dir / digest.removeprefix("sha256:")).write_bytes(content)
        source_digests.append(digest)
        manifest_entries.append(
            {
                "logical_path": logical_path,
                "media_type": (
                    "text/markdown" if logical_path.endswith(".md") else "text/plain"
                ),
                "byte_size": len(content),
                "digest": digest,
                "classification": classification,
            }
        )

    context_bundle = {
        "protocol": PROTOCOL,
        "schema": "context-bundle/v1",
        "schema_version": 1,
        "created_at": CREATED_AT,
        "identity": deepcopy(identity),
        "repository": deepcopy(repository),
        "context_bundle_id": "context-bundle-admission",
        "manifest_entries": manifest_entries,
        "work_item_revision": {
            "outcome": "Implement deterministic admission checks.",
            "acceptance_criteria": ["The admission fixture remains deterministic."],
        },
        "repository_instructions": ["Do not publish changes."],
        "trusted_policy_summary": ["Factory authority is deny-by-default."],
        "approved_repository_memory": [],
        "dependency_outputs": [],
        "operator_input": [],
        "construction": {
            "builder": "admission-fixture/v1",
            "source_digests": source_digests,
        },
        "bundle_digest": PLACEHOLDER_DIGEST,
    }
    context_bundle["bundle_digest"] = contract_document_digest(context_bundle)
    context_bundle_path = context_dir / "context-bundle.json"
    _write_json(context_bundle_path, context_bundle)

    run_spec = {
        "protocol": PROTOCOL,
        "schema": "run-spec/v1",
        "schema_version": 1,
        "created_at": CREATED_AT,
        "identity": identity,
        "repository": repository,
        "operation": "author",
        "workspace": {
            "path": str(workspace.resolve()),
            "initial_state": "clean_base",
        },
        "task": {
            "outcome": "Implement deterministic admission checks.",
            "acceptance_criteria": ["The admission fixture remains deterministic."],
            "non_goals": ["Publication"],
            "constraints": ["Do not publish changes."],
        },
        "policy": {
            "allowed_paths": ["src/**", "tests/**"],
            "prohibited_paths": [".git"],
            "allowed_stages": ["plan", "loop", "verify"],
            "allowed_commands": [["pytest", "-q"]],
            "allowed_environment_keys": ["PATH"],
            "network_profile": "model-gateway-only",
            "credential_profile": "no-external-credentials",
            "model_profile": "default-model",
            "max_wall_seconds": 300,
            "max_agent_turns": 10,
            "max_model_tokens": 20_000,
        },
        "capabilities": {
            "required": ["author"],
            "optional": ["structured-events"],
        },
        "context": {
            "manifest_path": str(context_bundle_path.resolve()),
            "expected_digest": context_bundle["bundle_digest"],
        },
        "verification_input": None,
        "resume": {
            "checkpoint_path": None,
            "expected_digest": None,
        },
        "outputs": {
            "output_dir": str(output_dir.resolve()),
            "stream_events_to_stdout": False,
        },
    }
    run_spec_path = input_dir / "run-spec.json"
    _write_json(run_spec_path, run_spec)

    case = AdmissionCase(
        root=root,
        workspace=workspace.resolve(),
        output_dir=output_dir.resolve(),
        run_spec_path=run_spec_path.resolve(),
        context_bundle_path=context_bundle_path.resolve(),
        context_objects_dir=context_objects_dir.resolve(),
        base_commit_sha=base_commit_sha,
        run_spec=run_spec,
        context_bundle=context_bundle,
    )
    case.write_context_bundle()
    case.write_run_spec()
    return case


Snapshot = tuple[tuple[str, str, int, int, bytes | str | None], ...]


def filesystem_snapshot(root: Path) -> Snapshot:
    entries: list[tuple[str, str, int, int, bytes | str | None]] = []
    paths = [root, *sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix())]
    for path in paths:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            kind = "symlink"
            content: bytes | str | None = os.readlink(path)
        elif path.is_file():
            kind = "file"
            content = path.read_bytes()
        elif path.is_dir():
            kind = "directory"
            content = None
        else:
            kind = "special"
            content = None
        entries.append((relative, kind, mode, metadata.st_mtime_ns, content))
    return tuple(entries)


def assert_read_only(root: Path, action: Callable[[], object]) -> object:
    before = filesystem_snapshot(root)
    try:
        return action()
    finally:
        after = filesystem_snapshot(root)
        assert after == before, "admission validation mutated its input filesystem"

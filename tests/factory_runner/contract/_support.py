from __future__ import annotations

from copy import deepcopy
import importlib
from types import ModuleType
from typing import Any, Callable

import pytest


PROTOCOL = "factory-runner-protocol/v1"
CREATED_AT = "2026-07-30T10:00:00Z"
FINISHED_AT = "2026-07-30T10:00:01Z"
BASE_COMMIT_SHA = "a" * 40
DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)
DIGEST_C = "sha256:" + ("c" * 64)
DIGEST_D = "sha256:" + ("d" * 64)

MODEL_NAMES = (
    "RunSpec",
    "ContextBundle",
    "Checkpoint",
    "VerificationEvidence",
    "ChangeSet",
    "RunResult",
    "RunnerEvent",
)

REQUIRED_API = (
    *MODEL_NAMES,
    "canonical_json_bytes",
    "sha256_digest",
    "negotiate_protocol",
    "validate_checkpoint_compatibility",
)


def _load_protocol_api() -> tuple[ModuleType | None, str | None]:
    try:
        module = importlib.import_module("ai_native.factory_runner.protocol")
    except ModuleNotFoundError:
        return (
            None,
            "public module ai_native.factory_runner.protocol is missing",
        )
    missing = [name for name in REQUIRED_API if not hasattr(module, name)]
    if missing:
        return (
            None,
            "public protocol surface is incomplete: " + ", ".join(sorted(missing)),
        )
    return module, None


def require_protocol_api() -> ModuleType:
    """Produce one focused RED failure for the intentionally absent surface."""

    module, error = _load_protocol_api()
    if module is None:
        pytest.fail(f"AN-01 RED: {error}", pytrace=False)
    return module


def protocol_api() -> ModuleType:
    """Let detailed cases collect now and activate as the surface appears."""

    module, error = _load_protocol_api()
    if module is None:
        pytest.skip(f"blocked by intended AN-01 RED: {error}")
    return module


def validate(model_name: str, payload: dict[str, Any]) -> Any:
    model = getattr(protocol_api(), model_name)
    return model.model_validate(deepcopy(payload))


def dumped(model_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return validate(model_name, payload).model_dump(mode="json")


def assert_invalid(model_name: str, payload: dict[str, Any]) -> None:
    model = getattr(protocol_api(), model_name)
    with pytest.raises(ValueError):
        model.model_validate(deepcopy(payload))


def identity(*, attempt_id: str = "attempt-01") -> dict[str, str]:
    return {
        "work_item_id": "work-item-01",
        "work_item_revision_id": "revision-01",
        "delivery_phase_id": "phase-01",
        "run_id": "run-01",
        "attempt_id": attempt_id,
        "correlation_id": "correlation-01",
    }


def repository() -> dict[str, str]:
    return {
        "repository_id": "repository-01",
        "display_name": "owner/repository",
        "base_commit_sha": BASE_COMMIT_SHA,
    }


def document_envelope(schema: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "schema": schema,
        "schema_version": 1,
        "created_at": CREATED_AT,
        "identity": identity(),
        "repository": repository(),
    }


def artifact_ref(
    path: str = "objects/" + ("a" * 64),
    digest: str = DIGEST_A,
) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": "application/octet-stream",
        "byte_size": 3,
        "digest": digest,
    }


def allowed_policy() -> dict[str, Any]:
    return {
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "prohibited_paths": [".git"],
        "allowed_stages": ["plan", "loop", "verify"],
        "allowed_commands": [["pytest", "-q"]],
        "allowed_environment_keys": ["PATH"],
        "network_profile": "model-gateway-only",
        "credential_profile": "no-external-credentials",
        "model_profile": "default-model",
        "max_wall_seconds": 600,
        "max_agent_turns": 20,
        "max_model_tokens": 50_000,
    }


def run_spec() -> dict[str, Any]:
    payload = document_envelope("run-spec/v1")
    payload.update(
        {
            "operation": "author",
            "workspace": {
                "path": "/workspace/target",
                "initial_state": "clean_base",
            },
            "task": {
                "outcome": "Implement the deterministic fixture.",
                "acceptance_criteria": ["The fixture passes."],
                "non_goals": [],
                "constraints": ["Do not publish."],
            },
            "policy": allowed_policy(),
            "capabilities": {
                "required": ["author"],
                "optional": ["structured-events"],
            },
            "context": {
                "manifest_path": "/factory/input/context/context-bundle.json",
                "expected_digest": DIGEST_A,
            },
            "resume": {
                "checkpoint_path": None,
                "expected_digest": None,
            },
            "outputs": {
                "output_dir": "/factory/output",
                "stream_events_to_stdout": False,
            },
        }
    )
    return payload


def context_bundle() -> dict[str, Any]:
    payload = document_envelope("context-bundle/v1")
    payload.update(
        {
            "context_bundle_id": "context-01",
            "manifest_entries": [
                {
                    "logical_path": "work-item/revision.md",
                    "media_type": "text/markdown",
                    "byte_size": 3,
                    "digest": DIGEST_A,
                    "classification": "work_item_revision",
                }
            ],
            "work_item_revision": {
                "outcome": "Implement the deterministic fixture.",
                "acceptance_criteria": ["The fixture passes."],
            },
            "repository_instructions": [],
            "trusted_policy_summary": ["No publication authority."],
            "approved_repository_memory": [],
            "dependency_outputs": [],
            "operator_input": [],
            "construction": {
                "builder": "fixture-builder/v1",
                "source_digests": [DIGEST_A],
            },
            "bundle_digest": DIGEST_B,
        }
    )
    return payload


def checkpoint() -> dict[str, Any]:
    payload = document_envelope("checkpoint/v1")
    payload["identity"] = identity(attempt_id="attempt-00")
    payload.update(
        {
            "checkpoint_id": "checkpoint-01",
            "sequence": 1,
            "producer_attempt_id": "attempt-00",
            "compatibility": {
                "protocol": PROTOCOL,
                "required_capabilities": ["author"],
                "minimum_runner_version": "1.4.0",
            },
            "context_bundle_digest": DIGEST_A,
            "run_spec_digest": DIGEST_B,
            "workspace_patch_digest": DIGEST_C,
            "completed_stages": ["plan"],
            "next_permitted_stage": "loop",
            "workflow_state": {"status": "ready"},
            "evidence_refs": [],
            "artifact_manifest": [artifact_ref()],
            "authority": allowed_policy(),
            "budgets": {
                "consumed": {
                    "wall_seconds": 10,
                    "agent_turns": 2,
                    "model_tokens": 1_000,
                },
                "remaining": {
                    "wall_seconds": 590,
                    "agent_turns": 18,
                    "model_tokens": 49_000,
                },
            },
            "decisions": ["Use the fixture adapter."],
            "assumptions": [],
            "open_questions": [],
            "object_digests": [DIGEST_A],
            "checkpoint_digest": DIGEST_D,
        }
    )
    return payload


def evidence_item(phase: str = "red") -> dict[str, Any]:
    return {
        "phase": phase,
        "command": ["pytest", "-q", "tests/test_app.py"],
        "working_directory": ".",
        "environment_keys": ["PATH"],
        "started_at": CREATED_AT,
        "finished_at": FINISHED_AT,
        "duration_seconds": 1.0,
        "exit_code": 1 if phase == "red" else 0,
        "termination_reason": "exited",
        "expected_status": "failed" if phase == "red" else "passed",
        "actual_status": "failed" if phase == "red" else "passed",
        "failure_classification": (
            "expected_behavioral_failure" if phase == "red" else "none"
        ),
        "stdout": artifact_ref("objects/" + ("b" * 64), DIGEST_B),
        "stderr": artifact_ref("objects/" + ("c" * 64), DIGEST_C),
        "test_reports": [],
        "tool_versions": {"pytest": "8.4.2"},
        "repository_files_changed": False,
    }


def verification_evidence() -> dict[str, Any]:
    payload = document_envelope("verification-evidence/v1")
    payload.update(
        {
            "environment_kind": "authoring",
            "runner": {"version": "1.4.0", "image": None},
            "context_digest": DIGEST_A,
            "change_set_digest": None,
            "items": [evidence_item()],
            "overall_status": "passed",
            "advisory_observations": [],
            "evidence_set_digest": DIGEST_C,
        }
    )
    return payload


def changed_file(operation: str = "modify") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": "src/app.py",
        "operation": operation,
        "previous_path": None,
        "previous_blob_digest": DIGEST_A,
        "resulting_blob_digest": DIGEST_B,
        "previous_mode": "100644",
        "resulting_mode": "100644",
        "binary": False,
        "allowed_path_decision": "allowed",
    }
    if operation == "add":
        entry["previous_blob_digest"] = None
        entry["previous_mode"] = None
    elif operation == "delete":
        entry["resulting_blob_digest"] = None
        entry["resulting_mode"] = None
    elif operation == "rename":
        entry["previous_path"] = "src/old-app.py"
    return entry


def change_set(operation: str = "modify") -> dict[str, Any]:
    payload = document_envelope("change-set/v1")
    payload.update(
        {
            "change_set_id": "change-set-01",
            "runner_digest": DIGEST_A,
            "context_digest": DIGEST_B,
            "patch": {
                **artifact_ref("changeset/change.patch", DIGEST_C),
                "media_type": "application/vnd.git.binary-patch",
            },
            "diff_digest": DIGEST_D,
            "changed_files": [changed_file(operation)],
            "evidence_set_digest": DIGEST_A,
            "evidence_refs": [artifact_ref("evidence/evidence.json", DIGEST_A)],
            "acceptance_criteria_results": [
                {"criterion": "The fixture passes.", "status": "passed"}
            ],
            "outcome_summary": "Implemented the deterministic fixture.",
            "assumptions": [],
            "residual_risks": [],
            "policy_observations": [],
            "generated_artifacts": [],
            "change_set_digest": DIGEST_B,
        }
    )
    return payload


def run_result(
    *,
    operation: str = "author",
    outcome: str = "succeeded",
) -> dict[str, Any]:
    payload = document_envelope("run-result/v1")
    payload.update(
        {
            "operation": operation,
            "outcome": outcome,
            "reason_code": "completed",
            "message": "The deterministic fixture completed.",
            "started_at": CREATED_AT,
            "finished_at": FINISHED_AT,
            "completed_stages": ["plan", "loop", "verify"],
            "latest_checkpoint": artifact_ref(
                "checkpoints/1/checkpoint.json", DIGEST_A
            ),
            "change_set": (
                artifact_ref("changeset/change-set.json", DIGEST_B)
                if operation == "author" and outcome == "succeeded"
                else None
            ),
            "verification_evidence": (
                artifact_ref("evidence/verification-evidence.json", DIGEST_C)
                if operation == "verify" and outcome == "succeeded"
                else None
            ),
            "event_stream_digest": DIGEST_A,
            "output_manifest_digest": DIGEST_B,
            "runner_build": {"version": "1.4.0", "source_commit": BASE_COMMIT_SHA},
            "result_digest": DIGEST_D,
        }
    )
    return payload


def runner_event(event_type: str = "StageCompleted") -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "schema": "runner-event/v1",
        "schema_version": 1,
        "run_id": "run-01",
        "attempt_id": "attempt-01",
        "sequence": 1,
        "timestamp": CREATED_AT,
        "event_type": event_type,
        "correlation_id": "correlation-01",
        "causation_id": None,
        "sanitised_payload": {"stage": "plan"},
        "artifact_refs": [],
    }


BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "RunSpec": run_spec,
    "ContextBundle": context_bundle,
    "Checkpoint": checkpoint,
    "VerificationEvidence": verification_evidence,
    "ChangeSet": change_set,
    "RunResult": run_result,
    "RunnerEvent": runner_event,
}

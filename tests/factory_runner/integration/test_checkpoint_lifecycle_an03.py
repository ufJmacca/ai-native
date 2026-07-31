from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ai_native.factory_runner import runner as runner_module
from ai_native.factory_runner.canonical import canonical_json_bytes
from ai_native.factory_runner.contracts.checkpoint import Checkpoint
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.process import CancellationToken
from ai_native.factory_runner.protocol import (
    contract_document_digest,
    sha256_digest,
    validate_contract,
    verify_contract_digest,
)
from tests.factory_runner.admission._fixtures import filesystem_snapshot
from tests.factory_runner.integration._support import (
    AUTHORED_APP,
    FactoryInvocation,
    REPOSITORY_ROOT,
    factory_command,
    factory_environment,
    invoke_factory,
    load_valid_result,
    prepare_clean_verification_from_author,
)

_AUTHOR_SAFE_BOUNDARIES = (
    "red",
    "stage:plan",
    "stage:architecture",
    "stage:prd",
    "stage:slice",
    "stage:loop",
    "stage:verify",
    "green",
    "refactor",
    "verification",
    "author-verification",
)
_ORDERED_AUTHOR_STAGES = (
    "plan",
    "architecture",
    "prd",
    "slice",
    "loop",
    "verify",
)
_ORDERED_AUTHOR_PHASES = ("red", "green", "refactor", "verification")
_CHECKPOINT_ATTEMPT_SECRET = "attempt-checkpoint-credential-DO-NOT-RESTORE-7bf824ac"
_BINARY_CHECKPOINT_ATTEMPT_SECRET = (
    "attempt-binary-checkpoint-credential-DO-NOT-RESTORE-59492f4c"
)
_KILL_AT_CHECKPOINT = Path(__file__).with_name("_kill_at_checkpoint.py")


def _events(invocation: FactoryInvocation) -> tuple[RunnerEvent, ...]:
    return tuple(
        validate_contract(line, expected_schema="runner-event/v1")
        for line in (invocation.output_dir / "events.ndjson").read_bytes().splitlines()
    )


def _checkpoint(invocation: FactoryInvocation, result: object) -> Checkpoint:
    reference = result.latest_checkpoint
    assert reference is not None
    content = (invocation.output_dir / reference.path).read_bytes()
    assert len(content) == reference.byte_size
    assert sha256_digest(content) == reference.digest
    model = validate_contract(content, expected_schema="checkpoint/v1")
    assert isinstance(model, Checkpoint)
    verify_contract_digest(model)
    for artifact in model.artifact_manifest:
        payload = (invocation.output_dir / artifact.path).read_bytes()
        assert len(payload) == artifact.byte_size
        assert sha256_digest(payload) == artifact.digest
    return model


def _cancel_after_loop(
    factory_invocation: Callable[..., FactoryInvocation],
) -> tuple[FactoryInvocation, object, Checkpoint]:
    invocation = factory_invocation(operation="author")
    pause_marker = invocation.marker_path.with_name("verification-agent.started")
    process = subprocess.Popen(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=factory_environment(
            invocation,
            agent_mode="author-pause-verify",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not pause_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pause_marker.exists()
    process.terminate()
    stdout, stderr = process.communicate(timeout=40)
    assert process.returncode == 8, stderr
    assert stdout == ""

    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    events = _events(invocation)
    event_types = tuple(event.event_type for event in events)
    cancellation = event_types.index("RunnerCancellationRequested")
    checkpoint_written = max(
        index
        for index, event_type in enumerate(event_types)
        if event_type == "CheckpointWritten"
    )
    assert cancellation < checkpoint_written
    assert events[checkpoint_written].artifact_refs == (result.latest_checkpoint,)
    assert result.outcome == "cancelled"
    assert checkpoint.completed_stages[-1] == "loop"
    assert checkpoint.next_permitted_stage == "verify"
    assert checkpoint.workspace_patch_digest is not None
    return invocation, result, checkpoint


def _replacement(
    invocation: FactoryInvocation,
    result: object,
    checkpoint: Checkpoint,
    *,
    label: str,
) -> FactoryInvocation:
    resume_root = invocation.input_dir / "resume"
    shutil.copytree(
        invocation.output_dir / "checkpoints",
        resume_root / "checkpoints",
        dirs_exist_ok=True,
    )
    reference = result.latest_checkpoint
    assert reference is not None
    output_dir = invocation.output_dir.with_name(f"output-{label}")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    payload["identity"]["attempt_id"] = f"attempt-an-03-{label}"
    payload["resume"] = {
        "checkpoint_path": str((resume_root / reference.path).resolve()),
        "expected_digest": checkpoint.checkpoint_digest,
    }
    payload["outputs"]["output_dir"] = str(output_dir.resolve())
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return replace(
        invocation,
        output_dir=output_dir,
        marker_path=invocation.marker_path.with_name(f"agent-calls-{label}.log"),
    )


def _reset_workspace(invocation: FactoryInvocation) -> None:
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=invocation.workspace,
        check=True,
        capture_output=True,
    )


def _resume_root(invocation: FactoryInvocation) -> Path:
    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    checkpoint_path = Path(run_spec["resume"]["checkpoint_path"])
    return checkpoint_path.parents[2]


def _rewrite_phase_outcome_object(
    invocation: FactoryInvocation,
    reference: ArtifactReference,
    *,
    mutation: str,
) -> None:
    resume_root = _resume_root(invocation)
    checkpoint_path = resume_root / reference.path
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    outcome_state = checkpoint["workflow_state"]["phase_evidence"]["outcome_state"]
    old_digest = outcome_state["object_digest"]
    old_reference = next(
        item for item in checkpoint["artifact_manifest"] if item["digest"] == old_digest
    )
    old_object_path = resume_root / old_reference["path"]
    outcome_document = json.loads(old_object_path.read_text(encoding="utf-8"))
    green = next(
        outcome
        for outcome in outcome_document["outcomes"]
        if outcome["phase"] == "green"
    )
    if mutation == "cancelled":
        green["cancelled"] = True
    elif mutation == "command":
        green["items"][0]["command"] = ["python", "-c", "raise SystemExit(0)"]
    else:
        raise AssertionError("unsupported phase outcome mutation")

    new_content = canonical_json_bytes(outcome_document)
    new_digest = sha256_digest(new_content)
    new_reference = {
        **old_reference,
        "path": (
            f"checkpoints/{checkpoint['sequence']}/objects/"
            f"{new_digest.removeprefix('sha256:')}"
        ),
        "byte_size": len(new_content),
        "digest": new_digest,
    }
    new_object_path = resume_root / new_reference["path"]
    new_object_path.write_bytes(new_content)
    old_object_path.unlink()
    outcome_state["object_digest"] = new_digest
    outcome_state["byte_size"] = len(new_content)
    checkpoint["artifact_manifest"] = [
        new_reference if item["digest"] == old_digest else item
        for item in checkpoint["artifact_manifest"]
    ]
    checkpoint["evidence_refs"] = [
        new_reference if item["digest"] == old_digest else item
        for item in checkpoint["evidence_refs"]
    ]
    checkpoint["object_digests"] = [
        new_digest if digest == old_digest else digest
        for digest in checkpoint["object_digests"]
    ]
    checkpoint["checkpoint_digest"] = contract_document_digest(checkpoint)
    checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))

    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    run_spec["resume"]["expected_digest"] = checkpoint["checkpoint_digest"]
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _inject_workspace_patch(
    invocation: FactoryInvocation,
    reference: ArtifactReference,
    patch: bytes,
) -> None:
    resume_root = _resume_root(invocation)
    checkpoint_path = resume_root / reference.path
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    patch_digest = sha256_digest(patch)
    patch_reference = {
        "path": (
            f"checkpoints/{checkpoint['sequence']}/objects/"
            f"{patch_digest.removeprefix('sha256:')}"
        ),
        "media_type": "application/octet-stream",
        "byte_size": len(patch),
        "digest": patch_digest,
    }
    (resume_root / patch_reference["path"]).write_bytes(patch)
    checkpoint["workspace_patch_digest"] = patch_digest
    checkpoint["artifact_manifest"].append(patch_reference)
    checkpoint["artifact_manifest"].sort(key=lambda item: item["path"])
    checkpoint["object_digests"] = [
        item["digest"] for item in checkpoint["artifact_manifest"]
    ]
    checkpoint["checkpoint_digest"] = contract_document_digest(checkpoint)
    checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))

    run_spec = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    run_spec["resume"]["expected_digest"] = checkpoint["checkpoint_digest"]
    invocation.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_successful_author_result_references_a_valid_latest_checkpoint(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")

    completed = invoke_factory(invocation, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    writes = [
        event
        for event in _events(invocation)
        if event.event_type == "CheckpointWritten"
    ]
    assert (
        tuple(event.sanitised_payload["boundary"] for event in writes)
        == _AUTHOR_SAFE_BOUNDARIES
    )
    assert tuple(event.sanitised_payload["sequence"] for event in writes) == tuple(
        range(1, len(writes) + 1)
    )
    assert writes[-1].artifact_refs == (result.latest_checkpoint,)
    assert checkpoint.completed_stages == result.completed_stages
    assert checkpoint.next_permitted_stage is None
    for event in writes:
        reference = event.artifact_refs[0]
        stored = validate_contract(
            (invocation.output_dir / reference.path).read_bytes(),
            expected_schema="checkpoint/v1",
        )
        assert isinstance(stored, Checkpoint)
        assert stored.evidence_refs
        assert set(stored.evidence_refs).issubset(set(stored.artifact_manifest))
        assert all(
            item.path.startswith(f"checkpoints/{stored.sequence}/objects/")
            for item in stored.evidence_refs
        )


def _assert_resume_without_replay(
    original: FactoryInvocation,
    result: object,
    stored: Checkpoint,
    *,
    boundary: str,
    label: str,
) -> None:
    _reset_workspace(original)
    resumed = _replacement(
        original,
        result,
        stored,
        label=label,
    )

    replacement = invoke_factory(resumed, agent_mode="author")

    assert replacement.returncode == 0, f"{boundary}: {replacement.stderr}"
    replacement_result = load_valid_result(resumed)
    assert replacement_result.reason_code == "completed", boundary
    assert (resumed.workspace / "app.py").read_text() == AUTHORED_APP
    resume_events = _events(resumed)
    event_types = tuple(event.event_type for event in resume_events)
    assert event_types.count("CheckpointRestored") == 1, boundary
    started_stages = tuple(
        event.sanitised_payload["stage"]
        for event in resume_events
        if event.event_type == "StageStarted"
    )
    assert started_stages == tuple(
        stage
        for stage in _ORDERED_AUTHOR_STAGES
        if stage not in stored.completed_stages
    ), boundary
    completed_phases = tuple(stored.workflow_state["completed_phases"])
    started_phases = tuple(
        event.sanitised_payload["phase"]
        for event in resume_events
        if event.event_type == "TestStarted"
    )
    assert started_phases == tuple(
        phase for phase in _ORDERED_AUTHOR_PHASES if phase not in completed_phases
    ), boundary


@pytest.mark.parametrize("boundary", _AUTHOR_SAFE_BOUNDARIES)
def test_every_author_safe_boundary_handles_sigterm_and_resumes(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    original = factory_invocation(operation="author")
    cancellation = CancellationToken()
    publish_external_bundle = runner_module.OutputWriter.publish_external_bundle
    terminated_at_boundary = False
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

    def request_cancellation(
        _signum: int,
        _frame: object,
    ) -> None:
        cancellation.cancel()

    def cancel_after_boundary_checkpoint(
        writer: object,
        references: object,
        contents: object,
    ) -> object:
        nonlocal terminated_at_boundary
        published = publish_external_bundle(
            writer,
            references,  # type: ignore[arg-type]
            contents,  # type: ignore[arg-type]
        )
        assert isinstance(contents, dict)
        checkpoint_contents = [
            content
            for path, content in contents.items()
            if isinstance(path, str) and path.endswith("/checkpoint.json")
        ]
        if checkpoint_contents:
            checkpoint = json.loads(checkpoint_contents[0])
            if checkpoint["workflow_state"]["boundary"] == boundary:
                terminated_at_boundary = True
                os.kill(os.getpid(), signal.SIGTERM)
        return published

    signal.signal(signal.SIGTERM, request_cancellation)
    monkeypatch.setattr(
        runner_module.OutputWriter,
        "publish_external_bundle",
        cancel_after_boundary_checkpoint,
    )
    try:
        exit_code = runner_module.execute_factory(
            expected_operation="author",
            run_spec_path=original.run_spec_path,
            output_dir=original.output_dir,
            environment=factory_environment(original, agent_mode="author"),
            cancellation_token=cancellation,
            log=lambda _message: None,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)

    assert exit_code == 8, boundary
    assert terminated_at_boundary, boundary
    result = load_valid_result(original)
    assert result.outcome == "cancelled", boundary
    stored = _checkpoint(original, result)
    writes = tuple(
        event for event in _events(original) if event.event_type == "CheckpointWritten"
    )
    assert writes[-2].sanitised_payload["boundary"] == boundary
    assert writes[-1].sanitised_payload["boundary"] == "cancellation"
    assert writes[-1].artifact_refs == (result.latest_checkpoint,)
    assert stored.workflow_state["boundary"] == "cancellation"
    _assert_resume_without_replay(
        original,
        result,
        stored,
        boundary=boundary,
        label=f"sigterm-{boundary.replace(':', '-')}",
    )


@pytest.mark.parametrize("boundary", _AUTHOR_SAFE_BOUNDARIES)
def test_every_author_safe_boundary_survives_sigkill_and_resume(
    factory_invocation: Callable[..., FactoryInvocation],
    boundary: str,
) -> None:
    original = factory_invocation(operation="author")
    killed = subprocess.run(
        [
            sys.executable,
            str(_KILL_AT_CHECKPOINT),
            "--run-spec",
            str(original.run_spec_path),
            "--output-dir",
            str(original.output_dir),
            "--boundary",
            boundary,
        ],
        cwd=REPOSITORY_ROOT,
        env=factory_environment(original, agent_mode="author"),
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert killed.returncode == -signal.SIGKILL, f"{boundary}: {killed.stderr}"
    assert not (original.output_dir / "completion.json").exists(), boundary
    assert not (original.output_dir / "result" / "run-result.json").exists(), boundary
    checkpoint_paths = tuple(
        sorted(
            original.output_dir.glob("checkpoints/*/checkpoint.json"),
            key=lambda path: int(path.parent.name),
        )
    )
    assert checkpoint_paths, boundary
    checkpoint_path = checkpoint_paths[-1]
    checkpoint_content = checkpoint_path.read_bytes()
    reference = ArtifactReference(
        path=checkpoint_path.relative_to(original.output_dir).as_posix(),
        media_type="application/json",
        byte_size=len(checkpoint_content),
        digest=sha256_digest(checkpoint_content),
    )
    result = SimpleNamespace(latest_checkpoint=reference)
    stored = _checkpoint(original, result)
    assert stored.workflow_state["boundary"] == boundary
    _assert_resume_without_replay(
        original,
        result,
        stored,
        boundary=boundary,
        label=f"sigkill-{boundary.replace(':', '-')}",
    )


def test_no_change_result_checkpoints_the_already_green_baseline(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    command = list(payload["policy"]["allowed_commands"][0])
    command[-1] = "raise SystemExit(0)"
    payload["policy"]["allowed_commands"] = [command]
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = invoke_factory(invocation, agent_mode="author-no-change")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    assert result.outcome == "no_change"
    assert checkpoint.workspace_patch_digest is None
    assert checkpoint.workflow_state["baseline_already_green"] is True
    assert checkpoint.workflow_state["completed_phases"] == (
        "red",
        "verification",
    )
    phase_descriptor = checkpoint.workflow_state["phase_evidence"]
    assert phase_descriptor["schema"] == "phase-evidence-state/v1"
    assert (
        checkpoint.workflow_state["already_green_observation"]["schema"]
        == "already-green-observation-state/v1"
    )
    assert checkpoint.evidence_refs
    assert set(checkpoint.evidence_refs).issubset(set(checkpoint.artifact_manifest))
    assert checkpoint.artifact_manifest
    writes = [
        event
        for event in _events(invocation)
        if event.event_type == "CheckpointWritten"
    ]
    assert writes[-1].sanitised_payload["boundary"] == "verification"


def test_no_change_checkpoint_resumes_from_detached_proof_without_reexecution(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    command = list(payload["policy"]["allowed_commands"][0])
    command[-1] = "raise SystemExit(0)"
    payload["policy"]["allowed_commands"] = [command]
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    completed = invoke_factory(invocation, agent_mode="author-no-change")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    checkpoint = _checkpoint(invocation, result)
    resumed = _replacement(invocation, result, checkpoint, label="no-change-resume")

    replacement = invoke_factory(resumed, agent_mode="fail-if-called")

    assert replacement.returncode == 0, replacement.stderr
    replacement_result = load_valid_result(resumed)
    assert replacement_result.outcome == "no_change"
    assert not resumed.marker_path.exists()
    assert all(event.event_type != "TestStarted" for event in _events(resumed))


def test_clean_verification_result_has_a_verify_only_checkpoint(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
) -> None:
    author = factory_invocation(operation="author")
    authored = invoke_factory(author, agent_mode="author")
    assert authored.returncode == 0, authored.stderr
    author_result = load_valid_result(author)
    verification = prepare_clean_verification_from_author(
        tmp_path / "clean-verification",
        author_invocation=author,
        author_result=author_result,
    )

    completed = invoke_factory(
        verification,
        agent_mode="fail-if-called",
    )

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(verification)
    checkpoint = _checkpoint(verification, result)
    assert checkpoint.operation == "verify"
    assert checkpoint.completed_stages == ("verify",)
    assert checkpoint.next_permitted_stage is None
    assert checkpoint.workspace_patch_digest is None
    assert checkpoint.workflow_state["completed_phases"] == ("verification",)
    assert (
        checkpoint.workflow_state["phase_evidence"]["schema"]
        == "phase-evidence-state/v1"
    )
    assert checkpoint.evidence_refs
    assert set(checkpoint.evidence_refs).issubset(set(checkpoint.artifact_manifest))
    writes = [
        event
        for event in _events(verification)
        if event.event_type == "CheckpointWritten"
    ]
    assert len(writes) == 1
    assert writes[0].sanitised_payload["boundary"] == "clean-verification"
    assert writes[0].artifact_refs == (result.latest_checkpoint,)


def test_completed_clean_verification_checkpoint_resumes_without_reexecution(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
) -> None:
    author = factory_invocation(operation="author")
    authored = invoke_factory(author, agent_mode="author")
    assert authored.returncode == 0, authored.stderr
    author_result = load_valid_result(author)
    verification = prepare_clean_verification_from_author(
        tmp_path / "clean-verification-resume",
        author_invocation=author,
        author_result=author_result,
    )
    completed = invoke_factory(verification, agent_mode="fail-if-called")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(verification)
    checkpoint = _checkpoint(verification, result)
    resumed = _replacement(
        verification,
        result,
        checkpoint,
        label="clean-verification-resumed",
    )

    replacement = invoke_factory(resumed, agent_mode="fail-if-called")

    assert replacement.returncode == 0, replacement.stderr
    replacement_result = load_valid_result(resumed)
    assert replacement_result.completed_stages == ("verify",)
    assert all(event.event_type != "TestStarted" for event in _events(resumed))


def test_patch_free_checkpoint_workflows_reject_injected_workspace_changes(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
) -> None:
    author = factory_invocation(operation="author")
    authored = invoke_factory(author, agent_mode="author")
    assert authored.returncode == 0, authored.stderr
    author_result = load_valid_result(author)
    author_checkpoint = _checkpoint(author, author_result)
    patch_reference = next(
        artifact
        for artifact in author_checkpoint.artifact_manifest
        if artifact.digest == author_checkpoint.workspace_patch_digest
    )
    patch = (author.output_dir / patch_reference.path).read_bytes()

    no_change = factory_invocation(operation="author")
    payload = json.loads(no_change.run_spec_path.read_text(encoding="utf-8"))
    command = list(payload["policy"]["allowed_commands"][0])
    command[-1] = "raise SystemExit(0)"
    payload["policy"]["allowed_commands"] = [command]
    no_change.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    no_change_completed = invoke_factory(
        no_change,
        agent_mode="author-no-change",
    )
    assert no_change_completed.returncode == 0, no_change_completed.stderr
    no_change_result = load_valid_result(no_change)
    no_change_checkpoint = _checkpoint(no_change, no_change_result)

    verification = prepare_clean_verification_from_author(
        tmp_path / "patch-free-clean-verification",
        author_invocation=author,
        author_result=author_result,
    )
    verified = invoke_factory(verification, agent_mode="fail-if-called")
    assert verified.returncode == 0, verified.stderr
    verification_result = load_valid_result(verification)
    verification_checkpoint = _checkpoint(verification, verification_result)

    cases = (
        ("already-green", no_change, no_change_result, no_change_checkpoint),
        (
            "clean-verification",
            verification,
            verification_result,
            verification_checkpoint,
        ),
    )
    for label, original, result, stored in cases:
        resumed = _replacement(original, result, stored, label=f"patched-{label}")
        reference = result.latest_checkpoint
        assert reference is not None
        _inject_workspace_patch(resumed, reference, patch)
        before = filesystem_snapshot(resumed.workspace)

        replacement = invoke_factory(resumed, agent_mode="fail-if-called")

        assert replacement.returncode == 5, f"{label}: {replacement.stderr}"
        assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
        assert not resumed.marker_path.exists()
        assert filesystem_snapshot(resumed.workspace) == before
        assert all(
            event.event_type != "CheckpointRestored" for event in _events(resumed)
        )


def test_current_attempt_secret_in_checkpoint_is_denied_before_workspace_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original = factory_invocation(operation="author")
    completed = invoke_factory(original, agent_mode="author")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(original)
    stored = _checkpoint(original, result)
    (original.workspace / "app.py").write_text(
        AUTHORED_APP + f"# {_CHECKPOINT_ATTEMPT_SECRET}\n",
        encoding="utf-8",
    )
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "app.py"],
        cwd=original.workspace,
        check=True,
        capture_output=True,
    ).stdout
    assert _CHECKPOINT_ATTEMPT_SECRET.encode() in patch
    _reset_workspace(original)
    resumed = _replacement(
        original,
        result,
        stored,
        label="checkpoint-attempt-secret",
    )
    reference = result.latest_checkpoint
    assert reference is not None
    _inject_workspace_patch(resumed, reference, patch)
    before = filesystem_snapshot(resumed.workspace)
    environment = factory_environment(resumed, agent_mode="fail-if-called")
    environment["SERVICE_TOKEN"] = _CHECKPOINT_ATTEMPT_SECRET

    replacement = subprocess.run(
        factory_command(resumed),
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert replacement.returncode == 3, replacement.stderr
    assert _CHECKPOINT_ATTEMPT_SECRET not in replacement.stdout
    assert _CHECKPOINT_ATTEMPT_SECRET not in replacement.stderr
    assert not resumed.output_dir.exists()
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before


def test_binary_checkpoint_secret_is_rolled_back_before_workspace_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original = factory_invocation(operation="author")
    completed = invoke_factory(original, agent_mode="author")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(original)
    stored = _checkpoint(original, result)
    (original.workspace / "app.py").write_bytes(
        b"\x00binary-secret:" + _BINARY_CHECKPOINT_ATTEMPT_SECRET.encode() + b"\n"
    )
    patch = subprocess.run(
        ["git", "diff", "--binary", "--full-index", "HEAD", "--", "app.py"],
        cwd=original.workspace,
        check=True,
        capture_output=True,
    ).stdout
    assert b"GIT binary patch" in patch
    assert _BINARY_CHECKPOINT_ATTEMPT_SECRET.encode() not in patch
    _reset_workspace(original)
    resumed = _replacement(
        original,
        result,
        stored,
        label="binary-checkpoint-attempt-secret",
    )
    reference = result.latest_checkpoint
    assert reference is not None
    _inject_workspace_patch(resumed, reference, patch)
    before = filesystem_snapshot(resumed.workspace)
    environment = factory_environment(resumed, agent_mode="fail-if-called")
    environment["SERVICE_TOKEN"] = _BINARY_CHECKPOINT_ATTEMPT_SECRET

    replacement = subprocess.run(
        factory_command(resumed),
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert replacement.returncode == 5, replacement.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    after = filesystem_snapshot(resumed.workspace)
    assert tuple(
        entry
        for entry in after
        if entry[0] != ".git" and not entry[0].startswith(".git/")
    ) == tuple(
        entry
        for entry in before
        if entry[0] != ".git" and not entry[0].startswith(".git/")
    )
    assert all(event.event_type != "CheckpointRestored" for event in _events(resumed))
    assert _BINARY_CHECKPOINT_ATTEMPT_SECRET not in replacement.stdout
    assert _BINARY_CHECKPOINT_ATTEMPT_SECRET not in replacement.stderr


def test_replacement_restores_and_continues_without_replaying_stages(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    assert (original.workspace / "app.py").read_text() != AUTHORED_APP
    resumed = _replacement(original, cancelled, stored, label="resumed")

    completed = invoke_factory(resumed, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(resumed)
    continued = _checkpoint(resumed, result)
    events = _events(resumed)
    started = tuple(
        event.sanitised_payload["stage"]
        for event in events
        if event.event_type == "StageStarted"
    )
    assert tuple(event.event_type for event in events).index(
        "CheckpointRestored"
    ) < tuple(event.event_type for event in events).index("StageStarted")
    assert started == ("verify",)
    assert (resumed.workspace / "app.py").read_text() == AUTHORED_APP
    assert continued.sequence > stored.sequence
    assert continued.completed_stages == (*stored.completed_stages, "verify")


def test_replacement_may_remove_already_completed_stage_authority(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    resumed = _replacement(original, cancelled, stored, label="narrowed-stages")
    payload = json.loads(resumed.run_spec_path.read_text(encoding="utf-8"))
    payload["policy"]["allowed_stages"] = ["verify"]
    resumed.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = invoke_factory(resumed, agent_mode="author")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(resumed)
    continued = _checkpoint(resumed, result)
    started = tuple(
        event.sanitised_payload["stage"]
        for event in _events(resumed)
        if event.event_type == "StageStarted"
    )
    assert started == ("verify",)
    assert result.completed_stages == ("verify",)
    assert continued.completed_stages == ("verify",)
    assert continued.authority.allowed_stages == ("verify",)


def test_corrupt_resume_is_rejected_before_workspace_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    resumed = _replacement(original, cancelled, stored, label="corrupt")
    patch = next(
        artifact
        for artifact in stored.artifact_manifest
        if artifact.digest == stored.workspace_patch_digest
    )
    object_path = _resume_root(resumed) / patch.path
    object_path.write_bytes(b"x" * object_path.stat().st_size)
    before = filesystem_snapshot(resumed.workspace)

    completed = invoke_factory(resumed, agent_mode="fail-if-called")

    assert completed.returncode == 5, completed.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before


def test_narrowed_path_policy_rejects_restore_without_workspace_mutation(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(original)
    resumed = _replacement(original, cancelled, stored, label="narrowed-paths")
    payload = json.loads(resumed.run_spec_path.read_text(encoding="utf-8"))
    payload["policy"]["allowed_paths"] = ["tests/**"]
    resumed.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = filesystem_snapshot(resumed.workspace)

    completed = invoke_factory(resumed, agent_mode="fail-if-called")

    assert completed.returncode == 5, completed.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before


def test_phase_progress_cannot_outrun_the_checkpoint_stage_cursor(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    original = factory_invocation(operation="author")
    completed = invoke_factory(original, agent_mode="author")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(original)
    stored = _checkpoint(original, result)
    _reset_workspace(original)
    resumed = _replacement(original, result, stored, label="phase-cursor-mismatch")
    reference = result.latest_checkpoint
    assert reference is not None
    checkpoint_path = _resume_root(resumed) / reference.path
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["completed_stages"] = []
    payload["next_permitted_stage"] = "plan"
    payload["workflow_state"].pop("private_author_state", None)
    payload["checkpoint_digest"] = contract_document_digest(payload)
    checkpoint_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    run_spec = json.loads(resumed.run_spec_path.read_text(encoding="utf-8"))
    run_spec["resume"]["expected_digest"] = payload["checkpoint_digest"]
    resumed.run_spec_path.write_text(
        json.dumps(run_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = filesystem_snapshot(resumed.workspace)

    replacement = invoke_factory(resumed, agent_mode="fail-if-called")

    assert replacement.returncode == 5, replacement.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before
    assert all(event.event_type != "CheckpointRestored" for event in _events(resumed))


def test_immediate_restored_successor_can_resume_a_third_attempt(
    factory_invocation: Callable[..., FactoryInvocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, cancelled, stored = _cancel_after_loop(factory_invocation)
    _reset_workspace(first)
    second = _replacement(first, cancelled, stored, label="second-attempt")
    cancellation = CancellationToken()
    publish_external_bundle = runner_module.OutputWriter.publish_external_bundle

    def cancel_after_restored_checkpoint(
        writer: object,
        references: object,
        contents: object,
    ) -> object:
        published = publish_external_bundle(
            writer,
            references,  # type: ignore[arg-type]
            contents,  # type: ignore[arg-type]
        )
        assert isinstance(contents, dict)
        checkpoint_contents = [
            content
            for path, content in contents.items()
            if isinstance(path, str) and path.endswith("/checkpoint.json")
        ]
        if checkpoint_contents:
            checkpoint = json.loads(checkpoint_contents[0])
            if checkpoint["workflow_state"]["boundary"] == "restored":
                cancellation.cancel()
        return published

    monkeypatch.setattr(
        runner_module.OutputWriter,
        "publish_external_bundle",
        cancel_after_restored_checkpoint,
    )
    second_exit = runner_module.execute_factory(
        expected_operation="author",
        run_spec_path=second.run_spec_path,
        output_dir=second.output_dir,
        environment=factory_environment(second, agent_mode="author"),
        cancellation_token=cancellation,
        log=lambda _message: None,
    )
    assert second_exit == 8
    second_result = load_valid_result(second)
    second_checkpoint = _checkpoint(second, second_result)
    assert second_checkpoint.producer_attempt_id == ("attempt-an-03-second-attempt")
    assert not second.marker_path.exists()

    _reset_workspace(second)
    third = _replacement(
        second,
        second_result,
        second_checkpoint,
        label="third-attempt",
    )
    replacement = invoke_factory(third, agent_mode="author")

    assert replacement.returncode == 0, replacement.stderr
    assert load_valid_result(third).reason_code == "completed"


@pytest.mark.parametrize("mutation", ("cancelled", "command"))
def test_completed_phase_evidence_must_match_status_and_producer_authority(
    factory_invocation: Callable[..., FactoryInvocation],
    mutation: str,
) -> None:
    original = factory_invocation(operation="author")
    completed = invoke_factory(original, agent_mode="author")
    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(original)
    stored = _checkpoint(original, result)
    _reset_workspace(original)
    resumed = _replacement(
        original,
        result,
        stored,
        label=f"phase-evidence-{mutation}",
    )
    reference = result.latest_checkpoint
    assert reference is not None
    _rewrite_phase_outcome_object(
        resumed,
        reference,
        mutation=mutation,
    )
    before = filesystem_snapshot(resumed.workspace)

    replacement = invoke_factory(resumed, agent_mode="fail-if-called")

    assert replacement.returncode == 5, replacement.stderr
    assert load_valid_result(resumed).reason_code == "checkpoint_incompatible"
    assert not resumed.marker_path.exists()
    assert filesystem_snapshot(resumed.workspace) == before
    assert all(event.event_type != "CheckpointRestored" for event in _events(resumed))

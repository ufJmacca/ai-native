from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.contracts.verification_evidence import EvidenceItem
from ai_native.factory_runner.outputs import OutputWriter
from ai_native.factory_runner.phase_checkpoint import (
    PHASE_EVIDENCE_WORKFLOW_KEY,
    PhaseEvidenceError,
    PhaseEvidenceLimits,
    restore_phase_evidence,
    snapshot_phase_evidence,
)
from ai_native.factory_runner.process_policy import FactoryPolicyViolation
from ai_native.factory_runner.redaction import SecretPolicy, SecretScanner
from ai_native.factory_runner.verification import PhaseExecutionOutcome


COMMAND = ("pytest", "-q", "tests/test_app.py")
SECOND_COMMAND = ("pytest", "-q", "tests/test_other.py")
CREATED_AT = "2026-07-31T00:00:00Z"
FINISHED_AT = "2026-07-31T00:00:01Z"


def _inputs(
    output_dir: Path,
    *,
    operation: str = "author",
) -> Any:
    return SimpleNamespace(
        output_dir=output_dir,
        run_spec=SimpleNamespace(
            operation=operation,
            policy=SimpleNamespace(
                allowed_commands=(COMMAND, SECOND_COMMAND),
                allowed_environment_keys=("PATH",),
            ),
        ),
    )


def _reference(
    writer: OutputWriter,
    path: str,
    content: bytes,
    *,
    media_type: str = "text/plain",
):
    return writer.write_bytes(path, content, media_type=media_type)


def _item(
    *,
    phase: str,
    command: tuple[str, ...],
    stdout: Any,
    stderr: Any,
    reports: tuple[Any, ...] = (),
) -> EvidenceItem:
    red = phase == "red"
    return EvidenceItem(
        phase=cast(Any, phase),
        command=command,
        working_directory=".",
        environment_keys=("PATH",),
        started_at=CREATED_AT,
        finished_at=FINISHED_AT,
        duration_seconds=1.0,
        exit_code=1 if red else 0,
        termination_reason="exited",
        expected_status="failed" if red else "passed",
        actual_status="failed" if red else "passed",
        failure_classification=(
            "expected_behavioral_failure" if red else "none"
        ),
        stdout=stdout,
        stderr=stderr,
        test_reports=reports,
        tool_versions={"pytest": "9.0.0"},
        repository_files_changed=False,
    )


def _phase_fixture(
    writer: OutputWriter,
) -> tuple[tuple[PhaseExecutionOutcome, ...], dict[str, bytes]]:
    content = {
        "evidence/objects/red-command-001.stdout": b"",
        "evidence/objects/red-command-001.stderr": b"AssertionError\n",
        "evidence/objects/green-command-001.stdout": b"\xffgreen-one\n",
        "evidence/objects/green-command-001.stderr": b"",
        "evidence/objects/green-command-001.junit.xml": b"<testsuite />\n",
        "evidence/objects/green-command-002.stdout": b"green-two\n",
        "evidence/objects/green-command-002.stderr": b"",
    }
    refs = {
        path: _reference(
            writer,
            path,
            payload,
            media_type=(
                "application/xml" if path.endswith(".xml") else "text/plain"
            ),
        )
        for path, payload in content.items()
    }
    red = PhaseExecutionOutcome(
        phase="red",
        passed=True,
        cancelled=False,
        timed_out=False,
        items=(
            _item(
                phase="red",
                command=COMMAND,
                stdout=refs["evidence/objects/red-command-001.stdout"],
                stderr=refs["evidence/objects/red-command-001.stderr"],
            ),
        ),
    )
    green = PhaseExecutionOutcome(
        phase="green",
        passed=True,
        cancelled=False,
        timed_out=False,
        items=(
            _item(
                phase="green",
                command=COMMAND,
                stdout=refs["evidence/objects/green-command-001.stdout"],
                stderr=refs["evidence/objects/green-command-001.stderr"],
                reports=(
                    refs["evidence/objects/green-command-001.junit.xml"],
                ),
            ),
            _item(
                phase="green",
                command=SECOND_COMMAND,
                stdout=refs["evidence/objects/green-command-002.stdout"],
                stderr=refs["evidence/objects/green-command-002.stderr"],
            ),
        ),
    )
    return (red, green), content


def _object_mapping(snapshot: Any) -> dict[str, bytes]:
    return {
        f"checkpoints/3/objects/{item.digest.removeprefix('sha256:')}": (
            item.content
        )
        for item in snapshot.objects
    }


def test_phase_snapshot_captures_exact_refs_reports_and_deduplicated_objects(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    writer = OutputWriter(output)
    inputs = _inputs(output)
    outcomes, content = _phase_fixture(writer)

    snapshot = snapshot_phase_evidence(
        inputs,
        writer=writer,
        phase_outcomes=outcomes,
    )

    assert PHASE_EVIDENCE_WORKFLOW_KEY == "phase_evidence"
    assert snapshot.descriptor["schema"] == "phase-evidence-state/v1"
    assert [
        outcome["phase"] for outcome in snapshot.descriptor["outcomes"]
    ] == ["red", "green"]
    assert [
        artifact["path"] for artifact in snapshot.descriptor["artifacts"]
    ] == sorted(content)
    assert {
        artifact["digest"] for artifact in snapshot.descriptor["artifacts"]
    } == {sha256_digest(payload) for payload in content.values()}
    assert {item.content for item in snapshot.objects} == set(content.values())
    assert len(snapshot.objects) < len(content)
    assert all(
        item.digest == sha256_digest(item.content)
        for item in snapshot.objects
    )


def test_phase_restore_recreates_exact_outcomes_and_writer_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_writer = OutputWriter(source)
    source_inputs = _inputs(source)
    outcomes, content = _phase_fixture(source_writer)
    snapshot = snapshot_phase_evidence(
        source_inputs,
        writer=source_writer,
        phase_outcomes=outcomes,
    )
    destination = tmp_path / "destination"
    destination.mkdir()
    destination_writer = OutputWriter(destination)
    destination_inputs = _inputs(destination)

    restored = restore_phase_evidence(
        destination_inputs,
        writer=destination_writer,
        descriptor=snapshot.descriptor,
        objects=_object_mapping(snapshot),
    )

    assert restored == outcomes
    for path, expected in content.items():
        assert (destination / path).read_bytes() == expected
    repeated_refs = [
        reference
        for outcome in restored
        for item in outcome.items
        for reference in (item.stdout, item.stderr, *item.test_reports)
    ]
    assert [
        reference.model_dump(mode="json")
        for reference in repeated_refs
    ] == [
        reference.model_dump(mode="json")
        for outcome in outcomes
        for item in outcome.items
        for reference in (item.stdout, item.stderr, *item.test_reports)
    ]


@pytest.mark.parametrize(
    "mutation",
    ["phase-order", "command", "environment", "duplicate-ref"],
)
def test_snapshot_rejects_phase_or_admission_incompatibility(
    tmp_path: Path,
    mutation: str,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    writer = OutputWriter(output)
    inputs = _inputs(output)
    outcomes, _content = _phase_fixture(writer)
    red, green = outcomes
    if mutation == "phase-order":
        damaged = (green, red)
    elif mutation == "command":
        item = green.items[0].model_copy(update={"command": ("other",)})
        damaged = (red, PhaseExecutionOutcome(
            phase="green",
            passed=True,
            cancelled=False,
            timed_out=False,
            items=(item, green.items[1]),
        ))
    elif mutation == "environment":
        item = green.items[0].model_copy(update={"environment_keys": ()})
        damaged = (red, PhaseExecutionOutcome(
            phase="green",
            passed=True,
            cancelled=False,
            timed_out=False,
            items=(item, green.items[1]),
        ))
    else:
        item = green.items[0].model_copy(update={"stderr": green.items[0].stdout})
        damaged = (red, PhaseExecutionOutcome(
            phase="green",
            passed=True,
            cancelled=False,
            timed_out=False,
            items=(item, green.items[1]),
        ))

    with pytest.raises(
        PhaseEvidenceError,
        match="phase|order|command|environment|duplicate",
    ):
        snapshot_phase_evidence(
            inputs,
            writer=writer,
            phase_outcomes=damaged,
        )


@pytest.mark.parametrize("damage", ["path", "duplicate", "digest", "outcome"])
def test_restore_rejects_untrusted_descriptor_or_objects_before_writing(
    tmp_path: Path,
    damage: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_writer = OutputWriter(source)
    source_inputs = _inputs(source)
    outcomes, _content = _phase_fixture(source_writer)
    snapshot = snapshot_phase_evidence(
        source_inputs,
        writer=source_writer,
        phase_outcomes=outcomes,
    )
    descriptor = json.loads(canonical_json_bytes(snapshot.descriptor))
    objects = _object_mapping(snapshot)
    if damage == "path":
        descriptor["artifacts"][0]["path"] = "../escape"
    elif damage == "duplicate":
        descriptor["artifacts"].append(
            deepcopy(descriptor["artifacts"][0])
        )
    elif damage == "digest":
        first = next(iter(objects))
        objects[first] = b"x" * len(objects[first])
    else:
        descriptor["outcomes"][1]["items"][0]["command"] = ["other"]
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(
        PhaseEvidenceError,
        match="path|duplicate|digest|command|descriptor",
    ):
        restore_phase_evidence(
            _inputs(destination),
            writer=OutputWriter(destination),
            descriptor=descriptor,
            objects=objects,
        )

    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_snapshot_rejects_link_or_special_referenced_output(
    tmp_path: Path,
    replacement: str,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    writer = OutputWriter(output)
    inputs = _inputs(output)
    outcomes, _content = _phase_fixture(writer)
    target = output / outcomes[0].items[0].stderr.path
    target.unlink()
    if replacement == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"AssertionError\n")
        target.symlink_to(outside)
    else:
        os.mkfifo(target)

    with pytest.raises(PhaseEvidenceError, match="link|regular|artifact"):
        snapshot_phase_evidence(
            inputs,
            writer=writer,
            phase_outcomes=outcomes,
        )


def test_snapshot_enforces_secret_and_size_limits_without_echoing_content(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    writer = OutputWriter(output)
    inputs = _inputs(output)
    outcomes, _content = _phase_fixture(writer)
    canary = b"known-phase-evidence-credential"
    target = output / outcomes[1].items[0].stdout.path
    replacement = canary + b"-oversize"
    target.write_bytes(replacement)
    damaged_ref = outcomes[1].items[0].stdout.model_copy(
        update={
            "byte_size": len(replacement),
            "digest": sha256_digest(replacement),
        }
    )
    damaged_item = outcomes[1].items[0].model_copy(
        update={"stdout": damaged_ref}
    )
    damaged_green = PhaseExecutionOutcome(
        phase="green",
        passed=True,
        cancelled=False,
        timed_out=False,
        items=(damaged_item, outcomes[1].items[1]),
    )
    scanner = SecretScanner(SecretPolicy((("phase-token", canary),)))

    with pytest.raises(FactoryPolicyViolation) as caught:
        snapshot_phase_evidence(
            inputs,
            writer=writer,
            phase_outcomes=(outcomes[0], damaged_green),
            secret_scanner=scanner,
        )
    assert canary.decode() not in str(caught.value)

    with pytest.raises(PhaseEvidenceError, match="size|limit"):
        snapshot_phase_evidence(
            inputs,
            writer=writer,
            phase_outcomes=(outcomes[0], damaged_green),
            limits=PhaseEvidenceLimits(
                max_artifacts=20,
                max_artifact_bytes=8,
                max_total_bytes=64,
            ),
        )

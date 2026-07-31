from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_native.factory_runner.contracts.verification_evidence import EvidenceItem
from ai_native.factory_runner.outputs import OutputWriter
from ai_native.factory_runner.phase_checkpoint import (
    PhaseEvidenceError,
    PhaseEvidenceSnapshot,
    restore_phase_evidence,
    snapshot_phase_evidence,
)
from ai_native.factory_runner.verification import PhaseExecutionOutcome


PRODUCER_COMMAND = ("pytest", "-q", "tests/test_app.py")
DISJOINT_COMMAND = ("pytest", "-q", "tests/test_other.py")


def _inputs(
    output_dir: Path,
    *,
    commands: tuple[tuple[str, ...], ...],
    environment_keys: tuple[str, ...],
) -> Any:
    return SimpleNamespace(
        output_dir=output_dir,
        run_spec=SimpleNamespace(
            operation="author",
            policy=SimpleNamespace(
                allowed_commands=commands,
                allowed_environment_keys=environment_keys,
            ),
        ),
    )


def _producer_snapshot(output_dir: Path) -> PhaseEvidenceSnapshot:
    writer = OutputWriter(output_dir)
    stdout = writer.write_bytes(
        "evidence/objects/red-command-001.stdout",
        b"",
        media_type="text/plain",
    )
    stderr = writer.write_bytes(
        "evidence/objects/red-command-001.stderr",
        b"AssertionError\n",
        media_type="text/plain",
    )
    item = EvidenceItem(
        phase="red",
        command=PRODUCER_COMMAND,
        working_directory=".",
        environment_keys=("PATH",),
        started_at="2026-07-31T00:00:00Z",
        finished_at="2026-07-31T00:00:01Z",
        duration_seconds=1.0,
        exit_code=1,
        termination_reason="exited",
        expected_status="failed",
        actual_status="failed",
        failure_classification="expected_behavioral_failure",
        stdout=stdout,
        stderr=stderr,
        test_reports=(),
        tool_versions={},
        repository_files_changed=False,
    )
    return snapshot_phase_evidence(
        _inputs(
            output_dir,
            commands=(PRODUCER_COMMAND,),
            environment_keys=("PATH",),
        ),
        writer=writer,
        phase_outcomes=(
            PhaseExecutionOutcome(
                phase="red",
                passed=True,
                cancelled=False,
                timed_out=False,
                items=(item,),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("commands", "environment_keys"),
    (
        ((DISJOINT_COMMAND,), ("PATH",)),
        ((PRODUCER_COMMAND,), ("OTHER",)),
    ),
    ids=("allowed-commands", "allowed-environment-keys"),
)
def test_relaxed_restore_still_requires_current_authority_to_be_a_subset(
    tmp_path: Path,
    commands: tuple[tuple[str, ...], ...],
    environment_keys: tuple[str, ...],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    snapshot = _producer_snapshot(source)
    destination = tmp_path / "destination"
    destination.mkdir()
    objects = {
        f"checkpoints/1/objects/{item.digest.removeprefix('sha256:')}": (item.content)
        for item in snapshot.objects
    }

    with pytest.raises(
        PhaseEvidenceError,
        match="current policy exceeds phase evidence producer authority",
    ):
        restore_phase_evidence(
            _inputs(
                destination,
                commands=commands,
                environment_keys=environment_keys,
            ),
            writer=OutputWriter(destination),
            descriptor=snapshot.descriptor,
            objects=objects,
            enforce_current_policy=False,
        )

    assert list(destination.iterdir()) == []

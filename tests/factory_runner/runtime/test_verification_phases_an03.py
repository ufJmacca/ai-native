from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ai_native.factory_runner.admission import ValidatedInputs
from ai_native.factory_runner.contracts.common import (
    RepositoryIdentity,
    RunIdentity,
)
from ai_native.factory_runner.evidence import EvidenceSufficiencyError
from ai_native.factory_runner.outputs import OutputWriter
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessResult,
)
from ai_native.factory_runner.protocol import verify_contract_digest
from ai_native.factory_runner.verification import (
    FalseRedEvidenceError,
    PhaseCommandCompletion,
    PhaseCommandStart,
    RedAlreadyGreen,
    execute_declared_phase,
    execute_verification,
    finalize_authoring_evidence,
)


COMMAND = ("/usr/bin/env", "factory-verification-fixture")
SECOND_COMMAND = ("/usr/bin/env", "factory-verification-fixture-two")
CONTEXT_DIGEST = "sha256:" + ("a" * 64)
CHANGE_SET_DIGEST = "sha256:" + ("b" * 64)


class _FakeProcessRunner:
    def __init__(self, results: Sequence[FactoryProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> FactoryProcessResult:
        del cwd, environment, timeout_seconds
        self.calls.append(tuple(command))
        if not self._results:
            raise AssertionError("unexpected verification command")
        return self._results.pop(0)


class _StableGitRuntime:
    def run(self, *arguments: str) -> bytes:
        return b"stable:" + b"\0".join(value.encode() for value in arguments)


def _result(
    *,
    returncode: int | None,
    stdout: bytes = b"",
    stderr: bytes = b"",
    termination_reason: str = "exited",
) -> FactoryProcessResult:
    return FactoryProcessResult(
        command=COMMAND,
        returncode=returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        termination_reason=cast(Any, termination_reason),
        stdout_bytes=stdout,
        stderr_bytes=stderr,
    )


def _inputs(
    tmp_path: Path,
    *,
    commands: tuple[tuple[str, ...], ...] = (COMMAND,),
    clean_verification: bool = False,
) -> ValidatedInputs:
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()
    identity = RunIdentity(
        work_item_id="work-item-an-03",
        work_item_revision_id="revision-an-03",
        delivery_phase_id="AN-03",
        run_id="run-an-03",
        attempt_id="attempt-an-03",
        correlation_id="correlation-an-03",
    )
    repository = RepositoryIdentity(
        repository_id="fixture-repository",
        display_name="fixture/repository",
        base_commit_sha="a" * 40,
    )
    run_spec = SimpleNamespace(
        operation="verify" if clean_verification else "author",
        identity=identity,
        repository=repository,
        policy=SimpleNamespace(
            allowed_commands=commands,
            allowed_environment_keys=(),
        ),
    )
    return cast(
        ValidatedInputs,
        SimpleNamespace(
            run_spec=run_spec,
            workspace=workspace,
            output_dir=output_dir,
            environment={},
            context_digest=CONTEXT_DIGEST,
            change_set=(
                SimpleNamespace(change_set_digest=CHANGE_SET_DIGEST)
                if clean_verification
                else None
            ),
        ),
    )


def _execute_phase(
    inputs: ValidatedInputs,
    *,
    phase: str,
    results: Sequence[FactoryProcessResult],
    writer: OutputWriter,
    started: list[PhaseCommandStart] | None = None,
    completed: list[PhaseCommandCompletion] | None = None,
):
    scratch = inputs.workspace.parent / f"scratch-{phase}"
    return execute_declared_phase(
        inputs,
        phase=cast(Any, phase),
        writer=writer,
        process_runner=cast(Any, _FakeProcessRunner(results)),
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
        sterile_home=scratch / "home",
        temp_dir=scratch / "tmp",
        boundary_check=lambda: None,
        git_runtime=cast(Any, _StableGitRuntime()),
        on_command_started=None if started is None else started.append,
        on_command_completed=None if completed is None else completed.append,
    )


@pytest.mark.parametrize(
    ("phase", "result", "expected_status", "expected_classification"),
    [
        (
            "red",
            _result(returncode=1, stderr=b"Traceback\nAssertionError\n"),
            "failed",
            "expected_behavioral_failure",
        ),
        ("green", _result(returncode=0), "passed", "none"),
        ("refactor", _result(returncode=0), "passed", "none"),
        ("verification", _result(returncode=0), "passed", "none"),
    ],
)
def test_declared_phase_emits_exact_phase_specific_output_and_callbacks(
    tmp_path: Path,
    phase: str,
    result: FactoryProcessResult,
    expected_status: str,
    expected_classification: str,
) -> None:
    inputs = _inputs(tmp_path)
    writer = OutputWriter(inputs.output_dir)
    started: list[PhaseCommandStart] = []
    completed: list[PhaseCommandCompletion] = []

    outcome = _execute_phase(
        inputs,
        phase=phase,
        results=(result,),
        writer=writer,
        started=started,
        completed=completed,
    )

    assert len(outcome.items) == 1
    item = outcome.items[0]
    assert item.phase == phase
    assert item.actual_status == expected_status
    assert item.failure_classification == expected_classification
    assert item.stdout.path == f"evidence/objects/{phase}-command-001.stdout"
    assert item.stderr.path == f"evidence/objects/{phase}-command-001.stderr"
    assert (inputs.output_dir / item.stdout.path).read_bytes() == result.stdout_bytes
    assert (inputs.output_dir / item.stderr.path).read_bytes() == result.stderr_bytes
    assert started == [
        PhaseCommandStart(phase=cast(Any, phase), index=1, command=COMMAND)
    ]
    assert completed[0].phase == phase
    assert completed[0].actual_status == expected_status
    assert completed[0].failure_classification == expected_classification


@pytest.mark.parametrize(
    ("result", "expected_classification"),
    [
        (
            _result(returncode=1, stderr=b"SyntaxError: invalid syntax\n"),
            "syntax_error",
        ),
        (
            _result(returncode=2, stderr=b"ERROR collecting tests/test_app.py\n"),
            "collection_error",
        ),
        (
            _result(returncode=1, stderr=b"ModuleNotFoundError: no module named x\n"),
            "dependency_error",
        ),
        (
            _result(returncode=1, stderr=b"authentication credentials required\n"),
            "credential_error",
        ),
        (_result(returncode=1, stderr=b"unrelated test failed\n"), "unrelated_failure"),
        (
            _result(returncode=None, termination_reason="timed_out"),
            "timeout",
        ),
    ],
)
def test_false_red_is_rejected_after_capture_and_before_final_evidence(
    tmp_path: Path,
    result: FactoryProcessResult,
    expected_classification: str,
) -> None:
    inputs = _inputs(tmp_path)
    writer = OutputWriter(inputs.output_dir)

    with pytest.raises(FalseRedEvidenceError) as raised:
        _execute_phase(
            inputs,
            phase="red",
            results=(result,),
            writer=writer,
        )

    assert raised.value.failure_classification == expected_classification
    assert (
        inputs.output_dir / "evidence/objects/red-command-001.stdout"
    ).read_bytes() == result.stdout_bytes
    assert (
        inputs.output_dir / "evidence/objects/red-command-001.stderr"
    ).read_bytes() == result.stderr_bytes
    assert not (inputs.output_dir / "evidence/verification-evidence.json").exists()


def test_passing_red_probe_is_distinguished_for_explicit_no_change(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    writer = OutputWriter(inputs.output_dir)

    with pytest.raises(RedAlreadyGreen):
        _execute_phase(
            inputs,
            phase="red",
            results=(_result(returncode=0, stdout=b"already satisfied\n"),),
            writer=writer,
        )

    assert (
        inputs.output_dir / "evidence/objects/red-command-001.stdout"
    ).read_bytes() == b"already satisfied\n"
    assert not (inputs.output_dir / "evidence/verification-evidence.json").exists()


def test_author_evidence_is_written_once_after_complete_grouped_phase_history(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, commands=(COMMAND, SECOND_COMMAND))
    writer = OutputWriter(inputs.output_dir)
    red = _execute_phase(
        inputs,
        phase="red",
        results=(_result(returncode=1, stderr=b"AssertionError\n"),),
        writer=writer,
    )
    passing_results = (
        _result(returncode=0, stdout=b"\xfffirst\n"),
        _result(returncode=0, stdout=b"second\n"),
    )
    green = _execute_phase(
        inputs,
        phase="green",
        results=passing_results,
        writer=writer,
    )
    refactor = _execute_phase(
        inputs,
        phase="refactor",
        results=passing_results,
        writer=writer,
    )
    verification = _execute_phase(
        inputs,
        phase="verification",
        results=passing_results,
        writer=writer,
    )
    evidence_path = inputs.output_dir / "evidence/verification-evidence.json"
    assert not evidence_path.exists()

    outcome = finalize_authoring_evidence(
        inputs,
        writer=writer,
        phase_outcomes=(red, green, refactor, verification),
    )

    assert outcome.passed is True
    assert outcome.reference.path == "evidence/verification-evidence.json"
    assert tuple(item.phase for item in outcome.evidence.items) == (
        "red",
        "green",
        "green",
        "refactor",
        "refactor",
        "verification",
        "verification",
    )
    verify_contract_digest(outcome.evidence)
    with pytest.raises(ValueError, match="already exists"):
        finalize_authoring_evidence(
            inputs,
            writer=writer,
            phase_outcomes=(red, green, refactor, verification),
        )


def test_author_evidence_rejects_incomplete_or_failing_phase_history(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    writer = OutputWriter(inputs.output_dir)
    green = _execute_phase(
        inputs,
        phase="green",
        results=(_result(returncode=0),),
        writer=writer,
    )

    with pytest.raises(EvidenceSufficiencyError, match="red"):
        finalize_authoring_evidence(
            inputs,
            writer=writer,
            phase_outcomes=(green,),
        )


def test_clean_verification_run_cannot_finalize_authoring_provenance(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, clean_verification=True)
    writer = OutputWriter(inputs.output_dir)

    with pytest.raises(EvidenceSufficiencyError, match="author operation"):
        finalize_authoring_evidence(
            inputs,
            writer=writer,
            phase_outcomes=(),
        )


def test_clean_verification_keeps_clean_provenance_and_uses_phase_executor(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, clean_verification=True)
    writer = OutputWriter(inputs.output_dir)
    scratch = tmp_path / "scratch"

    outcome = execute_verification(
        inputs,
        writer=writer,
        process_runner=cast(
            Any,
            _FakeProcessRunner((_result(returncode=0, stdout=b"verified\n"),)),
        ),
        cancellation_token=CancellationToken(),
        deadline=Deadline.from_timeout(30),
        sterile_home=scratch / "home",
        temp_dir=scratch / "tmp",
        clean_verification=True,
        boundary_check=lambda: None,
        git_runtime=cast(Any, _StableGitRuntime()),
    )

    assert outcome.passed is True
    assert outcome.evidence.environment_kind == "clean_verification"
    assert outcome.evidence.change_set_digest == CHANGE_SET_DIGEST
    assert tuple(item.phase for item in outcome.evidence.items) == ("verification",)
    assert outcome.evidence.items[0].stdout.path == (
        "evidence/objects/verification-command-001.stdout"
    )
    verify_contract_digest(outcome.evidence)

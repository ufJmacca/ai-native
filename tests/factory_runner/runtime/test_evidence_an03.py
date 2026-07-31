from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.factory_runner.canonical import sha256_digest
from ai_native.factory_runner.contracts.common import (
    ArtifactReference,
    RepositoryIdentity,
    RunIdentity,
    RunnerBuildIdentity,
)
from ai_native.factory_runner.contracts.verification_evidence import EvidenceItem
from ai_native.factory_runner.evidence import (
    REDACTION_MARKER,
    EvidenceSufficiencyError,
    RedFailureObservation,
    build_authoring_evidence,
    classify_red_failure,
    redact_output_bytes,
)
from ai_native.factory_runner.protocol import verify_contract_digest


TIMESTAMP = "2026-07-31T00:00:00Z"
EMPTY_DIGEST = sha256_digest(b"")
CONTEXT_DIGEST = "sha256:" + ("a" * 64)
INTENDED_TEST = "tests/test_greeting.py::test_returns_requested_greeting"


def _red_observation(
    *,
    observed_failure: str = "assertion_failure",
    failed_tests: tuple[str, ...] = (INTENDED_TEST,),
    termination_reason: str = "exited",
    exit_code: int | None = 1,
) -> RedFailureObservation:
    return RedFailureObservation(
        exit_code=exit_code,
        termination_reason=termination_reason,
        observed_failure=observed_failure,
        intended_test=INTENDED_TEST,
        failed_tests=failed_tests,
    )


def _reference(path: str) -> ArtifactReference:
    return ArtifactReference(
        path=path,
        media_type="text/plain",
        byte_size=0,
        digest=EMPTY_DIGEST,
    )


def _item(phase: str) -> EvidenceItem:
    is_red = phase == "red"
    return EvidenceItem.model_validate(
        {
            "phase": phase,
            "command": ["pytest", "-q", INTENDED_TEST],
            "working_directory": ".",
            "environment_keys": [],
            "started_at": TIMESTAMP,
            "finished_at": TIMESTAMP,
            "duration_seconds": 0,
            "exit_code": 1 if is_red else 0,
            "termination_reason": "exited",
            "expected_status": "failed" if is_red else "passed",
            "actual_status": "failed" if is_red else "passed",
            "failure_classification": (
                "expected_behavioral_failure" if is_red else "none"
            ),
            "stdout": _reference(f"evidence/objects/{phase}.stdout"),
            "stderr": _reference(f"evidence/objects/{phase}.stderr"),
            "test_reports": [],
            "tool_versions": {"pytest": "fixture"},
            "repository_files_changed": phase in {"green", "refactor"},
        }
    )


def _author_evidence(
    items: tuple[EvidenceItem, ...],
    *,
    claimed_environment_kind: str = "authoring",
):
    return build_authoring_evidence(
        created_at=TIMESTAMP,
        identity=RunIdentity(
            work_item_id="work-item-an-03",
            work_item_revision_id="revision-an-03",
            delivery_phase_id="AN-03",
            run_id="run-an-03",
            attempt_id="attempt-an-03",
            correlation_id="correlation-an-03",
        ),
        repository=RepositoryIdentity(
            repository_id="fixture-repository",
            display_name="fixture/target-repository",
            base_commit_sha="a" * 40,
        ),
        runner=RunnerBuildIdentity(
            version="1.0.0",
            image=None,
            source_commit=None,
        ),
        context_digest=CONTEXT_DIGEST,
        items=items,
        advisory_observations=(),
        claimed_environment_kind=claimed_environment_kind,
    )


def test_red_classifier_accepts_only_the_intended_assertion_failure() -> None:
    decision = classify_red_failure(_red_observation())

    assert decision.accepted is True
    assert decision.failure_classification == "expected_behavioral_failure"


@pytest.mark.parametrize(
    ("observation", "expected_classification"),
    [
        (_red_observation(observed_failure="syntax_error"), "syntax_error"),
        (_red_observation(observed_failure="collection_error"), "collection_error"),
        (_red_observation(observed_failure="dependency_error"), "dependency_error"),
        (_red_observation(observed_failure="credential_error"), "credential_error"),
        (
            _red_observation(observed_failure="infrastructure_error"),
            "infrastructure_error",
        ),
        (
            _red_observation(
                observed_failure="timeout",
                termination_reason="timed_out",
                exit_code=None,
            ),
            "timeout",
        ),
        (
            _red_observation(
                failed_tests=("tests/test_unrelated.py::test_other_behavior",)
            ),
            "unrelated_failure",
        ),
        (
            _red_observation(
                failed_tests=(
                    INTENDED_TEST,
                    "tests/test_unrelated.py::test_other_behavior",
                )
            ),
            "unrelated_failure",
        ),
    ],
)
def test_red_classifier_rejects_false_red_failures(
    observation: RedFailureObservation,
    expected_classification: str,
) -> None:
    decision = classify_red_failure(observation)

    assert decision.accepted is False
    assert decision.failure_classification == expected_classification


def test_output_redaction_preserves_exact_bytes_except_for_secret_values() -> None:
    secret = b"secret-canary-an-03"
    raw = b"\x00before\xff\n" + secret + b"\r\nmiddle:" + secret + b":after\x80"

    preserved = redact_output_bytes(raw, secret_values=())
    redacted = redact_output_bytes(raw, secret_values=(secret,))

    assert preserved.content == raw
    assert preserved.redaction_count == 0
    assert redacted.content == raw.replace(secret, REDACTION_MARKER)
    assert redacted.redaction_count == 2
    assert secret not in redacted.content


def test_author_evidence_contains_complete_ordered_tdd_and_verification_phases() -> None:
    items = tuple(_item(phase) for phase in ("red", "green", "refactor", "verification"))

    evidence = _author_evidence(items)

    assert evidence.environment_kind == "authoring"
    assert evidence.change_set_digest is None
    assert tuple(item.phase for item in evidence.items) == (
        "red",
        "green",
        "refactor",
        "verification",
    )
    assert evidence.overall_status == "passed"
    verify_contract_digest(evidence)


@pytest.mark.parametrize(
    "missing_phase",
    ["red", "green", "refactor", "verification"],
)
def test_author_evidence_rejects_an_incomplete_phase_set(
    missing_phase: str,
) -> None:
    items = tuple(
        _item(phase)
        for phase in ("red", "green", "refactor", "verification")
        if phase != missing_phase
    )

    with pytest.raises(EvidenceSufficiencyError, match=missing_phase):
        _author_evidence(items)


def test_author_evidence_cannot_claim_clean_verification_provenance() -> None:
    items = tuple(_item(phase) for phase in ("red", "green", "refactor", "verification"))

    with pytest.raises(EvidenceSufficiencyError, match="authoring|clean"):
        _author_evidence(
            items,
            claimed_environment_kind="clean_verification",
        )


def test_evidence_module_does_not_normalise_output_through_text(tmp_path: Path) -> None:
    raw = b"\xff\xfe\x00raw\r\nbytes\n"
    captured = redact_output_bytes(raw, secret_values=())
    path = tmp_path / "captured.bin"

    path.write_bytes(captured.content)

    assert path.read_bytes() == raw
    assert sha256_digest(path.read_bytes()) == sha256_digest(raw)

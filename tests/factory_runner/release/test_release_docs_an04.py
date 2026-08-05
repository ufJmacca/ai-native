from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
RUNBOOK = REPOSITORY_ROOT / "docs" / "factory-runner" / "releasing.md"
README = REPOSITORY_ROOT / "docs" / "factory-runner" / "README.md"
RELEASES = REPOSITORY_ROOT / "docs" / "releases.md"
PHASE_EVIDENCE = tuple(
    REPOSITORY_ROOT / "docs" / "factory-runner" / "evidence" / f"AN-0{phase}.md"
    for phase in range(1, 5)
)


def test_makefile_exposes_reproducible_certification_and_release_gates() -> None:
    content = MAKEFILE.read_text(encoding="utf-8")

    assert "factory-certification-schemas:" in content
    assert "factory-certification-schemas-check:" in content
    assert "factory-release-tests:" in content
    assert (
        "python scripts/generate_factory_runner_certification_schemas.py --check"
        in content
    )
    assert "pytest tests/factory_runner/release" in content


def test_release_runbook_is_receipt_first_and_has_no_human_gate() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")
    lowered = " ".join(content.casefold().split())

    for required in (
        "release please",
        "draft release",
        "factory-runner-release-receipt.json",
        "compatibility-report.json",
        "ghcr.io/ufjmacca/ai-native-factory-runner@sha256:",
        "linux/amd64",
        "zero approving human reviews",
        "new semantic version",
        "gh attestation verify",
        "verify_factory_runner_release_receipt.py",
        "trusted cli merge",
        "gh api --method put",
        "sha=$expected_head_sha",
        "merge_method=merge",
    ):
        assert required in lowered

    assert "enables github auto-merge" not in lowered
    assert "--admin" not in lowered
    assert "--auto" not in lowered
    assert "manual approval" not in lowered
    assert "operator sign-off" not in lowered
    assert ":latest" not in lowered


def test_factory_runner_readme_links_the_release_runbook_and_an04_evidence() -> None:
    content = README.read_text(encoding="utf-8")
    lowered = content.casefold()

    assert "[Release and independent verification](releasing.md)" in content
    assert "[AN-04 release evidence](evidence/AN-04.md)" in content
    assert "trusted cli merge" in lowered
    assert "protected auto-merge remains" not in lowered


def test_general_release_docs_delegate_to_the_exact_head_merge_controller() -> None:
    lowered = RELEASES.read_text(encoding="utf-8").casefold()

    assert "trusted cli merge" in lowered
    assert "exact head" in lowered
    assert "native auto-merge is not used" in lowered


def test_phase_evidence_no_longer_delegates_to_native_auto_merge() -> None:
    for path in PHASE_EVIDENCE:
        lowered = " ".join(path.read_text(encoding="utf-8").casefold().split())

        assert "cli" in lowered, path.name
        assert "merge" in lowered, path.name
        for stale_policy in (
            "enables github auto-merge",
            "enables protected github auto-merge",
            "repository auto-merge is enabled",
            "workflow enables auto-merge",
            "receive protected github auto-merge",
        ):
            assert stale_policy not in lowered, path.name

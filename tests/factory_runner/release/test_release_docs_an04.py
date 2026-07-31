from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
RUNBOOK = REPOSITORY_ROOT / "docs" / "factory-runner" / "releasing.md"
README = REPOSITORY_ROOT / "docs" / "factory-runner" / "README.md"


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
    lowered = content.casefold()

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
    ):
        assert required in lowered

    assert "manual approval" not in lowered
    assert "operator sign-off" not in lowered
    assert ":latest" not in lowered


def test_factory_runner_readme_links_the_release_runbook_and_an04_evidence() -> None:
    content = README.read_text(encoding="utf-8")

    assert "[Release and independent verification](releasing.md)" in content
    assert "[AN-04 release evidence](evidence/AN-04.md)" in content

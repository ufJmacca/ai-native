from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from ai_native.factory_runner.compatibility_report import (
    COMPATIBILITY_REPORT_SCHEMA,
    COMPATIBILITY_SUITE_VERSION,
    FactoryRunnerCompatibilityReport,
    canonical_compatibility_report_bytes,
    compatibility_report_digest,
    validate_compatibility_report,
)


SOURCE_COMMIT = "a" * 40
SCHEMA_SET_DIGEST = "sha256:" + ("1" * 64)
SCHEMA_MANIFEST_DIGEST = "sha256:" + ("2" * 64)
WHEEL_DIGEST = "sha256:" + ("3" * 64)
IMAGE_DIGEST = "sha256:" + ("4" * 64)
IMAGE_REFERENCE = "ghcr.io/ufjmacca/ai-native-factory-runner@" + IMAGE_DIGEST
ARTIFACT_ORDER = ("source", "wheel", "oci")


def _digest(character: str) -> str:
    return "sha256:" + (character * 64)


def _build_identity(*, image: str | None) -> dict[str, object]:
    return {
        "schema": "factory-runner-build-identity/v1",
        "distribution": "ai-native-base",
        "version": "9.8.7",
        "source_repository": "ufJmacca/ai-native",
        "source_commit": SOURCE_COMMIT,
        "source_tag": None,
        "image": image,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": SCHEMA_MANIFEST_DIGEST,
    }


def _artifact_payloads() -> list[dict[str, object]]:
    return [
        {
            "kind": "source",
            "reference": f"ufJmacca/ai-native@{SOURCE_COMMIT}",
            "digest": None,
            "build_identity": _build_identity(image=None),
        },
        {
            "kind": "wheel",
            "reference": "ai_native_base-9.8.7-py3-none-any.whl",
            "digest": WHEEL_DIGEST,
            "build_identity": _build_identity(image=None),
        },
        {
            "kind": "oci",
            "reference": IMAGE_REFERENCE,
            "digest": IMAGE_DIGEST,
            "build_identity": _build_identity(image=IMAGE_REFERENCE),
        },
    ]


def _fixture_payload(
    fixture_id: str,
    operation: str,
    expected_outcome: str,
    digest_character: str,
) -> dict[str, object]:
    output_character, result_character, manifest_character = {
        "5": ("5", "6", "7"),
        "8": ("8", "9", "a"),
        "b": ("b", "c", "d"),
    }[digest_character]
    output_tree_digest = _digest(output_character)
    run_result_digest = _digest(result_character)
    output_manifest_digest = _digest(manifest_character)
    return {
        "fixture_id": fixture_id,
        "operation": operation,
        "expected_outcome": expected_outcome,
        "status": "passed",
        "canonical_output_tree_digest": output_tree_digest,
        "results": [
            {
                "artifact": artifact,
                "status": "passed",
                "actual_outcome": expected_outcome,
                "run_result_digest": run_result_digest,
                "output_manifest_digest": output_manifest_digest,
                "output_tree_digest": output_tree_digest,
            }
            for artifact in ARTIFACT_ORDER
        ],
    }


def compatibility_report_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": COMPATIBILITY_REPORT_SCHEMA,
        "protocol": "factory-runner-protocol/v1",
        "suite_version": COMPATIBILITY_SUITE_VERSION,
        "generated_at": "2026-07-31T06:00:00Z",
        "source_commit": SOURCE_COMMIT,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": SCHEMA_MANIFEST_DIGEST,
        "artifacts": _artifact_payloads(),
        "fixtures": [
            _fixture_payload("author-success", "author", "succeeded", "5"),
            _fixture_payload("author-no-change", "author", "no_change", "8"),
            _fixture_payload("verify-success", "verify", "succeeded", "b"),
        ],
        "status": "passed",
        "report_digest": _digest("0"),
    }
    payload["report_digest"] = compatibility_report_digest(payload)
    return payload


def test_compatibility_report_binds_all_artifacts_and_mandatory_fixtures() -> None:
    report = validate_compatibility_report(compatibility_report_payload())

    assert isinstance(report, FactoryRunnerCompatibilityReport)
    assert tuple(artifact.kind for artifact in report.artifacts) == ARTIFACT_ORDER
    assert tuple(fixture.fixture_id for fixture in report.fixtures) == (
        "author-success",
        "author-no-change",
        "verify-success",
    )
    assert report.status == "passed"
    assert report.report_digest == compatibility_report_digest(report)


def test_compatibility_report_has_one_canonical_encoding() -> None:
    payload = compatibility_report_payload()
    report = validate_compatibility_report(payload)

    assert canonical_compatibility_report_bytes(payload) == (
        canonical_compatibility_report_bytes(report)
    )
    assert json.loads(canonical_compatibility_report_bytes(report)) == payload
    assert b"\n" not in canonical_compatibility_report_bytes(report)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["artifacts"].reverse(),  # type: ignore[union-attr]
            "source, wheel, oci",
        ),
        (
            lambda payload: payload["artifacts"][1]["build_identity"].update(  # type: ignore[index,union-attr]
                {"schema_set_digest": _digest("e")}
            ),
            "shared build identity",
        ),
        (
            lambda payload: payload["artifacts"][2].update(  # type: ignore[index,union-attr]
                {"reference": ("ghcr.io/ufjmacca/ai-native-factory-runner:latest")}
            ),
            "digest-pinned",
        ),
        (
            lambda payload: payload["fixtures"][0]["results"][1].update(  # type: ignore[index,union-attr]
                {"output_tree_digest": _digest("f")}
            ),
            "equivalent",
        ),
        (
            lambda payload: payload["fixtures"][1]["results"][0].update(  # type: ignore[index,union-attr]
                {"actual_outcome": "succeeded"}
            ),
            "expected outcome",
        ),
        (
            lambda payload: payload["fixtures"].pop(),  # type: ignore[union-attr]
            "at least 3",
        ),
        (
            lambda payload: payload.update({"report_digest": _digest("f")}),
            "report_digest",
        ),
    ],
)
def test_compatibility_report_rejects_non_certifying_results(
    mutate,
    message: str,
) -> None:
    payload = copy.deepcopy(compatibility_report_payload())
    mutate(payload)
    if message != "report_digest":
        payload["report_digest"] = compatibility_report_digest(payload)

    with pytest.raises((ValidationError, ValueError), match=message):
        validate_compatibility_report(payload)


def test_compatibility_report_rejects_unknown_fields_and_duplicate_json() -> None:
    payload = compatibility_report_payload()
    payload["manual_approval"] = True

    with pytest.raises(ValidationError, match="manual_approval"):
        validate_compatibility_report(payload)

    encoded = canonical_compatibility_report_bytes(
        compatibility_report_payload()
    ).decode("utf-8")
    duplicated = encoded.replace(
        '"status":"passed"',
        '"status":"passed","status":"passed"',
        1,
    )
    with pytest.raises(ValueError, match="duplicate"):
        validate_compatibility_report(duplicated)

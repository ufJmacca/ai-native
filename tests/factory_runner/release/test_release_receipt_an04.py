from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from ai_native.factory_runner.release_receipt import (
    FactoryRunnerReleaseReceipt,
    validate_release_receipt,
)


SOURCE_COMMIT = "83e674f8161f38ef9bf4551e92bf655f278262c4"
WHEEL_DIGEST = "sha256:" + ("1" * 64)
IMAGE_DIGEST = "sha256:" + ("2" * 64)
SCHEMA_SET_DIGEST = "sha256:" + ("3" * 64)
SCHEMA_MANIFEST_DIGEST = "sha256:" + ("4" * 64)
REPORT_DIGEST = "sha256:" + ("5" * 64)
SBOM_DIGEST = "sha256:" + ("6" * 64)
SCAN_DIGEST = "sha256:" + ("7" * 64)
PROVENANCE_DIGEST = "sha256:" + ("8" * 64)


def _receipt_payload() -> dict[str, object]:
    tag = "ai-native-base-v1.5.0"
    release_root = f"https://github.com/ufJmacca/ai-native/releases/download/{tag}"
    repository = "ghcr.io/ufjmacca/ai-native-factory-runner"
    return {
        "receipt_schema": "factory-runner-release-receipt/v1",
        "protocol": "factory-runner-protocol/v1",
        "released_at": "2026-07-31T06:00:00Z",
        "source": {
            "repository": "ufJmacca/ai-native",
            "git_commit_sha": SOURCE_COMMIT,
            "git_tag": tag,
        },
        "wheel": {
            "distribution": "ai-native-base",
            "version": "1.5.0",
            "filename": "ai_native_base-1.5.0-py3-none-any.whl",
            "sha256": WHEEL_DIGEST,
            "download_url": (f"{release_root}/ai_native_base-1.5.0-py3-none-any.whl"),
        },
        "oci_image": {
            "repository": repository,
            "digest": IMAGE_DIGEST,
            "pinned_reference": f"{repository}@{IMAGE_DIGEST}",
            "platforms": ["linux/amd64"],
        },
        "contracts": {
            "schema_set_digest": SCHEMA_SET_DIGEST,
            "schema_manifest_sha256": SCHEMA_MANIFEST_DIGEST,
        },
        "compatibility": {
            "suite_version": "factory-runner-compatibility/v1",
            "status": "passed",
            "report_url": f"{release_root}/compatibility-report.json",
            "report_sha256": REPORT_DIGEST,
        },
        "supply_chain": {
            "sbom_url": f"{release_root}/factory-runner.spdx.json",
            "sbom_sha256": SBOM_DIGEST,
            "vulnerability_scan": {
                "scanner": "trivy",
                "policy": "no-fixable-critical-or-high/v1",
                "status": "passed",
                "report_url": f"{release_root}/trivy-report.sarif",
                "report_sha256": SCAN_DIGEST,
            },
            "provenance_url": f"{release_root}/provenance.intoto.jsonl",
            "provenance_sha256": PROVENANCE_DIGEST,
            "signature_reference": (
                "https://github.com/ufJmacca/ai-native/attestations/123456"
            ),
        },
    }


def test_release_receipt_accepts_only_complete_immutable_release_identity() -> None:
    receipt = validate_release_receipt(_receipt_payload())

    assert isinstance(receipt, FactoryRunnerReleaseReceipt)
    assert receipt.source.git_commit_sha == SOURCE_COMMIT
    assert receipt.oci_image.pinned_reference.endswith(f"@{IMAGE_DIGEST}")
    assert receipt.compatibility.status == "passed"
    assert receipt.supply_chain.vulnerability_scan.status == "passed"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["oci_image"].update(  # type: ignore[union-attr]
                {"pinned_reference": "ghcr.io/ufjmacca/ai-native-factory-runner:latest"}
            ),
            "pinned_reference",
        ),
        (
            lambda payload: payload["oci_image"].update(  # type: ignore[union-attr]
                {
                    "pinned_reference": (
                        "ghcr.io/ufjmacca/ai-native-factory-runner@sha256:" + ("a" * 64)
                    )
                }
            ),
            "digest",
        ),
        (
            lambda payload: payload["source"].update(  # type: ignore[union-attr]
                {"git_commit_sha": "main"}
            ),
            "git_commit_sha",
        ),
        (
            lambda payload: payload["compatibility"].update(  # type: ignore[union-attr]
                {"status": "failed"}
            ),
            "passed",
        ),
        (
            lambda payload: payload["supply_chain"].pop("provenance_url"),  # type: ignore[union-attr]
            "provenance_url",
        ),
        (
            lambda payload: payload["wheel"].update(  # type: ignore[union-attr]
                {"filename": "other-1.5.0-py3-none-any.whl"}
            ),
            "filename",
        ),
    ],
)
def test_release_receipt_rejects_incomplete_or_inconsistent_release(
    mutate,
    message: str,
) -> None:
    payload = copy.deepcopy(_receipt_payload())
    mutate(payload)

    with pytest.raises((ValidationError, ValueError), match=message):
        validate_release_receipt(payload)


def test_release_receipt_rejects_unknown_fields() -> None:
    payload = _receipt_payload()
    payload["manual_approval"] = True

    with pytest.raises(ValidationError, match="manual_approval"):
        validate_release_receipt(payload)

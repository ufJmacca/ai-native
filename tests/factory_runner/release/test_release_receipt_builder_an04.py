from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from ai_native.factory_runner.canonical import canonical_json_bytes
from ai_native.factory_runner.compatibility_report import (
    canonical_compatibility_report_bytes,
    compatibility_report_digest,
)
from ai_native.factory_runner.release_receipt import validate_release_receipt
from scripts import build_factory_runner_release_receipt as builder


SOURCE_COMMIT = "83e674f8161f38ef9bf4551e92bf655f278262c4"
VERSION = "1.5.0"
TAG = f"ai-native-base-v{VERSION}"
WHEEL_FILENAME = f"ai_native_base-{VERSION}-py3-none-any.whl"
IMAGE_REPOSITORY = "ghcr.io/ufjmacca/ai-native-factory-runner"
IMAGE_DIGEST = "sha256:" + ("2" * 64)
IMAGE_REFERENCE = f"{IMAGE_REPOSITORY}@{IMAGE_DIGEST}"
SCHEMA_SET_DIGEST = "sha256:" + ("3" * 64)
RELEASE_ROOT = f"https://github.com/ufJmacca/ai-native/releases/download/{TAG}"
RELEASED_AT = "2026-07-31T06:00:00Z"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _write_wheel(
    path: Path,
    *,
    schema_manifest: bytes,
) -> None:
    identity = {
        "schema": "factory-runner-build-identity/v1",
        "distribution": "ai-native-base",
        "version": VERSION,
        "source_repository": "ufJmacca/ai-native",
        "source_commit": SOURCE_COMMIT,
        "source_tag": TAG,
        "image": None,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": _digest(schema_manifest),
    }
    members = {
        "ai_native/factory_runner/_build_identity.json": (
            json.dumps(identity, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode(),
        "ai_native/schemas/factory_runner/v1/schema-manifest.json": schema_manifest,
        "ai_native/schemas/factory_runner/v1/schema-set.sha256": (
            f"{SCHEMA_SET_DIGEST}\n".encode()
        ),
        f"ai_native_base-{VERSION}.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: ai-native-base\nVersion: 1.5.0\n\n"
        ),
        f"ai_native_base-{VERSION}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: fixture\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _build_identity(*, image_reference: str | None) -> dict[str, object]:
    return {
        "schema": "factory-runner-build-identity/v1",
        "distribution": "ai-native-base",
        "version": VERSION,
        "source_repository": "ufJmacca/ai-native",
        "source_commit": SOURCE_COMMIT,
        "source_tag": TAG,
        "image": image_reference,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": _digest(_schema_manifest_bytes()),
    }


def _schema_manifest_bytes() -> bytes:
    return b'{"manifest_version":1,"protocol":"factory-runner-protocol/v1"}\n'


def _write_compatibility_report(
    path: Path,
    *,
    wheel_digest: str,
) -> bytes:
    artifacts = [
        {
            "kind": "source",
            "reference": f"ufJmacca/ai-native@{SOURCE_COMMIT}",
            "digest": None,
            "build_identity": _build_identity(image_reference=None),
        },
        {
            "kind": "wheel",
            "reference": WHEEL_FILENAME,
            "digest": wheel_digest,
            "build_identity": _build_identity(image_reference=None),
        },
        {
            "kind": "oci",
            "reference": IMAGE_REFERENCE,
            "digest": IMAGE_DIGEST,
            "build_identity": _build_identity(image_reference=IMAGE_REFERENCE),
        },
    ]
    fixtures = []
    for index, (fixture_id, operation, outcome) in enumerate(
        (
            ("author-success", "author", "succeeded"),
            ("author-no-change", "author", "no_change"),
            ("verify-success", "verify", "succeeded"),
        ),
        start=4,
    ):
        tree_digest = "sha256:" + str(index) * 64
        result_digest = "sha256:" + str(index + 1) * 64
        manifest_digest = "sha256:" + str(index + 2) * 64
        fixtures.append(
            {
                "fixture_id": fixture_id,
                "operation": operation,
                "expected_outcome": outcome,
                "status": "passed",
                "canonical_output_tree_digest": tree_digest,
                "results": [
                    {
                        "artifact": artifact,
                        "status": "passed",
                        "actual_outcome": outcome,
                        "run_result_digest": result_digest,
                        "output_manifest_digest": manifest_digest,
                        "output_tree_digest": tree_digest,
                    }
                    for artifact in ("source", "wheel", "oci")
                ],
            }
        )

    report: dict[str, object] = {
        "schema": "factory-runner-compatibility-report/v1",
        "protocol": "factory-runner-protocol/v1",
        "suite_version": "factory-runner-compatibility/v1",
        "generated_at": RELEASED_AT,
        "source_commit": SOURCE_COMMIT,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": _digest(_schema_manifest_bytes()),
        "artifacts": artifacts,
        "fixtures": fixtures,
        "status": "passed",
        "report_digest": "sha256:" + ("0" * 64),
    }
    report["report_digest"] = compatibility_report_digest(report)
    content = canonical_compatibility_report_bytes(report)
    path.write_bytes(content)
    return content


def _release_fixture(
    tmp_path: Path,
    *,
    report_wheel_digest: str | None = None,
) -> tuple[Path, dict[str, Path]]:
    artifact_dir = tmp_path / "release-assets"
    artifact_dir.mkdir()
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()

    manifest_path = schema_dir / "schema-manifest.json"
    manifest_path.write_bytes(_schema_manifest_bytes())
    schema_set_path = schema_dir / "schema-set.sha256"
    schema_set_path.write_text(f"{SCHEMA_SET_DIGEST}\n", encoding="ascii")

    wheel_path = artifact_dir / WHEEL_FILENAME
    _write_wheel(wheel_path, schema_manifest=_schema_manifest_bytes())
    wheel_digest = _digest(wheel_path.read_bytes())
    report_path = artifact_dir / "compatibility-report.json"
    _write_compatibility_report(
        report_path,
        wheel_digest=report_wheel_digest or wheel_digest,
    )

    evidence = {
        "sbom": artifact_dir / "factory-runner.spdx.json",
        "vulnerability_report": artifact_dir / "trivy-report.sarif",
        "provenance": artifact_dir / "provenance.intoto.jsonl",
    }
    evidence["sbom"].write_bytes(
        b'{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n'
    )
    evidence["vulnerability_report"].write_bytes(
        b'{"runs":[{"results":[]}],"version":"2.1.0"}\n'
    )
    evidence["provenance"].write_bytes(
        b'{"_type":"https://in-toto.io/Statement/v1","subject":[]}\n'
    )
    paths = {
        "wheel": wheel_path,
        "schema_set": schema_set_path,
        "schema_manifest": manifest_path,
        "compatibility_report": report_path,
        **evidence,
    }
    return artifact_dir, paths


def _arguments(artifact_dir: Path, paths: dict[str, Path]) -> list[str]:
    return [
        "--version",
        VERSION,
        "--tag",
        TAG,
        "--source-sha",
        SOURCE_COMMIT,
        "--released-at",
        RELEASED_AT,
        "--wheel",
        str(paths["wheel"]),
        "--oci-reference",
        IMAGE_REFERENCE,
        "--platform",
        "linux/arm64",
        "--platform",
        "linux/amd64",
        "--schema-set",
        str(paths["schema_set"]),
        "--schema-manifest",
        str(paths["schema_manifest"]),
        "--compatibility-report",
        str(paths["compatibility_report"]),
        "--sbom",
        str(paths["sbom"]),
        "--vulnerability-report",
        str(paths["vulnerability_report"]),
        "--vulnerability-scanner",
        "trivy",
        "--vulnerability-policy",
        "no-fixable-critical-or-high/v1",
        "--provenance",
        str(paths["provenance"]),
        "--attestation-url",
        "https://github.com/ufJmacca/ai-native/attestations/123456",
        "--output",
        str(artifact_dir / "factory-runner-release-receipt.json"),
    ]


def test_builder_hashes_actual_evidence_emits_canonical_receipt_and_verifies_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    artifact_dir, paths = _release_fixture(tmp_path)
    calls: list[tuple[bytes, Path]] = []
    real_verify = builder.verify_local_release

    def record_verification(receipt: bytes, local_assets: Path):
        calls.append((receipt, local_assets))
        return real_verify(receipt, local_assets)

    monkeypatch.setattr(builder, "verify_local_release", record_verification)

    assert builder.main(_arguments(artifact_dir, paths)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""

    receipt_path = artifact_dir / "factory-runner-release-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = validate_release_receipt(receipt_bytes)
    assert receipt_bytes == canonical_json_bytes(receipt.model_dump(mode="json"))
    assert receipt.released_at == RELEASED_AT
    assert receipt.source.git_commit_sha == SOURCE_COMMIT
    assert receipt.wheel.sha256 == _digest(paths["wheel"].read_bytes())
    assert receipt.wheel.download_url == f"{RELEASE_ROOT}/{WHEEL_FILENAME}"
    assert receipt.oci_image.pinned_reference == IMAGE_REFERENCE
    assert receipt.oci_image.platforms == ("linux/amd64", "linux/arm64")
    assert receipt.contracts.schema_set_digest == SCHEMA_SET_DIGEST
    assert receipt.contracts.schema_manifest_sha256 == _digest(
        paths["schema_manifest"].read_bytes()
    )
    assert receipt.compatibility.report_sha256 == _digest(
        paths["compatibility_report"].read_bytes()
    )
    assert receipt.supply_chain.sbom_sha256 == _digest(paths["sbom"].read_bytes())
    assert receipt.supply_chain.vulnerability_scan.report_sha256 == _digest(
        paths["vulnerability_report"].read_bytes()
    )
    assert receipt.supply_chain.provenance_sha256 == _digest(
        paths["provenance"].read_bytes()
    )
    assert calls == [(receipt_bytes, artifact_dir)]

    assert builder.main(_arguments(artifact_dir, paths)) == 0
    assert receipt_path.read_bytes() == receipt_bytes


def test_builder_fails_closed_when_report_certifies_another_wheel(
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir, paths = _release_fixture(
        tmp_path,
        report_wheel_digest="sha256:" + ("a" * 64),
    )
    receipt_path = artifact_dir / "factory-runner-release-receipt.json"

    assert builder.main(_arguments(artifact_dir, paths)) == 1

    captured = capsys.readouterr()
    assert "compatibility" in captured.err
    assert "Traceback" not in captured.err
    assert not receipt_path.exists()


def test_builder_requires_receipt_resolved_assets_in_the_output_directory(
    tmp_path: Path,
    capsys,
) -> None:
    artifact_dir, paths = _release_fixture(tmp_path)
    external_sbom = tmp_path / paths["sbom"].name
    paths["sbom"].replace(external_sbom)
    paths["sbom"] = external_sbom

    assert builder.main(_arguments(artifact_dir, paths)) == 1

    captured = capsys.readouterr()
    assert "output directory" in captured.err
    assert not (artifact_dir / "factory-runner-release-receipt.json").exists()

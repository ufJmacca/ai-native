from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from ai_native.factory_runner.compatibility_report import (
    FactoryRunnerCompatibilityReport,
    canonical_compatibility_report_bytes,
    compatibility_report_digest,
)
from ai_native.factory_runner.release_verification import (
    ReleaseVerificationError,
    main,
    verify_local_release,
)


SOURCE_COMMIT = "83e674f8161f38ef9bf4551e92bf655f278262c4"
VERSION = "1.5.0"
TAG = f"ai-native-base-v{VERSION}"
WHEEL_FILENAME = f"ai_native_base-{VERSION}-py3-none-any.whl"
RELEASE_ROOT = f"https://github.com/ufJmacca/ai-native/releases/download/{TAG}"
IMAGE_DIGEST = "sha256:" + ("2" * 64)
SCHEMA_SET_DIGEST = "sha256:" + ("3" * 64)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _write_wheel(
    path: Path,
    *,
    metadata_name: str = "ai-native-base",
    metadata_version: str = VERSION,
    identity_overrides: dict[str, object] | None = None,
    schema_set_bytes: bytes | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> None:
    manifest_bytes = b'{"manifest_version":1,"protocol":"factory-runner-protocol/v1"}\n'
    identity: dict[str, object] = {
        "schema": "factory-runner-build-identity/v1",
        "distribution": "ai-native-base",
        "version": VERSION,
        "source_repository": "ufJmacca/ai-native",
        "source_commit": SOURCE_COMMIT,
        "source_tag": TAG,
        "image": None,
        "schema_set_digest": SCHEMA_SET_DIGEST,
        "schema_manifest_sha256": _digest(manifest_bytes),
    }
    identity.update(identity_overrides or {})
    metadata = (
        f"Metadata-Version: 2.4\nName: {metadata_name}\nVersion: {metadata_version}\n\n"
    ).encode()
    members = {
        "ai_native/factory_runner/_build_identity.json": (
            json.dumps(identity, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode(),
        "ai_native/schemas/factory_runner/v1/schema-manifest.json": manifest_bytes,
        "ai_native/schemas/factory_runner/v1/schema-set.sha256": (
            schema_set_bytes
            if schema_set_bytes is not None
            else f"{SCHEMA_SET_DIGEST}\n".encode()
        ),
        f"ai_native_base-{VERSION}.dist-info/METADATA": metadata,
        f"ai_native_base-{VERSION}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: fixture\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    members.update(extra_members or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path, dict[str, bytes]]:
    artifact_dir = tmp_path / "release-assets"
    artifact_dir.mkdir(parents=True)
    wheel_path = artifact_dir / WHEEL_FILENAME
    _write_wheel(wheel_path)

    artifacts = {
        "compatibility-report.json": (
            b'{"report_schema":"fixture/v1","status":"passed"}\n'
        ),
        "factory-runner.spdx.json": (
            b'{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n'
        ),
        "trivy-report.sarif": (b'{"runs":[{"results":[]}],"version":"2.1.0"}\n'),
        "provenance.intoto.jsonl": (
            b'{"_type":"https://in-toto.io/Statement/v1","subject":[]}\n'
        ),
    }
    for name, content in artifacts.items():
        (artifact_dir / name).write_bytes(content)

    with zipfile.ZipFile(wheel_path) as archive:
        manifest_bytes = archive.read(
            "ai_native/schemas/factory_runner/v1/schema-manifest.json"
        )
    repository = "ghcr.io/ufjmacca/ai-native-factory-runner"
    payload: dict[str, object] = {
        "receipt_schema": "factory-runner-release-receipt/v1",
        "protocol": "factory-runner-protocol/v1",
        "released_at": "2026-07-31T06:00:00Z",
        "source": {
            "repository": "ufJmacca/ai-native",
            "git_commit_sha": SOURCE_COMMIT,
            "git_tag": TAG,
        },
        "wheel": {
            "distribution": "ai-native-base",
            "version": VERSION,
            "filename": WHEEL_FILENAME,
            "sha256": _digest(wheel_path.read_bytes()),
            "download_url": f"{RELEASE_ROOT}/{WHEEL_FILENAME}",
        },
        "oci_image": {
            "repository": repository,
            "digest": IMAGE_DIGEST,
            "pinned_reference": f"{repository}@{IMAGE_DIGEST}",
            "platforms": ["linux/amd64"],
        },
        "contracts": {
            "schema_set_digest": SCHEMA_SET_DIGEST,
            "schema_manifest_sha256": _digest(manifest_bytes),
        },
        "compatibility": {
            "suite_version": "factory-runner-compatibility/v1",
            "status": "passed",
            "report_url": f"{RELEASE_ROOT}/compatibility-report.json",
            "report_sha256": _digest(artifacts["compatibility-report.json"]),
        },
        "supply_chain": {
            "sbom_url": f"{RELEASE_ROOT}/factory-runner.spdx.json",
            "sbom_sha256": _digest(artifacts["factory-runner.spdx.json"]),
            "vulnerability_scan": {
                "scanner": "trivy",
                "policy": "no-fixable-critical-or-high/v1",
                "status": "passed",
                "report_url": f"{RELEASE_ROOT}/trivy-report.sarif",
                "report_sha256": _digest(artifacts["trivy-report.sarif"]),
            },
            "provenance_url": f"{RELEASE_ROOT}/provenance.intoto.jsonl",
            "provenance_sha256": _digest(artifacts["provenance.intoto.jsonl"]),
            "signature_reference": (
                "https://github.com/ufJmacca/ai-native/attestations/" + ("9" * 64)
            ),
        },
    }
    return payload, artifact_dir, artifacts


def _refresh_wheel_digest(payload: dict[str, object], artifact_dir: Path) -> None:
    wheel = payload["wheel"]
    assert isinstance(wheel, dict)
    wheel["sha256"] = _digest((artifact_dir / WHEEL_FILENAME).read_bytes())


def _write_bound_compatibility_report(
    payload: dict[str, object],
    artifact_dir: Path,
    *,
    source_commit: str = SOURCE_COMMIT,
    wheel_digest: str | None = None,
) -> bytes:
    wheel = payload["wheel"]
    image = payload["oci_image"]
    contracts = payload["contracts"]
    assert isinstance(wheel, dict)
    assert isinstance(image, dict)
    assert isinstance(contracts, dict)
    selected_wheel_digest = wheel_digest or str(wheel["sha256"])
    image_reference = str(image["pinned_reference"])

    def build_identity(*, image_reference_value: str | None) -> dict[str, object]:
        return {
            "schema": "factory-runner-build-identity/v1",
            "distribution": "ai-native-base",
            "version": VERSION,
            "source_repository": "ufJmacca/ai-native",
            "source_commit": source_commit,
            "source_tag": TAG,
            "image": image_reference_value,
            "schema_set_digest": contracts["schema_set_digest"],
            "schema_manifest_sha256": contracts["schema_manifest_sha256"],
        }

    artifacts: list[dict[str, object]] = [
        {
            "kind": "source",
            "reference": f"ufJmacca/ai-native@{source_commit}",
            "digest": None,
            "build_identity": build_identity(image_reference_value=None),
        },
        {
            "kind": "wheel",
            "reference": WHEEL_FILENAME,
            "digest": selected_wheel_digest,
            "build_identity": build_identity(image_reference_value=None),
        },
        {
            "kind": "oci",
            "reference": image_reference,
            "digest": image["digest"],
            "build_identity": build_identity(image_reference_value=image_reference),
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
        "generated_at": "2026-07-31T06:00:00Z",
        "source_commit": source_commit,
        "schema_set_digest": contracts["schema_set_digest"],
        "schema_manifest_sha256": contracts["schema_manifest_sha256"],
        "artifacts": artifacts,
        "fixtures": fixtures,
        "status": "passed",
        "report_digest": "sha256:" + "0" * 64,
    }
    report["report_digest"] = compatibility_report_digest(report)
    encoded = canonical_compatibility_report_bytes(report)
    (artifact_dir / "compatibility-report.json").write_bytes(encoded)
    compatibility = payload["compatibility"]
    assert isinstance(compatibility, dict)
    compatibility["report_sha256"] = _digest(encoded)
    return encoded


def test_offline_verifier_binds_all_local_bytes_and_wheel_identity(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, artifacts = _fixture(tmp_path)
    calls: list[bytes] = []

    def validate_report(report: bytes, receipt) -> object:
        calls.append(report)
        assert receipt.source.git_commit_sha == SOURCE_COMMIT
        return {"status": "passed"}

    verified = verify_local_release(
        payload,
        artifact_dir,
        compatibility_validator=validate_report,
    )

    assert tuple(artifact.role for artifact in verified.artifacts) == (
        "wheel",
        "compatibility_report",
        "sbom",
        "vulnerability_scan",
        "provenance",
    )
    assert calls == [artifacts["compatibility-report.json"]]
    assert verified.build_identity.source_commit == SOURCE_COMMIT
    assert verified.build_identity.source_tag == TAG
    assert verified.build_identity.version == VERSION
    assert verified.compatibility_report == {"status": "passed"}


def test_default_validator_binds_the_strict_compatibility_report(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    _write_bound_compatibility_report(payload, artifact_dir)

    verified = verify_local_release(payload, artifact_dir)

    assert isinstance(
        verified.compatibility_report,
        FactoryRunnerCompatibilityReport,
    )
    assert verified.compatibility_report.source_commit == SOURCE_COMMIT
    assert verified.compatibility_report.artifacts[1].digest == (
        verified.receipt.wheel.sha256
    )
    assert verified.compatibility_report.artifacts[2].reference == (
        verified.receipt.oci_image.pinned_reference
    )


@pytest.mark.parametrize(
    ("source_commit", "wheel_digest"),
    [
        ("a" * 40, None),
        (SOURCE_COMMIT, "sha256:" + "a" * 64),
    ],
)
def test_default_validator_rejects_report_that_certifies_another_release(
    tmp_path: Path,
    source_commit: str,
    wheel_digest: str | None,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    _write_bound_compatibility_report(
        payload,
        artifact_dir,
        source_commit=source_commit,
        wheel_digest=wheel_digest,
    )

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(payload, artifact_dir)

    assert exc_info.value.code == "compatibility_report_invalid"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (WHEEL_FILENAME, "digest_mismatch"),
        ("compatibility-report.json", "digest_mismatch"),
        ("factory-runner.spdx.json", "digest_mismatch"),
        ("trivy-report.sarif", "digest_mismatch"),
        ("provenance.intoto.jsonl", "digest_mismatch"),
    ],
)
def test_offline_verifier_rejects_tampered_release_assets(
    tmp_path: Path,
    name: str,
    code: str,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    with (artifact_dir / name).open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "name",
    [
        WHEEL_FILENAME,
        "compatibility-report.json",
        "factory-runner.spdx.json",
        "trivy-report.sarif",
        "provenance.intoto.jsonl",
    ],
)
def test_offline_verifier_rejects_missing_release_assets(
    tmp_path: Path,
    name: str,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    (artifact_dir / name).unlink()

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == "artifact_missing"


def test_offline_verifier_rejects_unsafe_or_aliased_artifact_names(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    compatibility = payload["compatibility"]
    assert isinstance(compatibility, dict)
    compatibility["report_url"] = f"{RELEASE_ROOT}/%2e%2e%2fcompatibility-report.json"

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == "unsafe_artifact_name"

    payload, artifact_dir, _artifacts = _fixture(tmp_path / "collision")
    compatibility = payload["compatibility"]
    supply_chain = payload["supply_chain"]
    assert isinstance(compatibility, dict)
    assert isinstance(supply_chain, dict)
    compatibility["report_url"] = supply_chain["sbom_url"]
    compatibility["report_sha256"] = supply_chain["sbom_sha256"]

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == "artifact_name_collision"


def test_offline_verifier_rejects_symlinked_artifacts(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    report_path = artifact_dir / "compatibility-report.json"
    target_path = artifact_dir / "report-target.json"
    report_path.rename(target_path)
    os.symlink(target_path.name, report_path)

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == "unsafe_artifact_file"


@pytest.mark.parametrize(
    ("wheel_kwargs", "code"),
    [
        ({"metadata_name": "different"}, "wheel_metadata_mismatch"),
        ({"metadata_version": "9.9.9"}, "wheel_metadata_mismatch"),
        (
            {"identity_overrides": {"source_commit": "a" * 40}},
            "build_identity_mismatch",
        ),
        (
            {"identity_overrides": {"version": "9.9.9"}},
            "build_identity_invalid",
        ),
        (
            {"identity_overrides": {"schema_set_digest": "sha256:" + "a" * 64}},
            "build_identity_mismatch",
        ),
        (
            {"schema_set_bytes": b"sha256:" + b"a" * 64 + b"\n"},
            "wheel_schema_mismatch",
        ),
        (
            {"extra_members": {"../outside": b"unsafe"}},
            "unsafe_wheel_member",
        ),
    ],
)
def test_offline_verifier_rejects_wrong_wheel_release_identity(
    tmp_path: Path,
    wheel_kwargs: dict[str, object],
    code: str,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    _write_wheel(artifact_dir / WHEEL_FILENAME, **wheel_kwargs)
    _refresh_wheel_digest(payload, artifact_dir)

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == code


def test_offline_verifier_fails_closed_when_report_binding_fails(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)

    def reject_report(_report: bytes, _receipt) -> None:
        raise ValueError("report source commit does not match receipt")

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=reject_report,
        )

    assert exc_info.value.code == "compatibility_report_invalid"
    assert "source commit" in str(exc_info.value)


def test_offline_verifier_maps_invalid_receipt_status_to_stable_failure(
    tmp_path: Path,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    compatibility = payload["compatibility"]
    assert isinstance(compatibility, dict)
    compatibility["status"] = "failed"

    with pytest.raises(ReleaseVerificationError) as exc_info:
        verify_local_release(
            payload,
            artifact_dir,
            compatibility_validator=lambda _report, _receipt: None,
        )

    assert exc_info.value.code == "invalid_receipt"


def test_standalone_cli_reports_missing_receipt_without_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main(
        [
            "--receipt",
            str(tmp_path / "missing-receipt.json"),
            "--artifact-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "receipt_unavailable" in captured.err
    assert "Traceback" not in captured.err


def test_standalone_cli_emits_a_machine_readable_verified_summary(
    tmp_path: Path,
    capsys,
) -> None:
    payload, artifact_dir, _artifacts = _fixture(tmp_path)
    _write_bound_compatibility_report(payload, artifact_dir)
    receipt_path = tmp_path / "factory-runner-release-receipt.json"
    receipt_path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--receipt",
            str(receipt_path),
            "--artifact-dir",
            str(artifact_dir),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    summary = json.loads(captured.out)
    assert summary["status"] == "passed"
    assert summary["source_commit"] == SOURCE_COMMIT
    assert summary["wheel_version"] == VERSION
    assert [item["role"] for item in summary["verified_artifacts"]] == [
        "wheel",
        "compatibility_report",
        "sbom",
        "vulnerability_scan",
        "provenance",
    ]

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/verify_factory_runner_release_receipt.py",
            "--receipt",
            str(receipt_path),
            "--artifact-dir",
            str(artifact_dir),
            "--json",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["source_commit"] == SOURCE_COMMIT

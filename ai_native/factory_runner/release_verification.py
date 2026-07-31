"""Offline verification of receipt-resolved factory-runner release artifacts."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any
from urllib.parse import urlsplit
import zipfile

from pydantic import ValidationError

from ai_native.factory_runner.build_identity import FactoryRunnerBuildIdentity
from ai_native.factory_runner.release_receipt import (
    FactoryRunnerReleaseReceipt,
    validate_release_receipt,
)


_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_MAX_WHEEL_BYTES = 512 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_MAX_WHEEL_ENTRIES = 20_000
_MAX_WHEEL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_BUILD_IDENTITY_BYTES = 1024 * 1024
_MAX_SCHEMA_ARTIFACT_BYTES = 16 * 1024 * 1024


class ReleaseVerificationError(ValueError):
    """A stable, safe-to-report offline verification failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


CompatibilityReportValidator = Callable[
    [bytes, FactoryRunnerReleaseReceipt],
    object,
]


@dataclass(frozen=True, slots=True)
class VerifiedReleaseArtifact:
    role: str
    name: str
    path: Path
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class VerifiedLocalRelease:
    receipt: FactoryRunnerReleaseReceipt
    artifacts: tuple[VerifiedReleaseArtifact, ...]
    build_identity: FactoryRunnerBuildIdentity
    compatibility_report: object


@dataclass(frozen=True, slots=True)
class _ArtifactExpectation:
    role: str
    name: str
    expected_digest: str
    maximum_bytes: int


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _normalised_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _release_asset_name(
    url: str,
    *,
    receipt: FactoryRunnerReleaseReceipt,
) -> str:
    parsed = urlsplit(url)
    expected_prefix = f"/ufJmacca/ai-native/releases/download/{receipt.source.git_tag}/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(expected_prefix)
    ):
        raise ReleaseVerificationError(
            "unsafe_artifact_name",
            "release artifact URL is outside the receipt's immutable release",
        )
    relative = parsed.path.removeprefix(expected_prefix)
    if (
        not relative
        or "/" in relative
        or "\\" in relative
        or "%" in relative
        or relative in {".", ".."}
        or _ASSET_NAME_PATTERN.fullmatch(relative) is None
    ):
        raise ReleaseVerificationError(
            "unsafe_artifact_name",
            "release artifact URL does not name one safe direct release asset",
        )
    return relative


def _validate_artifact_root(path: Path) -> Path:
    root = Path(path)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ReleaseVerificationError(
            "artifact_directory_unavailable",
            "local release artifact directory is unavailable",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseVerificationError(
            "unsafe_artifact_directory",
            "local release artifact root must be a real directory",
        )
    return root


def _read_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    missing_code: str = "artifact_missing",
    unsafe_code: str = "unsafe_artifact_file",
) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseVerificationError(
            missing_code,
            f"required local file is missing: {path.name}",
        ) from exc
    except OSError as exc:
        raise ReleaseVerificationError(
            unsafe_code,
            f"required local file is unavailable: {path.name}",
        ) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseVerificationError(
            unsafe_code,
            f"required local file is not a regular file: {path.name}",
        )
    if before.st_size > maximum_bytes:
        raise ReleaseVerificationError(
            unsafe_code,
            f"required local file exceeds its verification limit: {path.name}",
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseVerificationError(
            unsafe_code,
            f"required local file could not be opened safely: {path.name}",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ReleaseVerificationError(
                unsafe_code,
                f"required local file changed while being opened: {path.name}",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
        if (
            len(content) > maximum_bytes
            or len(content) != opened.st_size
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ReleaseVerificationError(
                unsafe_code,
                f"required local file changed while being read: {path.name}",
            )
        return content
    except OSError as exc:
        raise ReleaseVerificationError(
            unsafe_code,
            f"required local file could not be read safely: {path.name}",
        ) from exc
    finally:
        os.close(descriptor)


def _artifact_expectations(
    receipt: FactoryRunnerReleaseReceipt,
) -> tuple[_ArtifactExpectation, ...]:
    wheel_name = receipt.wheel.filename
    if (
        _ASSET_NAME_PATTERN.fullmatch(wheel_name) is None
        or PurePosixPath(wheel_name).name != wheel_name
    ):
        raise ReleaseVerificationError(
            "unsafe_artifact_name",
            "wheel filename is not a safe release asset name",
        )
    if _release_asset_name(receipt.wheel.download_url, receipt=receipt) != wheel_name:
        raise ReleaseVerificationError(
            "unsafe_artifact_name",
            "wheel URL and filename resolve to different release assets",
        )

    expectations = (
        _ArtifactExpectation(
            "wheel",
            wheel_name,
            receipt.wheel.sha256,
            _MAX_WHEEL_BYTES,
        ),
        _ArtifactExpectation(
            "compatibility_report",
            _release_asset_name(receipt.compatibility.report_url, receipt=receipt),
            receipt.compatibility.report_sha256,
            _MAX_EVIDENCE_BYTES,
        ),
        _ArtifactExpectation(
            "sbom",
            _release_asset_name(receipt.supply_chain.sbom_url, receipt=receipt),
            receipt.supply_chain.sbom_sha256,
            _MAX_EVIDENCE_BYTES,
        ),
        _ArtifactExpectation(
            "vulnerability_scan",
            _release_asset_name(
                receipt.supply_chain.vulnerability_scan.report_url,
                receipt=receipt,
            ),
            receipt.supply_chain.vulnerability_scan.report_sha256,
            _MAX_EVIDENCE_BYTES,
        ),
        _ArtifactExpectation(
            "provenance",
            _release_asset_name(
                receipt.supply_chain.provenance_url,
                receipt=receipt,
            ),
            receipt.supply_chain.provenance_sha256,
            _MAX_EVIDENCE_BYTES,
        ),
    )
    names = tuple(item.name for item in expectations)
    if len(set(names)) != len(names):
        raise ReleaseVerificationError(
            "artifact_name_collision",
            "receipt assigns more than one release role to the same local asset",
        )
    return expectations


def _read_and_verify_artifacts(
    root: Path,
    expectations: Sequence[_ArtifactExpectation],
) -> tuple[tuple[VerifiedReleaseArtifact, ...], dict[str, bytes]]:
    verified: list[VerifiedReleaseArtifact] = []
    contents: dict[str, bytes] = {}
    for expectation in expectations:
        path = root / expectation.name
        content = _read_regular_file(
            path,
            maximum_bytes=expectation.maximum_bytes,
        )
        actual_digest = _sha256(content)
        if not hmac.compare_digest(actual_digest, expectation.expected_digest):
            raise ReleaseVerificationError(
                "digest_mismatch",
                f"{expectation.role} bytes do not match the receipt digest",
            )
        verified.append(
            VerifiedReleaseArtifact(
                role=expectation.role,
                name=expectation.name,
                path=path,
                sha256=actual_digest,
                byte_size=len(content),
            )
        )
        if expectation.role in {"wheel", "compatibility_report"}:
            contents[expectation.role] = content
    return tuple(verified), contents


def _safe_wheel_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    parts = PurePosixPath(name).parts
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ReleaseVerificationError(
            "unsafe_wheel_member",
            "wheel contains an unsafe archive member name",
        )
    if info.flag_bits & 0x1:
        raise ReleaseVerificationError(
            "unsafe_wheel_member",
            "wheel contains an encrypted archive member",
        )
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ReleaseVerificationError(
            "unsafe_wheel_member",
            "wheel contains a symbolic link or special archive member",
        )


def _read_wheel_member(
    archive: zipfile.ZipFile,
    member: str,
    *,
    maximum_bytes: int,
    missing_code: str,
) -> bytes:
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise ReleaseVerificationError(
            missing_code,
            f"wheel is missing required member: {member}",
        ) from exc
    if info.file_size > maximum_bytes:
        raise ReleaseVerificationError(
            "unsafe_wheel_member",
            f"wheel member exceeds its verification limit: {member}",
        )
    try:
        content = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ReleaseVerificationError(
            "invalid_wheel",
            f"wheel member cannot be read: {member}",
        ) from exc
    if len(content) != info.file_size:
        raise ReleaseVerificationError(
            "invalid_wheel",
            f"wheel member size does not match its archive record: {member}",
        )
    return content


def _inspect_wheel(
    content: bytes,
    receipt: FactoryRunnerReleaseReceipt,
) -> FactoryRunnerBuildIdentity:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        entries = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseVerificationError(
            "invalid_wheel",
            "released wheel is not a valid ZIP archive",
        ) from exc

    with archive:
        if not entries or len(entries) > _MAX_WHEEL_ENTRIES:
            raise ReleaseVerificationError(
                "invalid_wheel",
                "released wheel has an invalid archive inventory",
            )
        names = tuple(info.filename for info in entries)
        if len(set(names)) != len(names):
            raise ReleaseVerificationError(
                "invalid_wheel",
                "released wheel contains duplicate archive members",
            )
        total_uncompressed = 0
        for info in entries:
            _safe_wheel_member(info)
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_WHEEL_UNCOMPRESSED_BYTES:
                raise ReleaseVerificationError(
                    "invalid_wheel",
                    "released wheel exceeds the uncompressed verification limit",
                )

        normalised_distribution = receipt.wheel.distribution.replace("-", "_")
        dist_info_root = f"{normalised_distribution}-{receipt.wheel.version}.dist-info"
        metadata_path = f"{dist_info_root}/METADATA"
        metadata_members = tuple(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if metadata_members != (metadata_path,):
            raise ReleaseVerificationError(
                "wheel_metadata_mismatch",
                "wheel must contain exactly the expected distribution METADATA",
            )
        metadata_bytes = _read_wheel_member(
            archive,
            metadata_path,
            maximum_bytes=_MAX_METADATA_BYTES,
            missing_code="wheel_metadata_mismatch",
        )
        try:
            metadata = BytesParser(policy=policy.default).parsebytes(metadata_bytes)
        except (TypeError, ValueError) as exc:
            raise ReleaseVerificationError(
                "wheel_metadata_mismatch",
                "wheel METADATA is malformed",
            ) from exc
        names_in_metadata = metadata.get_all("Name", [])
        versions_in_metadata = metadata.get_all("Version", [])
        if (
            len(names_in_metadata) != 1
            or _normalised_distribution(names_in_metadata[0])
            != _normalised_distribution(receipt.wheel.distribution)
            or versions_in_metadata != [receipt.wheel.version]
        ):
            raise ReleaseVerificationError(
                "wheel_metadata_mismatch",
                "wheel METADATA name or version does not match the receipt",
            )

        identity_bytes = _read_wheel_member(
            archive,
            "ai_native/factory_runner/_build_identity.json",
            maximum_bytes=_MAX_BUILD_IDENTITY_BYTES,
            missing_code="build_identity_missing",
        )
        try:
            identity = FactoryRunnerBuildIdentity.model_validate_json(identity_bytes)
        except (ValidationError, ValueError) as exc:
            raise ReleaseVerificationError(
                "build_identity_invalid",
                "wheel build identity is invalid",
            ) from exc

        schema_set_bytes = _read_wheel_member(
            archive,
            "ai_native/schemas/factory_runner/v1/schema-set.sha256",
            maximum_bytes=_MAX_SCHEMA_ARTIFACT_BYTES,
            missing_code="wheel_schema_mismatch",
        )
        manifest_bytes = _read_wheel_member(
            archive,
            "ai_native/schemas/factory_runner/v1/schema-manifest.json",
            maximum_bytes=_MAX_SCHEMA_ARTIFACT_BYTES,
            missing_code="wheel_schema_mismatch",
        )

    try:
        embedded_schema_set = schema_set_bytes.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ReleaseVerificationError(
            "wheel_schema_mismatch",
            "wheel schema-set digest is not ASCII",
        ) from exc
    if (
        embedded_schema_set != receipt.contracts.schema_set_digest
        or _sha256(manifest_bytes) != receipt.contracts.schema_manifest_sha256
    ):
        raise ReleaseVerificationError(
            "wheel_schema_mismatch",
            "wheel schema artifacts do not match the receipt",
        )

    expected_identity = {
        "distribution": receipt.wheel.distribution,
        "version": receipt.wheel.version,
        "source_repository": receipt.source.repository,
        "source_commit": receipt.source.git_commit_sha,
        "source_tag": receipt.source.git_tag,
        "image": None,
        "schema_set_digest": receipt.contracts.schema_set_digest,
        "schema_manifest_sha256": receipt.contracts.schema_manifest_sha256,
    }
    actual_identity = identity.model_dump(mode="json", by_alias=False)
    for field_name, expected in expected_identity.items():
        if actual_identity[field_name] != expected:
            raise ReleaseVerificationError(
                "build_identity_mismatch",
                f"wheel build identity {field_name} does not match the receipt",
            )
    return identity


def _default_compatibility_validator(
    content: bytes,
    receipt: FactoryRunnerReleaseReceipt,
) -> object:
    try:
        from ai_native.factory_runner.compatibility_report import (
            validate_compatibility_report,
        )
    except ImportError as exc:
        raise ReleaseVerificationError(
            "compatibility_validator_unavailable",
            "compatibility report validator is unavailable",
        ) from exc

    report = validate_compatibility_report(content)
    if (
        report.protocol != receipt.protocol
        or report.suite_version != receipt.compatibility.suite_version
        or report.status != receipt.compatibility.status
        or report.source_commit != receipt.source.git_commit_sha
        or report.schema_set_digest != receipt.contracts.schema_set_digest
        or report.schema_manifest_sha256 != receipt.contracts.schema_manifest_sha256
    ):
        raise ReleaseVerificationError(
            "compatibility_report_invalid",
            "compatibility report identity does not match the release receipt",
        )

    source, wheel, image = report.artifacts
    expected_source = f"{receipt.source.repository}@{receipt.source.git_commit_sha}"
    if (
        source.kind != "source"
        or source.reference != expected_source
        or source.digest is not None
        or wheel.kind != "wheel"
        or wheel.reference != receipt.wheel.filename
        or wheel.digest != receipt.wheel.sha256
        or image.kind != "oci"
        or image.reference != receipt.oci_image.pinned_reference
        or image.digest != receipt.oci_image.digest
    ):
        raise ReleaseVerificationError(
            "compatibility_report_invalid",
            "compatibility report artifact binding does not match the receipt",
        )

    for artifact in report.artifacts:
        identity = artifact.build_identity
        if (
            identity.distribution != receipt.wheel.distribution
            or identity.version != receipt.wheel.version
            or identity.source_repository != receipt.source.repository
            or identity.source_commit != receipt.source.git_commit_sha
            or identity.source_tag != receipt.source.git_tag
            or identity.schema_set_digest != receipt.contracts.schema_set_digest
            or identity.schema_manifest_sha256
            != receipt.contracts.schema_manifest_sha256
        ):
            raise ReleaseVerificationError(
                "compatibility_report_invalid",
                "compatibility report build identity does not match the receipt",
            )
    return report


def _validate_compatibility(
    content: bytes,
    receipt: FactoryRunnerReleaseReceipt,
    validator: CompatibilityReportValidator | None,
) -> object:
    selected = validator or _default_compatibility_validator
    try:
        return selected(content, receipt)
    except ReleaseVerificationError:
        raise
    except Exception as exc:
        message = str(exc).strip() or "compatibility report binding failed"
        raise ReleaseVerificationError(
            "compatibility_report_invalid",
            message[:1024],
        ) from exc


def verify_local_release(
    receipt_value: (
        FactoryRunnerReleaseReceipt | Mapping[str, Any] | str | bytes | bytearray
    ),
    artifact_dir: Path | str,
    *,
    compatibility_validator: CompatibilityReportValidator | None = None,
) -> VerifiedLocalRelease:
    """Verify a receipt and all of its already-downloaded release assets."""

    try:
        receipt = validate_release_receipt(receipt_value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ReleaseVerificationError(
            "invalid_receipt",
            "release receipt failed structural validation",
        ) from exc
    root = _validate_artifact_root(Path(artifact_dir))
    expectations = _artifact_expectations(receipt)
    artifacts, contents = _read_and_verify_artifacts(root, expectations)
    build_identity = _inspect_wheel(contents["wheel"], receipt)
    compatibility_report = _validate_compatibility(
        contents["compatibility_report"],
        receipt,
        compatibility_validator,
    )
    return VerifiedLocalRelease(
        receipt=receipt,
        artifacts=artifacts,
        build_identity=build_identity,
        compatibility_report=compatibility_report,
    )


def _receipt_bytes(path: Path) -> bytes:
    return _read_regular_file(
        path,
        maximum_bytes=4 * 1024 * 1024,
        missing_code="receipt_unavailable",
        unsafe_code="receipt_unavailable",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a factory-runner release receipt against local, "
            "receipt-resolved artifacts without network access."
        )
    )
    parser.add_argument(
        "--receipt",
        required=True,
        type=Path,
        help="path to factory-runner-release-receipt.json",
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        type=Path,
        help="directory containing the downloaded release assets",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a compact machine-readable success summary",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt_content = _receipt_bytes(args.receipt)
        verified = verify_local_release(receipt_content, args.artifact_dir)
    except ReleaseVerificationError as exc:
        print(
            f"release verification failed [{exc.code}]: {exc}",
            file=sys.stderr,
        )
        return 1

    summary = {
        "protocol": verified.receipt.protocol,
        "receipt_schema": verified.receipt.receipt_schema,
        "source_commit": verified.receipt.source.git_commit_sha,
        "status": "passed",
        "verified_artifacts": [
            {
                "name": artifact.name,
                "role": artifact.role,
                "sha256": artifact.sha256,
            }
            for artifact in verified.artifacts
        ],
        "wheel_version": verified.receipt.wheel.version,
    }
    if args.json:
        print(
            json.dumps(
                summary,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            "verified factory-runner release "
            f"{verified.receipt.source.git_tag} at "
            f"{verified.receipt.source.git_commit_sha}"
        )
    return 0


__all__ = [
    "CompatibilityReportValidator",
    "ReleaseVerificationError",
    "VerifiedLocalRelease",
    "VerifiedReleaseArtifact",
    "main",
    "verify_local_release",
]


if __name__ == "__main__":
    raise SystemExit(main())

"""Build and locally verify one canonical factory-runner release receipt."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import re
import stat
import sys

from pydantic import ValidationError

from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.compatibility_report import (
    validate_compatibility_report,
)
from ai_native.factory_runner.release_receipt import validate_release_receipt
from ai_native.factory_runner.release_verification import (
    ReleaseVerificationError,
    verify_local_release,
)


_OUTPUT_FILENAME = "factory-runner-release-receipt.json"
_IMAGE_REPOSITORY = "ghcr.io/ufjmacca/ai-native-factory-runner"
_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_MAX_WHEEL_BYTES = 512 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_MAX_SCHEMA_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024


class ReceiptBuildError(ValueError):
    """A safe-to-report deterministic receipt construction failure."""


def _read_regular_file(
    path: Path,
    *,
    role: str,
    maximum_bytes: int,
) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ReceiptBuildError(f"{role} is missing") from exc
    except OSError as exc:
        raise ReceiptBuildError(f"{role} is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReceiptBuildError(f"{role} must be a real regular file")
    if before.st_size > maximum_bytes:
        raise ReceiptBuildError(f"{role} exceeds its permitted size")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReceiptBuildError(f"{role} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ReceiptBuildError(f"{role} changed while being opened")
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
            raise ReceiptBuildError(f"{role} changed while being read")
        return content
    except OSError as exc:
        raise ReceiptBuildError(f"{role} could not be read safely") from exc
    finally:
        os.close(descriptor)


def _validate_output_root(output: Path) -> Path:
    if output.name != _OUTPUT_FILENAME:
        raise ReceiptBuildError(f"output filename must be {_OUTPUT_FILENAME}")
    root = output.parent
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ReceiptBuildError("output directory is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReceiptBuildError("output directory must be a real directory")
    return root


def _release_asset(
    path: Path,
    *,
    root: Path,
    role: str,
    maximum_bytes: int,
) -> tuple[str, bytes]:
    name = path.name
    if (
        name == _OUTPUT_FILENAME
        or _ASSET_NAME_PATTERN.fullmatch(name) is None
        or name in {".", ".."}
    ):
        raise ReceiptBuildError(f"{role} does not have a safe release asset name")
    try:
        supplied_parent = path.parent.resolve(strict=True)
        output_root = root.resolve(strict=True)
    except OSError as exc:
        raise ReceiptBuildError(f"{role} output directory is unavailable") from exc
    if supplied_parent != output_root:
        raise ReceiptBuildError(f"{role} must be in the output directory")

    canonical_path = root / name
    return (
        name,
        _read_regular_file(
            canonical_path,
            role=role,
            maximum_bytes=maximum_bytes,
        ),
    )


def _schema_set_digest(content: bytes) -> str:
    try:
        decoded = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReceiptBuildError("schema set digest must be ASCII") from exc
    digest = decoded.removesuffix("\n")
    if decoded not in {digest, digest + "\n"}:
        raise ReceiptBuildError(
            "schema set digest must contain one digest and an optional newline"
        )
    return digest


def _parse_image_reference(reference: str) -> tuple[str, str]:
    repository, separator, digest = reference.rpartition("@")
    if not separator or repository != _IMAGE_REPOSITORY or not digest:
        raise ReceiptBuildError(
            "OCI reference must use the factory-runner repository and a digest"
        )
    return repository, digest


def _release_url(tag: str, asset_name: str) -> str:
    return f"https://github.com/ufJmacca/ai-native/releases/download/{tag}/{asset_name}"


def _write_new_or_identical(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        existing = _read_regular_file(
            path,
            role="existing release receipt",
            maximum_bytes=_MAX_RECEIPT_BYTES,
        )
        if existing != content:
            raise ReceiptBuildError(
                "output already exists with different receipt bytes"
            )
        return
    except OSError as exc:
        raise ReceiptBuildError("release receipt could not be created") from exc

    completed = False
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        completed = True
    except OSError as exc:
        raise ReceiptBuildError("release receipt could not be written") from exc
    finally:
        os.close(descriptor)
        if not completed:
            try:
                path.unlink()
            except OSError:
                pass

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        directory_descriptor = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise ReceiptBuildError(
            "release receipt output directory could not be opened"
        ) from exc
    try:
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise ReceiptBuildError(
            "release receipt output directory could not be synchronized"
        ) from exc
    finally:
        os.close(directory_descriptor)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a canonical factory-runner release receipt from actual local "
            "release evidence, then deep-verify the receipt-resolved asset set."
        )
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--released-at", required=True)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument(
        "--oci-reference",
        "--oci-ref",
        required=True,
        dest="oci_reference",
        help="digest-pinned factory-runner OCI reference",
    )
    parser.add_argument(
        "--platform",
        required=True,
        action="append",
        dest="platforms",
        help="published OCI platform; repeat for each platform",
    )
    parser.add_argument("--schema-set", required=True, type=Path)
    parser.add_argument("--schema-manifest", required=True, type=Path)
    parser.add_argument("--compatibility-report", required=True, type=Path)
    parser.add_argument("--sbom", required=True, type=Path)
    parser.add_argument("--vulnerability-report", required=True, type=Path)
    parser.add_argument("--vulnerability-scanner", required=True)
    parser.add_argument("--vulnerability-policy", required=True)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--attestation-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _build_receipt(args: argparse.Namespace) -> tuple[bytes, Path]:
    root = _validate_output_root(args.output)
    wheel_name, wheel = _release_asset(
        args.wheel,
        root=root,
        role="wheel",
        maximum_bytes=_MAX_WHEEL_BYTES,
    )
    report_name, compatibility_report = _release_asset(
        args.compatibility_report,
        root=root,
        role="compatibility report",
        maximum_bytes=_MAX_EVIDENCE_BYTES,
    )
    sbom_name, sbom = _release_asset(
        args.sbom,
        root=root,
        role="SBOM",
        maximum_bytes=_MAX_EVIDENCE_BYTES,
    )
    vulnerability_name, vulnerability_report = _release_asset(
        args.vulnerability_report,
        root=root,
        role="vulnerability report",
        maximum_bytes=_MAX_EVIDENCE_BYTES,
    )
    provenance_name, provenance = _release_asset(
        args.provenance,
        root=root,
        role="provenance",
        maximum_bytes=_MAX_EVIDENCE_BYTES,
    )
    asset_names = (
        wheel_name,
        report_name,
        sbom_name,
        vulnerability_name,
        provenance_name,
    )
    if len(set(asset_names)) != len(asset_names):
        raise ReceiptBuildError("release evidence asset names must be unique")

    schema_set = _schema_set_digest(
        _read_regular_file(
            args.schema_set,
            role="schema set digest",
            maximum_bytes=_MAX_SCHEMA_BYTES,
        )
    )
    schema_manifest = _read_regular_file(
        args.schema_manifest,
        role="schema manifest",
        maximum_bytes=_MAX_SCHEMA_BYTES,
    )
    validate_compatibility_report(compatibility_report)
    image_repository, image_digest = _parse_image_reference(args.oci_reference)
    platforms = tuple(sorted(set(args.platforms)))

    receipt = validate_release_receipt(
        {
            "receipt_schema": "factory-runner-release-receipt/v1",
            "protocol": "factory-runner-protocol/v1",
            "released_at": args.released_at,
            "source": {
                "repository": "ufJmacca/ai-native",
                "git_commit_sha": args.source_sha,
                "git_tag": args.tag,
            },
            "wheel": {
                "distribution": "ai-native-base",
                "version": args.version,
                "filename": wheel_name,
                "sha256": sha256_digest(wheel),
                "download_url": _release_url(args.tag, wheel_name),
            },
            "oci_image": {
                "repository": image_repository,
                "digest": image_digest,
                "pinned_reference": args.oci_reference,
                "platforms": platforms,
            },
            "contracts": {
                "schema_set_digest": schema_set,
                "schema_manifest_sha256": sha256_digest(schema_manifest),
            },
            "compatibility": {
                "suite_version": "factory-runner-compatibility/v1",
                "status": "passed",
                "report_url": _release_url(args.tag, report_name),
                "report_sha256": sha256_digest(compatibility_report),
            },
            "supply_chain": {
                "sbom_url": _release_url(args.tag, sbom_name),
                "sbom_sha256": sha256_digest(sbom),
                "vulnerability_scan": {
                    "scanner": args.vulnerability_scanner,
                    "policy": args.vulnerability_policy,
                    "status": "passed",
                    "report_url": _release_url(args.tag, vulnerability_name),
                    "report_sha256": sha256_digest(vulnerability_report),
                },
                "provenance_url": _release_url(args.tag, provenance_name),
                "provenance_sha256": sha256_digest(provenance),
                "signature_reference": args.attestation_url,
            },
        }
    )
    return canonical_json_bytes(receipt.model_dump(mode="json")), root


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt_bytes, artifact_root = _build_receipt(args)
        verify_local_release(receipt_bytes, artifact_root)
        _write_new_or_identical(args.output, receipt_bytes)
    except (
        OSError,
        ReceiptBuildError,
        ReleaseVerificationError,
        TypeError,
        ValidationError,
        ValueError,
    ) as exc:
        message = str(exc).strip() or "receipt construction failed"
        print(f"release receipt build failed: {message}", file=sys.stderr)
        return 1

    print(f"wrote and verified {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

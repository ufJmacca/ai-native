from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest


CANARY_ID = "attempt-gateway"
CANARY = b"anf_canary_DO_NOT_PERSIST_7b9e4d31"


@pytest.fixture
def redaction_api() -> ModuleType:
    try:
        return importlib.import_module("ai_native.factory_runner.redaction")
    except ModuleNotFoundError:
        pytest.fail("AN-03 redaction API is not implemented", pytrace=False)


def _scanner(redaction_api: ModuleType):
    policy = redaction_api.SecretPolicy(
        exact_canaries=((CANARY_ID, CANARY),),
    )
    return redaction_api.SecretScanner(policy)


def _writer(
    redaction_api: ModuleType,
    root: Path,
    *,
    max_artifact_bytes: int = 64,
    max_total_bytes: int = 128,
):
    from ai_native.factory_runner.outputs import OutputWriter

    return OutputWriter(
        root,
        secret_scanner=_scanner(redaction_api),
        max_artifact_bytes=max_artifact_bytes,
        max_total_bytes=max_total_bytes,
    )


def test_exact_canary_is_detected_across_stream_chunks(
    redaction_api: ModuleType,
) -> None:
    scanner = _scanner(redaction_api)

    with pytest.raises(redaction_api.SecretDetectedError):
        scanner.require_clean_chunks((b"prefix-" + CANARY[:13], CANARY[13:] + b"-suffix"))


def test_human_text_redaction_is_deterministic_and_rescans_clean(
    redaction_api: ModuleType,
) -> None:
    scanner = _scanner(redaction_api)
    contaminated = f"gateway said {CANARY.decode()} twice {CANARY.decode()}"

    first = scanner.redact_text(contaminated)
    second = scanner.redact_text(contaminated)

    assert first == second
    assert first == (
        f"gateway said [REDACTED:{CANARY_ID}] twice [REDACTED:{CANARY_ID}]"
    )
    assert CANARY not in first.encode()
    scanner.require_clean_chunks((first.encode(),))


@pytest.mark.parametrize(
    "payload",
    (
        b"semantic-prefix-" + CANARY + b"-suffix",
        b"\x00\xff\x10" + CANARY + b"\x00\x80\xfe",
    ),
    ids=("text-semantic-bytes", "binary-semantic-bytes"),
)
def test_semantic_and_binary_bytes_fail_closed(
    redaction_api: ModuleType,
    payload: bytes,
) -> None:
    scanner = _scanner(redaction_api)

    with pytest.raises(redaction_api.SecretDetectedError):
        scanner.require_clean_chunks((payload,))


def test_output_writer_enforces_per_artifact_and_aggregate_limits(
    redaction_api: ModuleType,
    tmp_path: Path,
) -> None:
    writer = _writer(
        redaction_api,
        tmp_path,
        max_artifact_bytes=8,
        max_total_bytes=12,
    )

    writer.write_bytes("objects/one.bin", b"12345678")
    with pytest.raises(ValueError, match="artifact.*size|artifact.*limit"):
        writer.write_bytes("objects/too-large.bin", b"123456789")
    writer.write_bytes("objects/two.bin", b"1234")
    with pytest.raises(ValueError, match="total.*size|total.*limit"):
        writer.write_bytes("objects/over-total.bin", b"x")

    assert not (tmp_path / "objects/too-large.bin").exists()
    assert not (tmp_path / "objects/over-total.bin").exists()


def test_output_writer_refuses_unsafe_paths_links_and_special_files(
    redaction_api: ModuleType,
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    os.mkfifo(tmp_path / "fifo")
    writer = _writer(redaction_api, tmp_path)

    for unsafe_path in ("../escape", "/absolute", "linked/escape", "fifo"):
        with pytest.raises(ValueError):
            writer.write_bytes(unsafe_path, b"safe")

    assert not (outside / "escape").exists()


def test_output_writer_never_leaves_canary_in_durable_bytes(
    redaction_api: ModuleType,
    tmp_path: Path,
) -> None:
    writer = _writer(redaction_api, tmp_path)

    with pytest.raises(redaction_api.SecretDetectedError):
        writer.write_bytes("evidence/contaminated.bin", b"before" + CANARY + b"after")

    durable_files = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    assert all(CANARY not in path.read_bytes() for path in durable_files)
    assert not (tmp_path / "evidence/contaminated.bin").exists()

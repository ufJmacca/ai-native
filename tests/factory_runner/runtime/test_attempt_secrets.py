from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

from ai_native.factory_runner.attempt_secrets import (
    AttemptSecretSourceError,
    build_attempt_secret_scanner,
)
from ai_native.factory_runner.redaction import SecretDetectedError


@pytest.mark.parametrize(
    "credential_key",
    (
        "SERVICE_TOKEN",
        "SERVICE_SECRET",
        "SERVICE_CREDENTIAL",
        "SERVICE_PASSWORD",
        "SERVICE_API_KEY",
        "SERVICE_ACCESS_KEY",
        "SERVICE_PRIVATE_KEY",
    ),
)
def test_credential_like_direct_values_become_exact_canaries(
    credential_key: str,
) -> None:
    secret = f"direct-value-for-{credential_key}".encode()
    scanner = build_attempt_secret_scanner(
        {
            "LANG": "C.UTF-8",
            credential_key: secret.decode(),
        }
    )

    with pytest.raises(SecretDetectedError):
        scanner.require_clean_chunks(
            (
                b"prefix-" + secret[:7],
                secret[7:] + b"-suffix",
            )
        )

    scanner.require_clean_chunks((b"C.UTF-8",))


@pytest.mark.parametrize(
    "file_key",
    (
        "ATTEMPT_GATEWAY_TOKEN_FILE",
        "SERVICE_SECRET_FILE",
        "SERVICE_CREDENTIAL_FILE",
    ),
)
def test_credential_file_contents_become_exact_canaries(
    tmp_path: Path,
    file_key: str,
) -> None:
    secret = b"credential-file-value-with-newline"
    source = tmp_path / "credential-source"
    source.write_bytes(secret + b"\n")

    scanner = build_attempt_secret_scanner(
        {
            "LANG": "C.UTF-8",
            file_key: str(source),
        }
    )

    with pytest.raises(SecretDetectedError):
        scanner.require_clean_chunks((secret[:11], secret[11:]))
    scanner.require_clean_chunks((str(source).encode(),))


def test_labels_are_generic_deterministic_and_values_are_deduplicated(
    tmp_path: Path,
) -> None:
    secret = "shared-attempt-credential"
    source = tmp_path / "sensitive-token-name"
    source.write_text(secret, encoding="utf-8")
    environment = {
        "ZZZ_TOKEN": secret,
        "AAA_SECRET_FILE": str(source),
        "SAFE_VALUE": secret,
    }

    first = build_attempt_secret_scanner(environment)
    second = build_attempt_secret_scanner(dict(reversed(tuple(environment.items()))))

    assert first.policy.exact_canaries == second.policy.exact_canaries
    assert len(first.policy.exact_canaries) == 1
    identifier, value = first.policy.exact_canaries[0]
    assert re.fullmatch(r"attempt-secret-[0-9]{4}", identifier)
    assert value == secret.encode()
    assert all(
        sensitive not in identifier
        for sensitive in (
            "ZZZ_TOKEN",
            "AAA_SECRET_FILE",
            source.name,
            secret,
        )
    )


def test_builtin_canary_policy_remains_enabled_and_cross_chunk() -> None:
    detection = b"FACTORY_SECRET_CANARY_attempt_builtin"
    scanner = build_attempt_secret_scanner({"LANG": "C.UTF-8"})

    with pytest.raises(SecretDetectedError) as caught:
        scanner.require_clean_chunks((b"prefix-" + detection[:17], detection[17:]))

    assert caught.value.identifier == "secret-canary"


@pytest.mark.parametrize(
    "damage",
    (
        "relative",
        "missing",
        "symlink",
        "parent-symlink",
        "directory",
        "fifo",
        "oversize",
        "unreadable",
    ),
)
def test_unsafe_credential_files_fail_closed_without_sensitive_diagnostics(
    tmp_path: Path,
    damage: str,
) -> None:
    secret = b"credential-file-must-never-appear-in-errors"
    source = tmp_path / "private-gateway-token"
    if damage == "relative":
        configured_path = Path(source.name)
    elif damage == "missing":
        configured_path = source
    elif damage == "symlink":
        target = tmp_path / "outside-secret"
        target.write_bytes(secret)
        source.symlink_to(target)
        configured_path = source
    elif damage == "parent-symlink":
        target_directory = tmp_path / "outside-directory"
        target_directory.mkdir()
        (target_directory / source.name).write_bytes(secret)
        linked_directory = tmp_path / "linked-directory"
        linked_directory.symlink_to(target_directory, target_is_directory=True)
        configured_path = linked_directory / source.name
    elif damage == "directory":
        source.mkdir()
        configured_path = source
    elif damage == "fifo":
        os.mkfifo(source)
        configured_path = source
    elif damage == "oversize":
        source.write_bytes(b"x" * ((64 * 1024) + 1))
        configured_path = source
    else:
        source.write_bytes(secret)
        source.chmod(0)
        configured_path = source

    environment = {"ATTEMPT_GATEWAY_TOKEN_FILE": str(configured_path)}
    with pytest.raises(AttemptSecretSourceError) as caught:
        build_attempt_secret_scanner(environment)

    diagnostic = str(caught.value)
    assert "ATTEMPT_GATEWAY_TOKEN_FILE" not in diagnostic
    assert str(configured_path) not in diagnostic
    assert configured_path.name not in diagnostic
    assert secret.decode() not in diagnostic


def test_oversized_direct_secret_fails_without_exposing_it() -> None:
    secret = "z" * ((64 * 1024) + 1)

    with pytest.raises(AttemptSecretSourceError) as caught:
        build_attempt_secret_scanner({"SERVICE_TOKEN": secret})

    assert secret not in str(caught.value)

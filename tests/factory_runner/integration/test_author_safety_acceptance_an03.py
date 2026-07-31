from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import stat
import subprocess
import sys
from typing import Literal

import pytest

from tests.factory_runner.integration._support import (
    FactoryInvocation,
    REPOSITORY_ROOT,
    assert_valid_completion,
    factory_command,
    factory_environment,
    git_status,
    load_valid_result,
)


SAFETY_AGENT = Path(__file__).with_name("_author_safety_agent.py")
ATTEMPT_CANARY = "attempt-credential-DO-NOT-PERSIST-4f31a90d"
SafetyMode = Literal[
    "author",
    "hardlink",
    "invalid-mode",
    "secret-binary-repository",
    "secret-model-output",
    "secret-repository",
    "special-file",
    "symlink",
]


def _write_policy(
    invocation: FactoryInvocation,
    *,
    commands: list[list[str]] | None = None,
    allowed_environment_keys: list[str] | None = None,
) -> None:
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    if commands is not None:
        payload["policy"]["allowed_commands"] = commands
    if allowed_environment_keys is not None:
        payload["policy"]["allowed_environment_keys"] = allowed_environment_keys
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safety_environment(
    invocation: FactoryInvocation,
    *,
    mode: SafetyMode,
    additions: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = factory_environment(invocation, agent_mode="fail-if-called")
    environment["AINATIVE_FACTORY_AGENT_COMMAND_JSON"] = json.dumps(
        [
            sys.executable,
            str(SAFETY_AGENT),
            "--mode",
            mode,
            "--marker",
            str(invocation.marker_path),
        ]
    )
    environment.update(additions or {})
    return environment


def _invoke(
    invocation: FactoryInvocation,
    *,
    mode: SafetyMode,
    additions: dict[str, str] | None = None,
    timeout: float = 45,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        factory_command(invocation),
        cwd=REPOSITORY_ROOT,
        env=_safety_environment(
            invocation,
            mode=mode,
            additions=additions,
        ),
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _events(invocation: FactoryInvocation) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (invocation.output_dir / "events.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _assert_policy_denied(
    invocation: FactoryInvocation,
    completed: subprocess.CompletedProcess[str],
) -> None:
    assert completed.returncode == 3, completed.stderr
    result = load_valid_result(invocation)
    assert result.outcome == "policy_denied"
    assert result.reason_code == "policy_denied"
    assert result.change_set is None


def _assert_not_persisted(invocation: FactoryInvocation, canary: bytes) -> None:
    for path in invocation.output_dir.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            assert canary not in path.read_bytes(), path


def test_partial_secret_admission_failure_emits_no_unscanned_identity(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    attempt_id = payload["identity"]["attempt_id"]

    completed = _invoke(
        invocation,
        mode="author",
        additions={
            "SERVICE_TOKEN": attempt_id,
            "ZZZ_TOKEN_FILE": str((tmp_path / "missing-token").resolve()),
        },
    )

    assert completed.returncode == 3, completed.stderr
    assert not invocation.output_dir.exists()
    assert attempt_id not in completed.stdout
    assert attempt_id not in completed.stderr


def test_protocol_identity_secret_is_policy_denied_not_invalid_output(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    attempt_id = payload["identity"]["attempt_id"]

    completed = _invoke(
        invocation,
        mode="author",
        additions={"SERVICE_TOKEN": attempt_id},
    )

    assert completed.returncode == 3, completed.stderr
    assert invocation.output_dir.is_dir()
    assert tuple(invocation.output_dir.iterdir()) == ()
    assert attempt_id not in completed.stdout
    assert attempt_id not in completed.stderr


def test_admission_failure_cannot_serialize_unscanned_identity_secret(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    attempt_id = 'attempt-credential-with-"quoted"-identity'
    payload["identity"]["attempt_id"] = attempt_id
    payload["context"]["expected_digest"] = "sha256:" + ("0" * 64)
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed = _invoke(
        invocation,
        mode="author",
        additions={"SERVICE_TOKEN": attempt_id},
    )

    assert completed.returncode == 3, completed.stderr
    if invocation.output_dir.exists():
        assert tuple(invocation.output_dir.iterdir()) == ()
    assert attempt_id not in completed.stdout
    assert attempt_id not in completed.stderr


@pytest.mark.parametrize(
    ("source", "expected_classification"),
    (
        ("def broken(:\n    pass\n", "syntax_error"),
        (
            "import sys; "
            "sys.stderr.write('ERROR collecting tests/test_target.py\\n'); "
            "raise SystemExit(1)",
            "collection_error",
        ),
        ("import definitely_missing_factory_dependency", "dependency_error"),
        ("raise SystemExit('unrelated failure')", "unrelated_failure"),
    ),
    ids=("syntax", "collection", "dependency", "unrelated"),
)
def test_false_red_is_rejected_before_authoring(
    factory_invocation: Callable[..., FactoryInvocation],
    source: str,
    expected_classification: str,
) -> None:
    invocation = factory_invocation(operation="author")
    _write_policy(
        invocation,
        commands=[[sys.executable, "-B", "-c", source]],
    )

    completed = _invoke(invocation, mode="author")

    _assert_policy_denied(invocation, completed)
    assert not invocation.marker_path.exists()
    assert git_status(invocation.workspace) == ""
    completions = [
        event for event in _events(invocation) if event["event_type"] == "TestCompleted"
    ]
    assert completions[-1]["sanitised_payload"]["phase"] == "red"
    assert (
        completions[-1]["sanitised_payload"]["failure_classification"]
        == expected_classification
    )


@pytest.mark.parametrize(
    ("mode", "verification_source"),
    (
        (
            "symlink",
            "from pathlib import Path; assert Path('app.py').is_symlink()",
        ),
        (
            "special-file",
            "from pathlib import Path; import stat; "
            "assert stat.S_ISFIFO(Path('app.py').lstat().st_mode)",
        ),
        (
            "invalid-mode",
            "from pathlib import Path; import stat; "
            "assert stat.S_IMODE(Path('app.py').stat().st_mode) == 0o600",
        ),
        (
            "hardlink",
            "from app import greeting; assert greeting('Codex') == 'Hello, Codex!'",
        ),
    ),
    ids=("symlink", "special-file", "invalid-mode", "hardlink"),
)
def test_author_change_must_be_a_supported_regular_file(
    factory_invocation: Callable[..., FactoryInvocation],
    mode: SafetyMode,
    verification_source: str,
) -> None:
    invocation = factory_invocation(operation="author")
    _write_policy(
        invocation,
        commands=[[sys.executable, "-B", "-c", verification_source]],
    )

    completed = _invoke(invocation, mode=mode)

    _assert_policy_denied(invocation, completed)
    result = load_valid_result(invocation)
    reference = result.latest_checkpoint
    assert reference is not None
    checkpoint = json.loads(
        (invocation.output_dir / reference.path).read_text(encoding="utf-8")
    )
    assert checkpoint["workspace_patch_digest"] is None
    assert tuple(checkpoint["completed_stages"]) == result.completed_stages
    if mode == "hardlink":
        outside = invocation.marker_path.with_name("outside-authored-app.py")
        assert outside.read_text(encoding="utf-8") == (
            'def greeting(name: str) -> str:\n    return f"Hello, {name}!"\n'
        )


def test_protocol_output_total_limit_is_enforced(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    behavior_command = [
        sys.executable,
        "-B",
        "-c",
        "from app import greeting; assert greeting('Codex') == 'Hello, Codex!'",
    ]
    output_commands = [
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; "
            "sys.stdout.buffer.write(b'x' * 1048576); "
            f"sys.stderr.buffer.write(b'y' * 1048576)  # {index}",
        ]
        for index in range(1, 12)
    ]
    _write_policy(
        invocation,
        commands=[behavior_command, *output_commands],
    )

    completed = _invoke(invocation, mode="author")

    _assert_policy_denied(invocation, completed)
    durable_files = [
        path
        for path in invocation.output_dir.rglob("*")
        if stat.S_ISREG(path.lstat().st_mode)
    ]
    assert sum(path.stat().st_size for path in durable_files) <= 64 * 1024 * 1024
    assert all(path.stat().st_size <= 16 * 1024 * 1024 for path in durable_files)
    result = load_valid_result(invocation)
    assert_valid_completion(invocation, result)


@pytest.mark.parametrize(
    ("source_kind", "sink_kind"),
    (
        ("direct", "command-output"),
        ("direct", "model-output"),
        ("direct", "repository"),
        ("direct", "binary-repository"),
        ("file", "model-output"),
        ("file", "repository"),
    ),
)
def test_attempt_secret_never_reaches_durable_output(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
    source_kind: str,
    sink_kind: str,
) -> None:
    invocation = factory_invocation(operation="author")
    additions: dict[str, str]
    allowed_environment_keys: list[str]
    if source_kind == "direct":
        additions = {"SERVICE_TOKEN": ATTEMPT_CANARY}
        allowed_environment_keys = ["SERVICE_TOKEN"]
    else:
        source = tmp_path / f"{sink_kind}-credential"
        source.write_text(ATTEMPT_CANARY + "\n", encoding="utf-8")
        additions = {"ATTEMPT_GATEWAY_TOKEN_FILE": str(source.resolve())}
        allowed_environment_keys = []

    mode: SafetyMode
    if sink_kind == "model-output":
        mode = "secret-model-output"
    elif sink_kind == "binary-repository":
        mode = "secret-binary-repository"
    elif sink_kind == "repository":
        mode = "secret-repository"
    else:
        mode = "author"

    commands: list[list[str]] | None = None
    if sink_kind == "command-output":
        commands = [
            [
                sys.executable,
                "-B",
                "-c",
                "import os; print(os.environ['SERVICE_TOKEN']); "
                "from app import greeting; "
                "assert greeting('Codex') == 'Hello, Codex!'",
            ]
        ]
    _write_policy(
        invocation,
        commands=commands,
        allowed_environment_keys=allowed_environment_keys,
    )

    completed = _invoke(
        invocation,
        mode=mode,
        additions=additions,
    )

    _assert_policy_denied(invocation, completed)
    assert ATTEMPT_CANARY not in completed.stdout
    assert ATTEMPT_CANARY not in completed.stderr
    _assert_not_persisted(invocation, ATTEMPT_CANARY.encode())


def test_gateway_only_credential_is_absent_during_deterministic_commands(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
) -> None:
    invocation = factory_invocation(operation="author")
    source = tmp_path / "gateway-only-credential"
    source.write_text(ATTEMPT_CANARY + "\n", encoding="utf-8")
    marker = tmp_path / "deterministic-command-stole-credential"
    command = (
        "from pathlib import Path; import os; "
        "root = Path(os.environ['HOME']).parent; "
        "matches = tuple(root.rglob('credential-*')); "
        f"Path({str(marker)!r}).write_bytes(matches[0].read_bytes()) "
        "if matches else None; "
        "from app import greeting; "
        "assert greeting('Codex') == 'Hello, Codex!'"
    )
    _write_policy(
        invocation,
        commands=[[sys.executable, "-B", "-c", command]],
        allowed_environment_keys=[],
    )

    completed = _invoke(
        invocation,
        mode="author",
        additions={
            "ATTEMPT_GATEWAY_TOKEN_FILE": str(source.resolve()),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()

from __future__ import annotations

from collections.abc import Callable
import json
import sys

import pytest

from tests.factory_runner.integration._support import (
    AgentMode,
    FactoryInvocation,
    invoke_factory,
    load_valid_change_set,
    load_valid_result,
)


def _configure_change(
    invocation: FactoryInvocation,
    *,
    allowed_paths: list[str],
    verification_source: str,
) -> None:
    payload = json.loads(invocation.run_spec_path.read_text(encoding="utf-8"))
    payload["policy"]["allowed_paths"] = allowed_paths
    payload["policy"]["allowed_commands"] = [
        [sys.executable, "-B", "-c", verification_source]
    ]
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("agent_mode", "allowed_paths", "verification_source", "expected"),
    [
        (
            "author-add",
            ["added.txt"],
            "from pathlib import Path; "
            "assert Path('added.txt').exists() and "
            "Path('added.txt').read_text().splitlines() == "
            "['factory addition']",
            [("add", "added.txt", None, "100644", False)],
        ),
        (
            "author-delete",
            ["app.py"],
            "from pathlib import Path; assert not Path('app.py').exists()",
            [("delete", "app.py", "100644", None, False)],
        ),
        (
            "author-rename",
            ["app.py", "renamed.py"],
            "from pathlib import Path; assert not Path('app.py').exists(); "
            "assert Path('renamed.py').exists()",
            [("rename", "renamed.py", "100644", "100644", False)],
        ),
        (
            "author-binary",
            ["app.py"],
            "from pathlib import Path; assert Path('app.py').read_bytes() == "
            "b'\\x00factory-binary\\xff\\n'",
            [("modify", "app.py", "100644", "100644", True)],
        ),
        (
            "author-mode",
            ["app.py"],
            "from pathlib import Path; import stat; "
            "assert Path('app.py').stat().st_mode & stat.S_IXUSR",
            [("modify", "app.py", "100644", "100755", False)],
        ),
    ],
    ids=["add", "delete", "rename", "binary", "mode"],
)
def test_factory_author_emits_complete_deterministic_changed_file_manifest(
    factory_invocation: Callable[..., FactoryInvocation],
    agent_mode: AgentMode,
    allowed_paths: list[str],
    verification_source: str,
    expected: list[tuple[str, str, str | None, str | None, bool]],
) -> None:
    invocation = factory_invocation(operation="author")
    _configure_change(
        invocation,
        allowed_paths=allowed_paths,
        verification_source=verification_source,
    )

    completed = invoke_factory(invocation, agent_mode=agent_mode)

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    change_set = load_valid_change_set(invocation, result)
    observed = [
        (
            changed.operation,
            changed.path,
            changed.previous_mode,
            changed.resulting_mode,
            changed.binary,
        )
        for changed in change_set.changed_files
    ]
    assert observed == expected
    if expected[0][0] == "rename":
        assert change_set.changed_files[0].previous_path == "app.py"
    patch = (invocation.output_dir / change_set.patch.path).read_bytes()
    assert patch
    assert b".git/" not in patch


def test_factory_author_reports_no_change_without_an_empty_changeset(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    invocation = factory_invocation(operation="author")
    _configure_change(
        invocation,
        allowed_paths=["app.py"],
        verification_source="raise SystemExit(0)",
    )

    completed = invoke_factory(invocation, agent_mode="author-no-change")

    assert completed.returncode == 0, completed.stderr
    result = load_valid_result(invocation)
    assert result.outcome == "no_change"
    assert result.change_set is None
    assert not (invocation.output_dir / "changeset" / "change.patch").exists()


def test_identical_worktree_state_has_identical_patch_and_manifest_digest(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    observations: list[tuple[bytes, str]] = []
    for _ in range(2):
        invocation = factory_invocation(operation="author")
        completed = invoke_factory(invocation, agent_mode="author")
        assert completed.returncode == 0, completed.stderr
        result = load_valid_result(invocation)
        change_set = load_valid_change_set(invocation, result)
        observations.append(
            (
                (invocation.output_dir / change_set.patch.path).read_bytes(),
                change_set.diff_digest,
            )
        )

    assert observations[0] == observations[1]


def test_secret_in_repository_change_is_never_persisted_to_protocol_output(
    factory_invocation: Callable[..., FactoryInvocation],
) -> None:
    canary = b"FACTORY_SECRET_CANARY_AN03_8f4d1c7e"
    invocation = factory_invocation(operation="author")
    _configure_change(
        invocation,
        allowed_paths=["app.py"],
        verification_source="from pathlib import Path; "
        "assert 'FACTORY_SECRET_CANARY_AN03_8f4d1c7e' in "
        "Path('app.py').read_text()",
    )

    completed = invoke_factory(invocation, agent_mode="author-secret")

    assert completed.returncode == 3, completed.stderr
    for path in invocation.output_dir.rglob("*"):
        if path.is_file():
            assert canary not in path.read_bytes(), path

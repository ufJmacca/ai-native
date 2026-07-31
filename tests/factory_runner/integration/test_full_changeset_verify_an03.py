from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys

import pytest

from tests.factory_runner.integration._support import (
    AgentMode,
    FactoryInvocation,
    invoke_factory,
    load_valid_change_set,
    load_valid_result,
    load_valid_verification_evidence,
    prepare_clean_verification_from_author,
)


def _configure_author(
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


FULL_CHANGE_CASES = [
    (
        "author-add",
        ["added.txt"],
        "from pathlib import Path; "
        "assert Path('added.txt').read_text().splitlines() == "
        "['factory addition']",
    ),
    (
        "author-delete",
        ["app.py"],
        "from pathlib import Path; assert not Path('app.py').exists()",
    ),
    (
        "author-rename",
        ["app.py", "renamed.py"],
        "from pathlib import Path; assert not Path('app.py').exists(); "
        "assert Path('renamed.py').exists()",
    ),
    (
        "author-binary",
        ["app.py"],
        "from pathlib import Path; assert Path('app.py').read_bytes() == "
        "b'\\x00factory-binary\\xff\\n'",
    ),
    (
        "author-mode",
        ["app.py"],
        "from pathlib import Path; import stat; "
        "assert Path('app.py').stat().st_mode & stat.S_IXUSR",
    ),
]


@pytest.mark.parametrize(
    ("agent_mode", "allowed_paths", "verification_source"),
    FULL_CHANGE_CASES,
    ids=["add", "delete", "rename", "binary", "mode"],
)
def test_full_author_changeset_passes_in_fresh_clean_verification(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
    agent_mode: AgentMode,
    allowed_paths: list[str],
    verification_source: str,
) -> None:
    author = factory_invocation(operation="author")
    _configure_author(
        author,
        allowed_paths=allowed_paths,
        verification_source=verification_source,
    )
    authored = invoke_factory(author, agent_mode=agent_mode)
    assert authored.returncode == 0, authored.stderr
    author_result = load_valid_result(author)
    change_set = load_valid_change_set(author, author_result)

    verify = prepare_clean_verification_from_author(
        tmp_path / f"verify-{agent_mode}",
        author_invocation=author,
        author_result=author_result,
    )
    assert verify.workspace != author.workspace
    verified = invoke_factory(verify, agent_mode="fail-if-called")

    assert verified.returncode == 0, verified.stderr
    assert not verify.marker_path.exists()
    result = load_valid_result(verify)
    assert result.operation == "verify"
    assert result.outcome == "succeeded"
    assert result.change_set is None
    evidence = load_valid_verification_evidence(verify, result)
    assert evidence.environment_kind == "clean_verification"
    assert evidence.change_set_digest == change_set.change_set_digest
    assert evidence.overall_status == "passed"
    assert not (verify.output_dir / "changeset").exists()


@pytest.mark.parametrize("mutation", ["extra-path", "content-mismatch"])
def test_clean_verification_rejects_unexpected_prepared_state(
    factory_invocation: Callable[..., FactoryInvocation],
    tmp_path: Path,
    mutation: str,
) -> None:
    author = factory_invocation(operation="author")
    _configure_author(
        author,
        allowed_paths=["added.txt"],
        verification_source="from pathlib import Path; "
        "assert Path('added.txt').read_text().splitlines() == "
        "['factory addition']",
    )
    authored = invoke_factory(author, agent_mode="author-add")
    assert authored.returncode == 0, authored.stderr
    author_result = load_valid_result(author)
    verify = prepare_clean_verification_from_author(
        tmp_path / f"verify-{mutation}",
        author_invocation=author,
        author_result=author_result,
    )
    if mutation == "extra-path":
        (verify.workspace / "unexpected.txt").write_text(
            "not declared\n",
            encoding="utf-8",
        )
    else:
        (verify.workspace / "added.txt").write_text(
            "different bytes\n",
            encoding="utf-8",
        )

    completed = invoke_factory(verify, agent_mode="fail-if-called")

    assert completed.returncode == 3, completed.stderr
    assert not verify.marker_path.exists()
    result = load_valid_result(verify)
    assert result.outcome == "policy_denied"
    assert result.change_set is None
    assert result.verification_evidence is None
    assert not (verify.output_dir / "changeset").exists()

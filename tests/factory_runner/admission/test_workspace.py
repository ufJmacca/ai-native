from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import ModuleType
from typing import Any

import pytest

from ai_native.factory_runner.git_runtime import FactoryGitRuntime
from ai_native.factory_runner.process import (
    CancellationToken,
    Deadline,
    FactoryProcessRunner,
)
from ai_native.factory_runner.protocol import contract_document_digest, sha256_digest
from tests.factory_runner.admission._fixtures import (
    AdmissionCase,
    assert_read_only,
)
from tests.factory_runner.admission.conftest import admit


def validate_workspace(admission_api: ModuleType, inputs: Any) -> object:
    deadline = Deadline.from_timeout(30)
    runtime = FactoryGitRuntime(
        workspace=inputs.workspace,
        output_dir=inputs.output_dir,
        environment={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_EXTERNAL_DIFF": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
        process_runner=FactoryProcessRunner(
            cancellation_token=CancellationToken(),
            deadline=deadline,
        ),
        deadline=deadline,
    )
    return admission_api.validate_workspace(inputs, git_runtime=runtime)


def assert_workspace_rejected(
    admission_api: ModuleType,
    case: AdmissionCase,
    reason_code: str,
) -> None:
    inputs = admit(admission_api, case)

    def reject() -> None:
        with pytest.raises(admission_api.FactoryAdmissionError) as captured:
            validate_workspace(admission_api, inputs)
        assert captured.value.reason_code == reason_code

    assert_read_only(case.root, reject)


def test_author_workspace_accepts_exact_clean_repository_and_head(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    inputs = admit(admission_api, admission_case)

    assert_read_only(
        admission_case.root,
        lambda: validate_workspace(admission_api, inputs),
    )


def test_workspace_path_must_be_exact_repository_root(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.set_workspace_path(admission_case.workspace / "src")

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


def test_workspace_path_must_be_a_git_repository(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    non_repository = admission_case.root / "not-a-repository"
    non_repository.mkdir()
    admission_case.set_workspace_path(non_repository)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


def test_linked_worktree_with_external_git_metadata_is_rejected(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    linked_workspace = admission_case.root / "linked-worktree"
    subprocess.run(
        (
            "git",
            "-C",
            str(admission_case.workspace),
            "worktree",
            "add",
            "--detach",
            str(linked_workspace),
            admission_case.base_commit_sha,
        ),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    assert (linked_workspace / ".git").is_file()
    admission_case.set_workspace_path(Path(linked_workspace))

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_author_head_must_equal_declared_base(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.set_declared_base("b" * 40)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "invalid_input",
    )


def test_workspace_with_a_configured_git_remote_is_rejected(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    remote = admission_case.root / "remote.git"
    subprocess.run(("git", "init", "--bare", str(remote)), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(admission_case.workspace),
            "remote",
            "add",
            "origin",
            str(remote),
        ),
        check=True,
    )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_workspace_with_external_git_config_include_is_rejected(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    external_config = admission_case.root / "external.gitconfig"
    external_config.write_text("[alias]\n  escaped = status\n", encoding="utf-8")
    with (admission_case.workspace / ".git" / "config").open(
        "a",
        encoding="utf-8",
    ) as local_config:
        local_config.write(f"\n[include]\n  path = {external_config}\n")

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_workspace_with_external_git_object_alternates_is_rejected(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    alternates = admission_case.workspace / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
        str(admission_case.root / "external-objects") + "\n",
        encoding="utf-8",
    )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_author_workspace_must_be_clean(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
    dirty_kind: str,
) -> None:
    if dirty_kind == "tracked":
        (admission_case.workspace / "src" / "app.py").write_text(
            "dirty tracked content\n",
            encoding="utf-8",
        )
    else:
        (admission_case.workspace / "untracked.txt").write_text(
            "dirty untracked content\n",
            encoding="utf-8",
        )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_author_rejects_symlinks_in_writable_path_trees(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    outside = admission_case.root / "outside"
    outside.mkdir()
    linked_path = admission_case.workspace / "src" / "outside-link"
    linked_path.symlink_to(outside, target_is_directory=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(admission_case.workspace),
            "add",
            "src/outside-link",
        ),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(admission_case.workspace),
            "commit",
            "-m",
            "tracked symlink fixture",
        ),
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ("git", "-C", str(admission_case.workspace), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    admission_case.set_declared_base(base)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_author_rejects_hard_links_in_writable_path_trees(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    source = admission_case.workspace / "src" / "app.py"
    outside_alias = admission_case.root / "outside-app-alias.py"
    os.link(source, outside_alias)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )
    assert outside_alias.read_bytes() == source.read_bytes()


def test_verify_accepts_a_prepared_uncommitted_change(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    inputs = admit(admission_api, admission_case)

    assert_read_only(
        admission_case.root,
        lambda: validate_workspace(admission_api, inputs),
    )


def test_verify_rejects_a_workspace_without_prepared_changes(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=False)

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_verify_rejects_content_that_does_not_match_the_change_set(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    (admission_case.workspace / "src" / "app.py").write_text(
        "different prepared content\n",
        encoding="utf-8",
    )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_verify_rejects_extra_workspace_changes(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    (admission_case.workspace / "extra.txt").write_text(
        "undeclared prepared content\n",
        encoding="utf-8",
    )

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_verify_change_set_paths_must_be_allowed_by_run_policy(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    admission_case.run_spec["policy"]["allowed_paths"] = ["tests/**"]
    admission_case.write_run_spec()

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )


def test_verify_patch_bytes_must_encode_the_prepared_change(
    admission_api: ModuleType,
    admission_case: AdmissionCase,
) -> None:
    admission_case.prepare_verification(apply_change=True)
    change_set_path = Path(
        admission_case.run_spec["verification_input"]["change_set_path"]
    )
    change_set = json.loads(change_set_path.read_text(encoding="utf-8"))
    unrelated_patch = b"unrelated but self-consistently referenced patch\n"
    patch_path = admission_case.run_spec_path.parent / change_set["patch"]["path"]
    patch_path.write_bytes(unrelated_patch)
    change_set["patch"]["byte_size"] = len(unrelated_patch)
    change_set["patch"]["digest"] = sha256_digest(unrelated_patch)
    change_set["change_set_digest"] = "sha256:" + ("0" * 64)
    change_set["change_set_digest"] = contract_document_digest(change_set)
    change_set_path.write_text(
        json.dumps(change_set, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    admission_case.run_spec["verification_input"]["expected_digest"] = change_set[
        "change_set_digest"
    ]
    admission_case.write_run_spec()

    assert_workspace_rejected(
        admission_api,
        admission_case,
        "policy_denied",
    )

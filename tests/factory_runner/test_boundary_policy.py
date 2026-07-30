from __future__ import annotations

from dataclasses import FrozenInstanceError
import subprocess
import sys

import pytest


def test_factory_mode_permits_execution_but_denies_publication_and_interaction() -> (
    None
):
    from ai_native.factory_runner.policy import (
        DEFAULT_FACTORY_MODE_CAPABILITIES,
        ExecutionCapability,
        ExecutionMode,
    )

    policy = DEFAULT_FACTORY_MODE_CAPABILITIES

    assert policy.mode is ExecutionMode.FACTORY
    assert ExecutionMode.LEGACY is not ExecutionMode.FACTORY
    assert policy.permits(ExecutionCapability.AUTHOR)
    assert policy.permits(ExecutionCapability.VERIFY)
    assert not policy.permits(ExecutionCapability.COMMIT)
    assert not policy.permits(ExecutionCapability.PULL_REQUEST)
    assert not policy.permits(ExecutionCapability.PUSH)
    assert not policy.permits(ExecutionCapability.MERGE)
    assert not policy.permits(ExecutionCapability.INTERACTIVE_INPUT)


def test_factory_mode_boundary_is_immutable() -> None:
    from ai_native.factory_runner.policy import (
        DEFAULT_FACTORY_MODE_CAPABILITIES,
        ExecutionCapability,
        FactoryModeCapabilities,
    )

    with pytest.raises((AttributeError, FrozenInstanceError)):
        DEFAULT_FACTORY_MODE_CAPABILITIES.allowed = frozenset()  # type: ignore[misc]

    with pytest.raises(TypeError):
        FactoryModeCapabilities(  # type: ignore[call-arg]
            allowed=frozenset({ExecutionCapability.COMMIT})
        )


def test_factory_mode_excludes_legacy_publication_stages() -> None:
    from ai_native.factory_runner.policy import DEFAULT_FACTORY_MODE_CAPABILITIES
    from ai_native.stages.capabilities import PUBLICATION_STAGES

    assert DEFAULT_FACTORY_MODE_CAPABILITIES.allowed_stages.isdisjoint(
        PUBLICATION_STAGES
    )
    assert DEFAULT_FACTORY_MODE_CAPABILITIES.permits_stage("verify")
    assert not DEFAULT_FACTORY_MODE_CAPABILITIES.permits_stage("commit")
    assert not DEFAULT_FACTORY_MODE_CAPABILITIES.permits_stage("pr")


def test_factory_author_import_does_not_load_publication_modules() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import sys; "
                "import ai_native.factory_runner.author; "
                "assert 'ai_native.stages.git_pr' not in sys.modules; "
                "assert 'ai_native.gitops' not in sys.modules"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_factory_cli_import_does_not_load_legacy_orchestrator() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import sys; "
                "import ai_native.cli; "
                "assert 'ai_native.orchestrator' not in sys.modules; "
                "assert 'ai_native.stages.git_pr' not in sys.modules; "
                "assert 'ai_native.gitops' not in sys.modules"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

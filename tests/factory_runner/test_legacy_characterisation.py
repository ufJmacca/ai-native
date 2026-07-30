from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_native.cli import _discover_config_path, build_parser, main
from ai_native.config import AppConfig
from ai_native.orchestrator import WorkflowOrchestrator
from ai_native.stages import ORDERED_STAGES
from ai_native.stages.capabilities import (
    CLI_STAGE_CHOICES,
    LEGACY_ORDERED_STAGES,
    PRE_SLICE_STAGES,
    SLICE_PIPELINE_STAGES,
)
from ai_native.stages.git_pr import commit_run, create_prs


LEGACY_COMMANDS = {
    "doctor",
    "init",
    "run",
    "stage",
    "loop",
    "verify",
    "commit",
    "review",
    "runs",
    "pr",
    "telemetry",
}


def test_legacy_top_level_help_and_version_remain_available(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ainative", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for command in LEGACY_COMMANDS:
        assert command in output


@pytest.mark.parametrize(
    "arguments,command",
    [
        (["run", "--spec", "feature.md"], "run"),
        (["stage", "--stage", "plan", "--spec", "feature.md"], "stage"),
        (["loop", "--spec", "feature.md"], "loop"),
        (["verify", "--spec", "feature.md"], "verify"),
        (["commit", "--spec", "feature.md"], "commit"),
        (["pr", "--spec", "feature.md"], "pr"),
    ],
)
def test_representative_legacy_commands_still_parse(arguments: list[str], command: str) -> None:
    args = build_parser().parse_args(arguments)

    assert args.command == command
    assert callable(args.func)


def test_configuration_discovery_and_default_run_paths_are_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "target"
    nested = repo_root / "packages" / "app"
    nested.mkdir(parents=True)
    config_path = repo_root / "ainative.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    assert _discover_config_path() == config_path.resolve()

    config = AppConfig.load(config_path)
    assert config.resolve_artifacts_dir(repo_root) == (
        repo_root / ".ai-native" / "runs"
    ).resolve()
    assert config.workspace.base_branch == "main"
    assert config.git.pr_draft is True


def test_legacy_workflow_stage_order_and_publication_handlers_are_unchanged(app_config) -> None:
    assert ORDERED_STAGES == [
        "intake",
        "recon",
        "plan",
        "architecture",
        "prd",
        "slice",
        "loop",
        "verify",
        "commit",
        "pr",
    ]
    assert ORDERED_STAGES == list(LEGACY_ORDERED_STAGES)
    assert PRE_SLICE_STAGES + SLICE_PIPELINE_STAGES == LEGACY_ORDERED_STAGES
    assert CLI_STAGE_CHOICES == LEGACY_ORDERED_STAGES[2:]

    orchestrator = WorkflowOrchestrator(app_config)
    assert list(orchestrator.stage_handlers) == ORDERED_STAGES
    assert orchestrator.stage_handlers["commit"] is commit_run
    assert orchestrator.stage_handlers["pr"] is create_prs

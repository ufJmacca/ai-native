from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_native.cli import _discover_config_path, _resolve_spec_path, _resolve_workspace_root, main
from ai_native.models import RunState
from ai_native.utils import utc_now


def test_cli_stage_command_invokes_orchestrator(monkeypatch, capsys, app_config, tmp_spec: Path, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = workspace_root / "feature.md"
    spec_path.write_text(tmp_spec.read_text(encoding="utf-8"), encoding="utf-8")

    class FakeOrchestrator:
        def __init__(self, config, progress=None, question_responder=None):
            self.config = config
            self.progress = progress
            self.question_responder = question_responder

        def run_until(self, spec_path, target_stage, run_dir=None, dry_run_pr=False, workspace_root=None):  # type: ignore[no-untyped-def]
            assert spec_path == (Path(workspace_root) / "feature.md").resolve()
            assert target_stage == "plan"
            assert Path(workspace_root) == workspace_root.resolve()
            assert callable(self.progress)
            assert callable(self.question_responder)
            self.progress("[ainative] plan: started")
            return RunState(
                run_id="run-1",
                feature_slug="sample",
                spec_path=str(spec_path),
                workspace_root=str(workspace_root),
                spec_hash="hash",
                run_dir=str(tmp_path / "artifacts" / "run-1"),
                created_at=utc_now(),
                updated_at=utc_now(),
            )

    monkeypatch.setattr("ai_native.cli._load_config", lambda _config_path=None: app_config)
    monkeypatch.setattr("ai_native.cli.WorkflowOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ainative", "stage", "--stage", "plan", "--spec", "feature.md", "--workspace-dir", str(workspace_root)],
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "[ainative] plan: started" in output
    assert str(tmp_path / "artifacts" / "run-1") in output


def test_resolve_spec_path_prefers_target_workspace_when_present(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = workspace_root / "specs" / "task-management.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# Spec\n", encoding="utf-8")

    resolved = _resolve_spec_path(app_config, "specs/task-management.md", workspace_root)

    assert resolved == spec_path.resolve()


def test_resolve_spec_path_falls_back_to_template_repo(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = app_config.repo_root / "specs" / "test-cli-fallback-spec.md"
    spec_path.write_text("# Spec\n", encoding="utf-8")
    try:
        resolved = _resolve_spec_path(app_config, "specs/test-cli-fallback-spec.md", workspace_root)
    finally:
        spec_path.unlink(missing_ok=True)

    assert resolved == spec_path.resolve()


def test_resolve_spec_path_raises_clear_error_when_missing(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()

    with pytest.raises(SystemExit, match="Spec file not found. Checked:"):
        _resolve_spec_path(app_config, "specs/missing.md", workspace_root)


def test_discover_config_path_walks_up_to_parent_repo(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    nested = repo_root / "apps" / "web"
    nested.mkdir(parents=True)
    config_path = repo_root / "ainative.yaml"
    config_path.write_text("git:\n  branch_prefix: codex\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    discovered = _discover_config_path()

    assert discovered == config_path.resolve()


def test_discover_config_path_prefers_env_override(monkeypatch, tmp_path: Path) -> None:
    custom_config = tmp_path / "custom.yaml"
    monkeypatch.setenv("AINATIVE_CONFIG", str(custom_config))

    discovered = _discover_config_path()

    assert discovered == custom_config.resolve()


def test_resolve_workspace_root_defaults_to_current_directory(app_config, monkeypatch, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    monkeypatch.chdir(workspace_root)

    resolved = _resolve_workspace_root(app_config, None)

    assert resolved == workspace_root.resolve()

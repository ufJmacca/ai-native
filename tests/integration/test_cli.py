from __future__ import annotations

import sys
from pathlib import Path

from ai_native.cli import main
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

    monkeypatch.setattr("ai_native.cli._load_config", lambda: app_config)
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

from __future__ import annotations

import sys
from pathlib import Path

from ai_native.cli import main
from ai_native.models import RunState
from ai_native.utils import utc_now


def test_cli_stage_command_invokes_orchestrator(monkeypatch, capsys, app_config, tmp_spec: Path, tmp_path: Path) -> None:
    class FakeOrchestrator:
        def __init__(self, config):
            self.config = config

        def run_until(self, spec_path, target_stage, run_dir=None, dry_run_pr=False):  # type: ignore[no-untyped-def]
            assert spec_path == tmp_spec.resolve()
            assert target_stage == "plan"
            return RunState(
                run_id="run-1",
                feature_slug="sample",
                spec_path=str(spec_path),
                spec_hash="hash",
                run_dir=str(tmp_path / "artifacts" / "run-1"),
                created_at=utc_now(),
                updated_at=utc_now(),
            )

    monkeypatch.setattr("ai_native.cli._load_config", lambda: app_config)
    monkeypatch.setattr("ai_native.cli.WorkflowOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(sys, "argv", ["ainative", "stage", "--stage", "plan", "--spec", str(tmp_spec)])

    assert main() == 0
    assert str(tmp_path / "artifacts" / "run-1") in capsys.readouterr().out


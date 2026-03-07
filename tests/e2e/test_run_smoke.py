from __future__ import annotations

from pathlib import Path

from ai_native.orchestrator import WorkflowOrchestrator
from tests.helpers import FakeWorkflowAdapter


def test_workflow_runs_through_verify_with_fake_agents(monkeypatch, app_config, tmp_spec: Path) -> None:
    fake_builder = FakeWorkflowAdapter()
    fake_critic = FakeWorkflowAdapter()
    fake_verifier = FakeWorkflowAdapter()
    fake_pr_reviewer = FakeWorkflowAdapter()

    monkeypatch.setattr(
        "ai_native.orchestrator.build_role_adapters",
        lambda config: {
            "builder": fake_builder,
            "critic": fake_critic,
            "verifier": fake_verifier,
            "pr_reviewer": fake_pr_reviewer,
        },
    )

    orchestrator = WorkflowOrchestrator(app_config)
    state = orchestrator.run_until(tmp_spec, "verify")
    run_dir = Path(state.run_dir)

    assert (run_dir / "recon" / "context.md").exists()
    assert (run_dir / "plan" / "grounding.md").exists()
    assert (run_dir / "plan" / "intent.md").exists()
    assert (run_dir / "plan" / "implementation.md").exists()
    assert (run_dir / "plan" / "plan.md").exists()
    assert (run_dir / "architecture" / "architecture.mmd").exists()
    assert (run_dir / "prd" / "prd.md").exists()
    assert (run_dir / "slice" / "slices.json").exists()
    assert (run_dir / "slices" / "S001" / "red.log").exists()
    assert (run_dir / "verify" / "S001.md").exists()

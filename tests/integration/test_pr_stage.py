from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages.common import ExecutionContext
from ai_native.stages.git_pr import create_prs
from ai_native.utils import utc_now, write_json
from tests.helpers import FakeWorkflowAdapter


def test_create_prs_dry_run_writes_body_and_review(app_config, tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "run-1"
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    (run_dir / "prd").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "Summary",
            "slices": [
                {
                    "id": "S001",
                    "name": "Create todos",
                    "goal": "Ship the first slice.",
                    "acceptance_criteria": ["Todo can be created"],
                    "file_impact": ["app.py"],
                    "test_plan": ["Test create endpoint"],
                    "dependencies": [],
                }
            ],
        },
    )
    write_json(
        run_dir / "prd" / "prd.json",
        {
            "title": "PRD",
            "user_value": "Users can create todos",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": [],
            "out_of_scope": [],
        },
    )
    state = RunState(
        run_id="run-1",
        feature_slug="todo",
        spec_path=str(tmp_path / "spec.md"),
        spec_hash="hash",
        run_dir=str(run_dir),
        created_at=utc_now(),
        updated_at=utc_now(),
        active_slice="S001",
    )
    prompt_library = PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts")
    adapter = FakeWorkflowAdapter()
    context = ExecutionContext(
        config=app_config,
        prompt_library=prompt_library,
        state_store=StateStore(tmp_path / "artifacts"),
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_path / "spec.md",
        run_dir=run_dir,
        builder=adapter,
        critic=adapter,
        verifier=adapter,
        pr_reviewer=adapter,
    )

    artifacts = create_prs(context, state, dry_run=True)

    assert any(path.name == "S001-body.md" for path in artifacts)
    assert any(path.name == "S001-review.md" for path in artifacts)

from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState
from ai_native.stages.common import ExecutionContext
from ai_native.utils import write_text


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    intake_dir = context.state_store.stage_dir(state, "intake")
    intake_path = intake_dir / "intake.md"
    content = "\n".join(
        [
            "# Intake",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Spec path: `{state.spec_path}`",
            f"- Workspace root: `{state.workspace_root}`",
            f"- Feature slug: `{state.feature_slug}`",
            "",
            "## Spec",
            Path(state.run_dir, "spec.md").read_text(encoding="utf-8"),
        ]
    )
    write_text(intake_path, content)
    return [intake_path]

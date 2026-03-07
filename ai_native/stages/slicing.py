from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState, SlicePlan
from ai_native.stages.common import ExecutionContext, dump_model, render_slice_markdown
from ai_native.utils import read_json, read_text, write_text


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "slice")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    prd = read_json(Path(state.run_dir) / "prd" / "prd.json")
    prompt = context.prompt_library.render(
        "slice.md",
        spec_text=read_text(context.spec_path),
        plan=plan,
        prd=prd,
    )
    schema_path = context.template_root / "ai_native" / "schemas" / "slice-plan.json"
    response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
    slice_plan = SlicePlan.model_validate(response.json_data)

    index_json = stage_dir / "slices.json"
    index_md = stage_dir / "slices.md"
    dump_model(index_json, slice_plan)
    write_text(index_md, render_slice_markdown(slice_plan))

    artifacts = [index_json, index_md]
    for slice_def in slice_plan.slices:
        slice_path = stage_dir / f"{slice_def.id}-{slice_def.name.lower().replace(' ', '-')}.md"
        write_text(
            slice_path,
            "\n".join(
                [
                    f"# {slice_def.id}: {slice_def.name}",
                    "",
                    "## Goal",
                    slice_def.goal,
                    "",
                    "## Acceptance Criteria",
                    "\n".join(f"- {item}" for item in slice_def.acceptance_criteria) or "- None",
                    "",
                    "## File Impact",
                    "\n".join(f"- {item}" for item in slice_def.file_impact) or "- None",
                    "",
                    "## Test Plan",
                    "\n".join(f"- {item}" for item in slice_def.test_plan) or "- None",
                    "",
                    "## Dependencies",
                    "\n".join(f"- {item}" for item in slice_def.dependencies) or "- None",
                ]
            ),
        )
        artifacts.append(slice_path)
    return artifacts

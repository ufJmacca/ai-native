from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from ai_native.models import RunState, SlicePlan
from ai_native.reference_workflow import append_reference_prompt_block
from ai_native.specs import load_prompt_spec_text
from ai_native.stages.common import ExecutionContext, dump_model, render_slice_markdown
from ai_native.utils import read_json, slugify, write_text

_SLICE_ARTIFACT_FILENAME_MAX_BYTES = 240
_SLICE_ARTIFACT_DIGEST_LENGTH = 16


def _slice_artifact_filename(slice_id: str, slice_name: str) -> str:
    """Return a single portable filename for one generated slice artifact."""

    safe_id = slice_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", safe_id):
        safe_id = slugify(safe_id)
    filename = f"{safe_id}-{slugify(slice_name)}.md"
    encoded_filename = filename.encode("ascii")
    if len(encoded_filename) <= _SLICE_ARTIFACT_FILENAME_MAX_BYTES:
        return filename

    digest = sha256(encoded_filename).hexdigest()[:_SLICE_ARTIFACT_DIGEST_LENGTH]
    suffix = f"-{digest}.md"
    prefix_bytes = _SLICE_ARTIFACT_FILENAME_MAX_BYTES - len(suffix.encode("ascii"))
    return f"{encoded_filename[:prefix_bytes].decode('ascii')}{suffix}"


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "slice")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    prd = read_json(Path(state.run_dir) / "prd" / "prd.json")
    prompt = append_reference_prompt_block(
        context.prompt_library.render(
            "slice.md",
            spec_text=load_prompt_spec_text(Path(state.run_dir), context.spec_path),
            plan=plan,
            prd=prd,
        ),
        Path(context.run_dir),
    )
    schema_path = context.template_root / "schemas" / "slice-plan.json"
    response = context.builder.run(
        prompt, cwd=context.repo_root, schema_path=schema_path
    )
    slice_plan = SlicePlan.model_validate(response.json_data)

    index_json = stage_dir / "slices.json"
    index_md = stage_dir / "slices.md"
    dump_model(index_json, slice_plan)
    write_text(index_md, render_slice_markdown(slice_plan))

    artifacts = [index_json, index_md]
    for slice_def in slice_plan.slices:
        slice_path = stage_dir / _slice_artifact_filename(
            slice_def.id,
            slice_def.name,
        )
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
                    "\n".join(f"- {item}" for item in slice_def.acceptance_criteria)
                    or "- None",
                    "",
                    "## File Impact",
                    "\n".join(f"- {item}" for item in slice_def.file_impact)
                    or "- None",
                    "",
                    "## Test Plan",
                    "\n".join(f"- {item}" for item in slice_def.test_plan) or "- None",
                    "",
                    "## Dependencies",
                    "\n".join(f"- {item}" for item in slice_def.dependencies)
                    or "- None",
                ]
            ),
        )
        artifacts.append(slice_path)
    return artifacts

from __future__ import annotations

from pathlib import Path

from ai_native.models import RunState, SlicePlan, VerificationReport
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_verification_markdown
from ai_native.utils import read_json, read_text, write_text


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    verification_dir = context.state_store.stage_dir(state, "verify")
    slice_plan = SlicePlan.model_validate(read_json(Path(state.run_dir) / "slice" / "slices.json"))
    artifacts: list[Path] = []
    for slice_def in slice_plan.slices:
        slice_dir = Path(state.run_dir) / "slices" / slice_def.id
        prompt = context.prompt_library.render(
            "verify.md",
            spec_text=read_text(context.spec_path),
            slice_definition=slice_def.model_dump(mode="json"),
            slice_dir=slice_dir,
        )
        schema_path = context.template_root / "ai_native" / "schemas" / "verification-report.json"
        response = context.verifier.run(prompt, cwd=context.repo_root, schema_path=schema_path)
        verification = VerificationReport.model_validate(response.json_data)
        json_path = verification_dir / f"{slice_def.id}.json"
        md_path = verification_dir / f"{slice_def.id}.md"
        dump_model(json_path, verification)
        write_text(md_path, render_verification_markdown(verification))
        artifacts.extend([json_path, md_path])
        if verification.verdict != "passed":
            raise StageError(f"Verification failed for slice {slice_def.id}: {verification.summary}")
    return artifacts

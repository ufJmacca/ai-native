from __future__ import annotations

from pathlib import Path

from ai_native.models import ReviewReport, RunState, SlicePlan
from ai_native.stages.common import ExecutionContext, StageError, write_review
from ai_native.utils import read_json, read_text, write_text


def _slice_dir(state: RunState, slice_id: str) -> Path:
    return Path(state.run_dir) / "slices" / slice_id


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    slice_plan = SlicePlan.model_validate(read_json(Path(state.run_dir) / "slice" / "slices.json"))
    artifacts: list[Path] = []
    for slice_def in slice_plan.slices:
        slice_dir = _slice_dir(state, slice_def.id)
        slice_dir.mkdir(parents=True, exist_ok=True)
        prompt = context.prompt_library.render(
            "loop.md",
            spec_text=read_text(context.spec_path),
            slice_definition=slice_def.model_dump(mode="json"),
            run_dir=state.run_dir,
            slice_dir=slice_dir,
        )
        builder_summary = context.builder.run(prompt, cwd=context.repo_root)
        summary_path = slice_dir / "builder-summary.md"
        write_text(summary_path, builder_summary.text or "# Builder Summary\n")
        artifacts.append(summary_path)

        red_log = slice_dir / "red.log"
        green_log = slice_dir / "green.log"
        refactor_notes = slice_dir / "refactor-notes.md"
        for expected_path in (red_log, green_log, refactor_notes):
            if not expected_path.exists():
                raise StageError(
                    f"Loop output missing for slice {slice_def.id}: expected {expected_path.name} to be created by the builder."
                )
            artifacts.append(expected_path)

        review_prompt = context.prompt_library.render(
            "test_review.md",
            spec_text=read_text(context.spec_path),
            slice_definition=slice_def.model_dump(mode="json"),
            slice_dir=slice_dir,
        )
        review_schema = context.template_root / "ai_native" / "schemas" / "review-report.json"
        review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
        review = ReviewReport.model_validate(review_response.json_data)
        review_md = slice_dir / "test-review.md"
        write_review(review_md, review)
        artifacts.extend([review_md, review_md.with_suffix(".json")])
        if context.config.quality_gates.require_test_critique and review.verdict != "approved":
            raise StageError(f"Test critique failed for slice {slice_def.id}: {review.summary}")
        state.active_slice = slice_def.id
    return artifacts

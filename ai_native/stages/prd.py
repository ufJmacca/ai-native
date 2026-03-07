from __future__ import annotations

from pathlib import Path

from ai_native.models import PRDArtifact, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_prd_markdown, write_review
from ai_native.utils import read_json, read_text, write_text


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "prd")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    architecture = read_json(Path(state.run_dir) / "architecture" / "architecture.json")
    prompt = context.prompt_library.render(
        "prd.md",
        spec_text=read_text(context.spec_path),
        plan=plan,
        architecture=architecture,
    )
    schema_path = context.repo_root / "ai_native" / "schemas" / "prd-artifact.json"
    response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
    prd = PRDArtifact.model_validate(response.json_data)

    prd_json = stage_dir / "prd.json"
    prd_md = stage_dir / "prd.md"
    dump_model(prd_json, prd)
    write_text(prd_md, render_prd_markdown(prd))

    review_prompt = context.prompt_library.render(
        "prd_review.md",
        spec_text=read_text(context.spec_path),
        prd=prd.model_dump(mode="json"),
        plan=plan,
    )
    review_schema = context.repo_root / "ai_native" / "schemas" / "review-report.json"
    review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
    review = ReviewReport.model_validate(review_response.json_data)
    review_md = stage_dir / "prd-review.md"
    write_review(review_md, review)
    if context.config.quality_gates.require_prd_approval and review.verdict != "approved":
        raise StageError(f"PRD critique failed: {review.summary}")
    return [prd_json, prd_md, review_md, review_md.with_suffix(".json")]


from __future__ import annotations

from pathlib import Path

from ai_native.models import PlanArtifact, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_plan_markdown, write_review
from ai_native.utils import read_json, read_text, write_text


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "plan")
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    grounding_prompt = context.prompt_library.render(
        "plan_phase_grounding.md",
        spec_text=read_text(context.spec_path),
        context_report=context_report,
    )
    grounding_notes = context.builder.run(grounding_prompt, cwd=context.repo_root).text
    grounding_md = stage_dir / "grounding.md"
    write_text(grounding_md, grounding_notes)

    intent_prompt = context.prompt_library.render(
        "plan_phase_intent.md",
        spec_text=read_text(context.spec_path),
        context_report=context_report,
        grounding_notes=grounding_notes,
    )
    intent_notes = context.builder.run(intent_prompt, cwd=context.repo_root).text
    intent_md = stage_dir / "intent.md"
    write_text(intent_md, intent_notes)

    implementation_prompt = context.prompt_library.render(
        "plan_phase_implementation.md",
        spec_text=read_text(context.spec_path),
        context_report=context_report,
        grounding_notes=grounding_notes,
        intent_notes=intent_notes,
    )
    implementation_notes = context.builder.run(implementation_prompt, cwd=context.repo_root).text
    implementation_md = stage_dir / "implementation.md"
    write_text(implementation_md, implementation_notes)

    prompt = context.prompt_library.render(
        "plan.md",
        spec_text=read_text(context.spec_path),
        context_report=context_report,
        grounding_notes=grounding_notes,
        intent_notes=intent_notes,
        implementation_notes=implementation_notes,
    )
    schema_path = context.repo_root / "ai_native" / "schemas" / "plan-artifact.json"
    response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
    plan = PlanArtifact.model_validate(response.json_data)

    plan_json = stage_dir / "plan.json"
    plan_md = stage_dir / "plan.md"
    dump_model(plan_json, plan)
    write_text(plan_md, render_plan_markdown(plan))

    review_prompt = context.prompt_library.render(
        "plan_review.md",
        spec_text=read_text(context.spec_path),
        plan=plan.model_dump(mode="json"),
        context_report=context_report,
    )
    review_schema = context.repo_root / "ai_native" / "schemas" / "review-report.json"
    review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
    review = ReviewReport.model_validate(review_response.json_data)
    review_md = stage_dir / "plan-review.md"
    write_review(review_md, review)
    if context.config.quality_gates.require_plan_approval and review.verdict != "approved":
        raise StageError(f"Plan critique failed: {review.summary}")
    return [grounding_md, intent_md, implementation_md, plan_json, plan_md, review_md, review_md.with_suffix(".json")]

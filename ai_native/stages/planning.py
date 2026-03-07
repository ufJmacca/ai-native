from __future__ import annotations

from pathlib import Path

from ai_native.models import PlanArtifact, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_plan_markdown, write_review
from ai_native.utils import read_json, read_text, write_text


def _render_plan_prompt(
    context: ExecutionContext,
    spec_text: str,
    context_report: dict[str, object],
    grounding_notes: str,
    intent_notes: str,
    implementation_notes: str,
    prior_plan: PlanArtifact | None = None,
    critique: ReviewReport | None = None,
) -> str:
    if prior_plan and critique:
        return context.prompt_library.render(
            "plan_revise.md",
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            intent_notes=intent_notes,
            implementation_notes=implementation_notes,
            prior_plan=prior_plan.model_dump(mode="json"),
            critique=critique.model_dump(mode="json"),
        )
    return context.prompt_library.render(
        "plan.md",
        spec_text=spec_text,
        context_report=context_report,
        grounding_notes=grounding_notes,
        intent_notes=intent_notes,
        implementation_notes=implementation_notes,
    )


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "plan")
    spec_text = read_text(context.spec_path)
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    artifacts: list[Path] = []

    context.emit_progress("[ainative] plan: grounding")
    grounding_prompt = context.prompt_library.render(
        "plan_phase_grounding.md",
        spec_text=spec_text,
        context_report=context_report,
    )
    grounding_notes = context.builder.run(grounding_prompt, cwd=context.repo_root).text
    grounding_md = stage_dir / "grounding.md"
    write_text(grounding_md, grounding_notes)
    artifacts.append(grounding_md)

    context.emit_progress("[ainative] plan: intent")
    intent_prompt = context.prompt_library.render(
        "plan_phase_intent.md",
        spec_text=spec_text,
        context_report=context_report,
        grounding_notes=grounding_notes,
    )
    intent_notes = context.builder.run(intent_prompt, cwd=context.repo_root).text
    intent_md = stage_dir / "intent.md"
    write_text(intent_md, intent_notes)
    artifacts.append(intent_md)

    context.emit_progress("[ainative] plan: implementation")
    implementation_prompt = context.prompt_library.render(
        "plan_phase_implementation.md",
        spec_text=spec_text,
        context_report=context_report,
        grounding_notes=grounding_notes,
        intent_notes=intent_notes,
    )
    implementation_notes = context.builder.run(implementation_prompt, cwd=context.repo_root).text
    implementation_md = stage_dir / "implementation.md"
    write_text(implementation_md, implementation_notes)
    artifacts.append(implementation_md)

    schema_path = context.template_root / "ai_native" / "schemas" / "plan-artifact.json"
    review_schema = context.template_root / "ai_native" / "schemas" / "review-report.json"
    max_attempts = max(1, context.config.workspace.plan_max_attempts)
    plan: PlanArtifact | None = None
    review: ReviewReport | None = None

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            context.emit_progress(f"[ainative] plan: synthesis attempt {attempt}/{max_attempts}")
        else:
            context.emit_progress(f"[ainative] plan: revision attempt {attempt}/{max_attempts}")

        prompt = _render_plan_prompt(
            context=context,
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            intent_notes=intent_notes,
            implementation_notes=implementation_notes,
            prior_plan=plan,
            critique=review,
        )
        response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
        plan = PlanArtifact.model_validate(response.json_data)

        plan_json = stage_dir / "plan.json"
        plan_md = stage_dir / "plan.md"
        dump_model(plan_json, plan)
        write_text(plan_md, render_plan_markdown(plan))
        attempt_plan_json = stage_dir / f"plan-attempt-{attempt}.json"
        attempt_plan_md = stage_dir / f"plan-attempt-{attempt}.md"
        dump_model(attempt_plan_json, plan)
        write_text(attempt_plan_md, render_plan_markdown(plan))
        artifacts.extend([plan_json, plan_md, attempt_plan_json, attempt_plan_md])

        review_prompt = context.prompt_library.render(
            "plan_review.md",
            spec_text=spec_text,
            plan=plan.model_dump(mode="json"),
            context_report=context_report,
        )
        review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
        review = ReviewReport.model_validate(review_response.json_data)
        review_md = stage_dir / "plan-review.md"
        attempt_review_md = stage_dir / f"plan-review-attempt-{attempt}.md"
        write_review(review_md, review)
        write_review(attempt_review_md, review)
        artifacts.extend([review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")])
        if review.verdict == "approved":
            return list(dict.fromkeys(artifacts))
        if attempt < max_attempts:
            context.emit_progress(f"[ainative] plan: critique requested changes, retrying - {review.summary}")

    if context.config.quality_gates.require_plan_approval and review is not None:
        raise StageError(f"Plan critique failed after {max_attempts} attempts: {review.summary}")
    return list(dict.fromkeys(artifacts))

from __future__ import annotations

import re
from pathlib import Path

from ai_native.models import PlanArtifact, QuestionBatch, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_plan_markdown, write_review
from ai_native.utils import read_json, read_text, write_json, write_text

ATTEMPT_RE = re.compile(r"plan-review-attempt-(?P<attempt>\d+)\.json$")


def _render_plan_prompt(
    context: ExecutionContext,
    spec_text: str,
    context_report: dict[str, object],
    grounding_notes: str,
    intent_notes: str,
    implementation_notes: str,
    user_answers: str,
    approval_checklist: str,
    critique_history: str,
    blocker_ledger: str,
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
            user_answers=user_answers,
            approval_checklist=approval_checklist,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
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
        user_answers=user_answers,
        approval_checklist=approval_checklist,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )


def _render_user_answers(answer_pairs: list[dict[str, str]]) -> str:
    if not answer_pairs:
        return "No user clarifications were requested."
    lines = []
    for item in answer_pairs:
        lines.extend([f"- Question: {item['question']}", f"  Answer: {item['answer'] or '(no answer provided)'}"])
    return "\n".join(lines)


def _write_question_artifacts(stage_dir: Path, batch: QuestionBatch, answer_pairs: list[dict[str, str]]) -> list[Path]:
    questions_json = stage_dir / "questions.json"
    questions_md = stage_dir / "questions.md"
    answers_json = stage_dir / "answers.json"
    answers_md = stage_dir / "answers.md"
    write_json(questions_json, batch.model_dump(mode="json"))
    write_text(
        questions_md,
        "\n".join(
            [
                "# Planning Questions",
                "",
                f"- Needs user input: `{str(batch.needs_user_input).lower()}`",
                "",
                "## Summary",
                batch.summary or "No planning clarification was needed.",
                "",
                "## Questions",
                "\n".join(f"- {question}" for question in batch.questions) or "- None",
            ]
        ),
    )
    write_json(answers_json, answer_pairs)
    write_text(
        answers_md,
        "\n".join(
            [
                "# Planning Answers",
                "",
                _render_user_answers(answer_pairs),
            ]
        ),
    )
    return [questions_json, questions_md, answers_json, answers_md]


def _existing_attempt_numbers(stage_dir: Path) -> list[int]:
    attempts: list[int] = []
    for review_path in stage_dir.glob("plan-review-attempt-*.json"):
        match = ATTEMPT_RE.fullmatch(review_path.name)
        if not match:
            continue
        attempt = int(match.group("attempt"))
        if (stage_dir / f"plan-attempt-{attempt}.json").exists():
            attempts.append(attempt)
    return sorted(attempts)


def _existing_plan_artifacts(stage_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        stage_dir / "grounding.md",
        stage_dir / "intent.md",
        stage_dir / "implementation.md",
        stage_dir / "approval-checklist.md",
        stage_dir / "critique-history.md",
        stage_dir / "blocker-ledger.md",
        stage_dir / "plan.json",
        stage_dir / "plan.md",
        stage_dir / "plan-review.json",
        stage_dir / "plan-review.md",
        stage_dir / "questions.json",
        stage_dir / "questions.md",
        stage_dir / "answers.json",
        stage_dir / "answers.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in ("plan-attempt-*.json", "plan-attempt-*.md", "plan-review-attempt-*.json", "plan-review-attempt-*.md"):
        paths.extend(sorted(stage_dir.glob(pattern)))
    return paths


def _load_resume_state(stage_dir: Path) -> dict[str, object] | None:
    grounding_md = stage_dir / "grounding.md"
    intent_md = stage_dir / "intent.md"
    implementation_md = stage_dir / "implementation.md"
    if not all(path.exists() for path in (grounding_md, intent_md, implementation_md)):
        return None
    attempts = _existing_attempt_numbers(stage_dir)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    return {
        "grounding_notes": read_text(grounding_md),
        "intent_notes": read_text(intent_md),
        "implementation_notes": read_text(implementation_md),
        "prior_plan": PlanArtifact.model_validate(read_json(stage_dir / f"plan-attempt-{last_attempt}.json")),
        "prior_review": ReviewReport.model_validate(read_json(stage_dir / f"plan-review-attempt-{last_attempt}.json")),
        "last_attempt": last_attempt,
    }


def _load_review_history(stage_dir: Path) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_attempt_numbers(stage_dir):
        history.append(
            (
                attempt,
                ReviewReport.model_validate(read_json(stage_dir / f"plan-review-attempt-{attempt}.json")),
            )
        )
    return history


def _normalize_blocker(text: str) -> str:
    return " ".join(text.lower().split())


def _collect_blocker_ledger(history: list[tuple[int, ReviewReport]]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for _attempt, review in history:
        for blocker in review.required_changes:
            key = _normalize_blocker(blocker)
            if key in seen:
                continue
            seen.add(key)
            blockers.append(blocker)
    return blockers


def _render_critique_history(history: list[tuple[int, ReviewReport]]) -> str:
    if not history:
        return "\n".join(
            [
                "# Critique History",
                "",
                "No prior critiques exist for this planning run.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved acceptance-critical blockers unless the revised plan makes them explicit or explicitly narrows scope to remove the ambiguity.",
    ]
    for attempt, review in history:
        lines.extend(
            [
                "",
                f"## Attempt {attempt}",
                f"- Verdict: `{review.verdict}`",
                f"- Summary: {review.summary}",
                "",
                "Required changes:",
            ]
        )
        if review.required_changes:
            lines.extend(f"- {change}" for change in review.required_changes)
        else:
            lines.append("- None")
    return "\n".join(lines)


def _render_blocker_ledger(blockers: list[str]) -> str:
    if not blockers:
        return "\n".join(
            [
                "# Blocker Ledger",
                "",
                "No carried-forward blockers exist yet.",
            ]
        )
    return "\n".join(
        [
            "# Blocker Ledger",
            "",
            "These are the stable acceptance-critical blockers accumulated so far. Resolve or explicitly de-scope them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(stage_dir: Path, approval_checklist: str, critique_history: str, blocker_ledger: str) -> list[Path]:
    approval_checklist_path = stage_dir / "approval-checklist.md"
    critique_history_path = stage_dir / "critique-history.md"
    blocker_ledger_path = stage_dir / "blocker-ledger.md"
    write_text(approval_checklist_path, approval_checklist)
    write_text(critique_history_path, critique_history)
    write_text(blocker_ledger_path, blocker_ledger)
    return [approval_checklist_path, critique_history_path, blocker_ledger_path]


def _parse_additional_attempts(answer: str, default_attempts: int) -> int:
    raw = answer.strip()
    if not raw:
        return default_attempts
    try:
        return max(1, int(raw))
    except ValueError:
        return default_attempts


def _ask_to_continue_after_exhaustion(context: ExecutionContext, current_limit: int, review: ReviewReport) -> int | None:
    responses = context.ask_questions(
        "plan",
        [
            (
                f"Planning has exhausted {current_limit} attempts. The latest critique summary is:\n"
                f"{review.summary}\n"
                "Continue with more planning attempts? Answer yes or no."
            ),
            f"If yes, how many additional attempts should be allowed? Press Enter to use {current_limit}.",
        ],
    )
    if not responses:
        return None
    if responses[0].strip().lower() not in {"y", "yes"}:
        return None
    return _parse_additional_attempts(responses[1] if len(responses) > 1 else "", current_limit)


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "plan")
    spec_text = read_text(context.spec_path)
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    grounding_md = stage_dir / "grounding.md"
    intent_md = stage_dir / "intent.md"
    implementation_md = stage_dir / "implementation.md"
    artifacts = _existing_plan_artifacts(stage_dir)
    resume_state = _load_resume_state(stage_dir)
    if resume_state:
        grounding_notes = str(resume_state["grounding_notes"])
        intent_notes = str(resume_state["intent_notes"])
        implementation_notes = str(resume_state["implementation_notes"])
        context.emit_progress(
            f"[ainative] plan: resuming from previous critique at attempt {int(resume_state['last_attempt']) + 1}"
        )
    else:
        context.emit_progress("[ainative] plan: grounding")
        grounding_prompt = context.prompt_library.render(
            "plan_phase_grounding.md",
            spec_text=spec_text,
            context_report=context_report,
        )
        grounding_notes = context.builder.run(grounding_prompt, cwd=context.repo_root).text
        write_text(grounding_md, grounding_notes)
        artifacts.append(grounding_md)

    answer_pairs: list[dict[str, str]] = []
    questions_json = stage_dir / "questions.json"
    answers_json = stage_dir / "answers.json"
    if answers_json.exists():
        answer_pairs = list(read_json(answers_json))
    remaining_run_budget = max(0, context.config.workspace.question_budget_per_run - int(state.metadata.get("question_batches_used", 0)))
    if not resume_state and context.config.workspace.question_budget_per_stage > 0 and remaining_run_budget > 0 and not answer_pairs:
        context.emit_progress("[ainative] plan: evaluating whether clarification is needed")
        question_prompt = context.prompt_library.render(
            "plan_questions.md",
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            max_questions=min(3, remaining_run_budget),
        )
        question_schema = context.template_root / "ai_native" / "schemas" / "question-batch.json"
        question_response = context.builder.run(question_prompt, cwd=context.repo_root, schema_path=question_schema)
        question_batch = QuestionBatch.model_validate(question_response.json_data)
        if question_batch.needs_user_input and question_batch.questions:
            answers = context.ask_questions("plan", question_batch.questions)
            answer_pairs = [
                {"question": question, "answer": answers[index] if index < len(answers) else ""}
                for index, question in enumerate(question_batch.questions)
            ]
            state.metadata["question_batches_used"] = int(state.metadata.get("question_batches_used", 0)) + 1
        artifacts.extend(_write_question_artifacts(stage_dir, question_batch, answer_pairs))
    elif questions_json.exists():
        existing_batch = QuestionBatch.model_validate(read_json(questions_json))
        artifacts.extend(_write_question_artifacts(stage_dir, existing_batch, answer_pairs))

    user_answers = _render_user_answers(answer_pairs)

    if not resume_state:
        context.emit_progress("[ainative] plan: intent")
        intent_prompt = context.prompt_library.render(
            "plan_phase_intent.md",
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            user_answers=user_answers,
        )
        intent_notes = context.builder.run(intent_prompt, cwd=context.repo_root).text
        write_text(intent_md, intent_notes)
        artifacts.append(intent_md)

        context.emit_progress("[ainative] plan: implementation")
        implementation_prompt = context.prompt_library.render(
            "plan_phase_implementation.md",
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            intent_notes=intent_notes,
            user_answers=user_answers,
        )
        implementation_notes = context.builder.run(implementation_prompt, cwd=context.repo_root).text
        write_text(implementation_md, implementation_notes)
        artifacts.append(implementation_md)

    approval_checklist_path = stage_dir / "approval-checklist.md"
    if approval_checklist_path.exists():
        approval_checklist = read_text(approval_checklist_path)
        artifacts.append(approval_checklist_path)
    else:
        context.emit_progress("[ainative] plan: approval checklist")
        checklist_prompt = context.prompt_library.render(
            "plan_checklist.md",
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            intent_notes=intent_notes,
            implementation_notes=implementation_notes,
            user_answers=user_answers,
        )
        approval_checklist = context.builder.run(checklist_prompt, cwd=context.repo_root).text
        write_text(approval_checklist_path, approval_checklist)
        artifacts.append(approval_checklist_path)

    review_history = _load_review_history(stage_dir)
    critique_history = _render_critique_history(review_history)
    blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
    artifacts.extend(_write_guidance_artifacts(stage_dir, approval_checklist, critique_history, blocker_ledger))

    schema_path = context.template_root / "ai_native" / "schemas" / "plan-artifact.json"
    review_schema = context.template_root / "ai_native" / "schemas" / "review-report.json"
    attempt_limit = max(1, context.config.workspace.plan_max_attempts)
    plan = PlanArtifact.model_validate(resume_state["prior_plan"]) if resume_state else None
    review = ReviewReport.model_validate(resume_state["prior_review"]) if resume_state else None
    next_attempt = int(resume_state["last_attempt"]) + 1 if resume_state else 1

    while True:
        if next_attempt > attempt_limit:
            if context.config.quality_gates.require_plan_approval and review is not None:
                context.emit_progress("[ainative] plan: attempt budget exhausted")
                additional_attempts = _ask_to_continue_after_exhaustion(context, attempt_limit, review)
                if additional_attempts is None:
                    raise StageError(f"Plan critique failed after {attempt_limit} attempts: {review.summary}")
                attempt_limit += additional_attempts
                context.emit_progress(
                    f"[ainative] plan: continuing with {additional_attempts} additional attempts (new limit {attempt_limit})"
                )
            else:
                return list(dict.fromkeys(artifacts))

        attempt = next_attempt
        if attempt == 1 and review is None:
            context.emit_progress(f"[ainative] plan: synthesis attempt {attempt}/{attempt_limit}")
        else:
            context.emit_progress(f"[ainative] plan: revision attempt {attempt}/{attempt_limit}")

        prompt = _render_plan_prompt(
            context=context,
            spec_text=spec_text,
            context_report=context_report,
            grounding_notes=grounding_notes,
            intent_notes=intent_notes,
            implementation_notes=implementation_notes,
            user_answers=user_answers,
            approval_checklist=approval_checklist,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
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
            approval_checklist=approval_checklist,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
        )
        review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
        review = ReviewReport.model_validate(review_response.json_data)
        review_md = stage_dir / "plan-review.md"
        attempt_review_md = stage_dir / f"plan-review-attempt-{attempt}.md"
        write_review(review_md, review)
        write_review(attempt_review_md, review)
        artifacts.extend([review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")])
        review_history = _load_review_history(stage_dir)
        critique_history = _render_critique_history(review_history)
        blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
        artifacts.extend(_write_guidance_artifacts(stage_dir, approval_checklist, critique_history, blocker_ledger))
        if review.verdict == "approved":
            return list(dict.fromkeys(artifacts))
        if attempt < attempt_limit:
            context.emit_progress(f"[ainative] plan: critique requested changes, retrying - {review.summary}")
        next_attempt = attempt + 1

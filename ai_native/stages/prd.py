from __future__ import annotations

import re
import shutil
from pathlib import Path

from ai_native.models import PRDArtifact, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_prd_markdown, write_review
from ai_native.utils import read_json, read_text, write_text

ATTEMPT_RE = re.compile(r"prd-review-attempt-(?P<attempt>\d+)\.json$")


def _existing_attempt_numbers(stage_dir: Path) -> list[int]:
    attempts: list[int] = []
    for review_path in stage_dir.glob("prd-review-attempt-*.json"):
        match = ATTEMPT_RE.fullmatch(review_path.name)
        if not match:
            continue
        attempt = int(match.group("attempt"))
        if (stage_dir / f"prd-attempt-{attempt}.json").exists():
            attempts.append(attempt)
    return sorted(attempts)


def _existing_prd_artifacts(stage_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        stage_dir / "prd.json",
        stage_dir / "prd.md",
        stage_dir / "prd-review.json",
        stage_dir / "prd-review.md",
        stage_dir / "critique-history.md",
        stage_dir / "blocker-ledger.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in ("prd-attempt-*.json", "prd-attempt-*.md", "prd-review-attempt-*.json", "prd-review-attempt-*.md"):
        paths.extend(sorted(stage_dir.glob(pattern)))
    return paths


def _materialize_legacy_attempt(stage_dir: Path) -> None:
    if _existing_attempt_numbers(stage_dir):
        return
    required = [
        stage_dir / "prd.json",
        stage_dir / "prd.md",
        stage_dir / "prd-review.json",
        stage_dir / "prd-review.md",
    ]
    if not all(path.exists() for path in required):
        return
    shutil.copyfile(stage_dir / "prd.json", stage_dir / "prd-attempt-1.json")
    shutil.copyfile(stage_dir / "prd.md", stage_dir / "prd-attempt-1.md")
    shutil.copyfile(stage_dir / "prd-review.json", stage_dir / "prd-review-attempt-1.json")
    shutil.copyfile(stage_dir / "prd-review.md", stage_dir / "prd-review-attempt-1.md")


def _load_resume_state(stage_dir: Path) -> dict[str, object] | None:
    attempts = _existing_attempt_numbers(stage_dir)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    return {
        "prior_prd": PRDArtifact.model_validate(read_json(stage_dir / f"prd-attempt-{last_attempt}.json")),
        "prior_review": ReviewReport.model_validate(read_json(stage_dir / f"prd-review-attempt-{last_attempt}.json")),
        "last_attempt": last_attempt,
    }


def _load_review_history(stage_dir: Path) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_attempt_numbers(stage_dir):
        history.append(
            (
                attempt,
                ReviewReport.model_validate(read_json(stage_dir / f"prd-review-attempt-{attempt}.json")),
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
                "No prior critiques exist for this PRD run.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved PRD blockers unless the revised PRD makes them explicit or explicitly narrows scope to remove the ambiguity.",
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
            "These are the stable PRD blockers accumulated so far. Resolve or explicitly de-scope them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(stage_dir: Path, critique_history: str, blocker_ledger: str) -> list[Path]:
    critique_history_path = stage_dir / "critique-history.md"
    blocker_ledger_path = stage_dir / "blocker-ledger.md"
    write_text(critique_history_path, critique_history)
    write_text(blocker_ledger_path, blocker_ledger)
    return [critique_history_path, blocker_ledger_path]


def _render_prd_prompt(
    context: ExecutionContext,
    spec_text: str,
    context_report: dict[str, object],
    plan: dict[str, object],
    architecture: dict[str, object],
    critique_history: str,
    blocker_ledger: str,
    prior_prd: PRDArtifact | None = None,
    critique: ReviewReport | None = None,
) -> str:
    if prior_prd and critique:
        return context.prompt_library.render(
            "prd_revise.md",
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            architecture=architecture,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
            prior_prd=prior_prd.model_dump(mode="json"),
            critique=critique.model_dump(mode="json"),
        )
    return context.prompt_library.render(
        "prd.md",
        spec_text=spec_text,
        context_report=context_report,
        plan=plan,
        architecture=architecture,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )


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
        "prd",
        [
            (
                f"PRD has exhausted {current_limit} attempts. The latest critique summary is:\n"
                f"{review.summary}\n"
                "Continue with more PRD attempts? Answer yes or no."
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
    stage_dir = context.state_store.stage_dir(state, "prd")
    _materialize_legacy_attempt(stage_dir)
    spec_text = read_text(context.spec_path)
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    architecture = read_json(Path(state.run_dir) / "architecture" / "architecture.json")
    artifacts = _existing_prd_artifacts(stage_dir)
    resume_state = _load_resume_state(stage_dir)
    review_history = _load_review_history(stage_dir)
    critique_history = _render_critique_history(review_history)
    blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
    artifacts.extend(_write_guidance_artifacts(stage_dir, critique_history, blocker_ledger))

    schema_path = context.template_root / "ai_native" / "schemas" / "prd-artifact.json"
    review_schema = context.template_root / "ai_native" / "schemas" / "review-report.json"
    attempt_limit = max(1, context.config.workspace.prd_max_attempts)
    prd = PRDArtifact.model_validate(resume_state["prior_prd"]) if resume_state else None
    review = ReviewReport.model_validate(resume_state["prior_review"]) if resume_state else None
    next_attempt = int(resume_state["last_attempt"]) + 1 if resume_state else 1

    if resume_state:
        context.emit_progress(f"[ainative] prd: resuming from previous critique at attempt {int(resume_state['last_attempt']) + 1}")

    while True:
        if next_attempt > attempt_limit:
            if context.config.quality_gates.require_prd_approval and review is not None:
                context.emit_progress("[ainative] prd: attempt budget exhausted")
                additional_attempts = _ask_to_continue_after_exhaustion(context, attempt_limit, review)
                if additional_attempts is None:
                    raise StageError(f"PRD critique failed after {attempt_limit} attempts: {review.summary}")
                attempt_limit += additional_attempts
                context.emit_progress(
                    f"[ainative] prd: continuing with {additional_attempts} additional attempts (new limit {attempt_limit})"
                )
            else:
                return list(dict.fromkeys(artifacts))

        attempt = next_attempt
        if attempt == 1 and review is None:
            context.emit_progress(f"[ainative] prd: synthesis attempt {attempt}/{attempt_limit}")
        else:
            context.emit_progress(f"[ainative] prd: revision attempt {attempt}/{attempt_limit}")

        prompt = _render_prd_prompt(
            context=context,
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            architecture=architecture,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
            prior_prd=prd,
            critique=review,
        )
        response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
        prd = PRDArtifact.model_validate(response.json_data)

        prd_json = stage_dir / "prd.json"
        prd_md = stage_dir / "prd.md"
        dump_model(prd_json, prd)
        write_text(prd_md, render_prd_markdown(prd))
        attempt_prd_json = stage_dir / f"prd-attempt-{attempt}.json"
        attempt_prd_md = stage_dir / f"prd-attempt-{attempt}.md"
        dump_model(attempt_prd_json, prd)
        write_text(attempt_prd_md, render_prd_markdown(prd))
        artifacts.extend([prd_json, prd_md, attempt_prd_json, attempt_prd_md])

        review_prompt = context.prompt_library.render(
            "prd_review.md",
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            architecture=architecture,
            prd=prd.model_dump(mode="json"),
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
        )
        review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
        review = ReviewReport.model_validate(review_response.json_data)
        review_md = stage_dir / "prd-review.md"
        attempt_review_md = stage_dir / f"prd-review-attempt-{attempt}.md"
        write_review(review_md, review)
        write_review(attempt_review_md, review)
        artifacts.extend([review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")])
        review_history = _load_review_history(stage_dir)
        critique_history = _render_critique_history(review_history)
        blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
        artifacts.extend(_write_guidance_artifacts(stage_dir, critique_history, blocker_ledger))
        if review.verdict == "approved":
            return list(dict.fromkeys(artifacts))
        if attempt < attempt_limit:
            context.emit_progress(f"[ainative] prd: critique requested changes, retrying - {review.summary}")
        next_attempt = attempt + 1

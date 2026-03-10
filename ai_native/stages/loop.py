from __future__ import annotations

import re
import shutil
from pathlib import Path

from ai_native.models import ReviewReport, RunState, SlicePlan
from ai_native.slice_runtime import load_slice_plan, selected_slices
from ai_native.stages.common import ExecutionContext, StageError, write_review
from ai_native.utils import read_json, read_text, write_text
from ai_native.workspace_artifacts import WORKSPACE_ARTIFACT_FILES, mirror_files, workspace_run_dir, workspace_slice_dir

ATTEMPT_RE = re.compile(r"test-review-attempt-(?P<attempt>\d+)\.json$")


def _slice_dir(state: RunState, slice_id: str) -> Path:
    return Path(state.run_dir) / "slices" / slice_id


def _agent_slice_dir(context: ExecutionContext, state: RunState, slice_id: str) -> Path:
    return workspace_slice_dir(state, slice_id, repo_root=context.repo_root)


def _target_slices(context: ExecutionContext, state: RunState, slice_plan: SlicePlan) -> list:
    return selected_slices(slice_plan, context.slice_id, state.active_slice)


def _existing_attempt_numbers(slice_dir: Path) -> list[int]:
    attempts: list[int] = []
    for review_path in slice_dir.glob("test-review-attempt-*.json"):
        match = ATTEMPT_RE.fullmatch(review_path.name)
        if not match:
            continue
        attempt = int(match.group("attempt"))
        if (slice_dir / f"builder-summary-attempt-{attempt}.md").exists():
            attempts.append(attempt)
    return sorted(attempts)


def _materialize_legacy_attempt(slice_dir: Path) -> None:
    if _existing_attempt_numbers(slice_dir):
        return
    required = [
        slice_dir / "builder-summary.md",
        slice_dir / "red.log",
        slice_dir / "green.log",
        slice_dir / "refactor-notes.md",
        slice_dir / "test-review.json",
        slice_dir / "test-review.md",
    ]
    if not all(path.exists() for path in required):
        return
    shutil.copyfile(slice_dir / "builder-summary.md", slice_dir / "builder-summary-attempt-1.md")
    shutil.copyfile(slice_dir / "red.log", slice_dir / "red-attempt-1.log")
    shutil.copyfile(slice_dir / "green.log", slice_dir / "green-attempt-1.log")
    shutil.copyfile(slice_dir / "refactor-notes.md", slice_dir / "refactor-notes-attempt-1.md")
    shutil.copyfile(slice_dir / "test-review.json", slice_dir / "test-review-attempt-1.json")
    shutil.copyfile(slice_dir / "test-review.md", slice_dir / "test-review-attempt-1.md")


def _existing_slice_artifacts(slice_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        slice_dir / "builder-summary.md",
        slice_dir / "red.log",
        slice_dir / "green.log",
        slice_dir / "refactor-notes.md",
        slice_dir / "test-review.json",
        slice_dir / "test-review.md",
        slice_dir / "critique-history.md",
        slice_dir / "blocker-ledger.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in (
        "builder-summary-attempt-*.md",
        "red-attempt-*.log",
        "green-attempt-*.log",
        "refactor-notes-attempt-*.md",
        "test-review-attempt-*.json",
        "test-review-attempt-*.md",
    ):
        paths.extend(sorted(slice_dir.glob(pattern)))
    return paths


def _load_resume_state(slice_dir: Path) -> dict[str, object] | None:
    attempts = _existing_attempt_numbers(slice_dir)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    return {
        "prior_summary": read_text(slice_dir / f"builder-summary-attempt-{last_attempt}.md"),
        "prior_review": ReviewReport.model_validate(read_json(slice_dir / f"test-review-attempt-{last_attempt}.json")),
        "last_attempt": last_attempt,
    }


def _load_review_history(slice_dir: Path) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_attempt_numbers(slice_dir):
        history.append((attempt, ReviewReport.model_validate(read_json(slice_dir / f"test-review-attempt-{attempt}.json"))))
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
                "No prior test critiques exist for this slice.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved test-quality blockers unless the revised slice resolves them explicitly.",
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
            "These are the stable test-quality blockers accumulated so far. Resolve them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(slice_dir: Path, critique_history: str, blocker_ledger: str) -> list[Path]:
    critique_history_path = slice_dir / "critique-history.md"
    blocker_ledger_path = slice_dir / "blocker-ledger.md"
    write_text(critique_history_path, critique_history)
    write_text(blocker_ledger_path, blocker_ledger)
    return [critique_history_path, blocker_ledger_path]


def _copy_attempt_artifacts(slice_dir: Path, attempt: int) -> list[Path]:
    paths: list[Path] = []
    for source_name, target_name in (
        ("builder-summary.md", f"builder-summary-attempt-{attempt}.md"),
        ("red.log", f"red-attempt-{attempt}.log"),
        ("green.log", f"green-attempt-{attempt}.log"),
        ("refactor-notes.md", f"refactor-notes-attempt-{attempt}.md"),
    ):
        source = slice_dir / source_name
        target = slice_dir / target_name
        if source.exists():
            shutil.copyfile(source, target)
            paths.append(target)
    return paths


def _render_loop_prompt(
    context: ExecutionContext,
    spec_text: str,
    slice_definition: dict[str, object],
    run_dir: str,
    slice_dir: Path,
    critique_history: str,
    blocker_ledger: str,
    prior_summary: str | None = None,
    critique: ReviewReport | None = None,
) -> str:
    if prior_summary and critique:
        return context.prompt_library.render(
            "loop_revise.md",
            spec_text=spec_text,
            slice_definition=slice_definition,
            run_dir=run_dir,
            slice_dir=slice_dir,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
            prior_summary=prior_summary,
            critique=critique.model_dump(mode="json"),
        )
    return context.prompt_library.render(
        "loop.md",
        spec_text=spec_text,
        slice_definition=slice_definition,
        run_dir=run_dir,
        slice_dir=slice_dir,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )


def _missing_output_review(slice_def_id: str, missing_files: list[str]) -> ReviewReport:
    return ReviewReport(
        verdict="changes_required",
        summary=f"Loop output missing for slice {slice_def_id}: {', '.join(missing_files)}",
        findings=[f"Expected {name} to be created by the builder." for name in missing_files],
        required_changes=[f"Create {name} with the required Ralph loop evidence." for name in missing_files],
    )


def _parse_additional_attempts(answer: str, default_attempts: int) -> int:
    raw = answer.strip()
    if not raw:
        return default_attempts
    try:
        return max(1, int(raw))
    except ValueError:
        return default_attempts


def _ask_to_continue_after_exhaustion(
    context: ExecutionContext,
    slice_id: str,
    current_limit: int,
    review: ReviewReport,
) -> int | None:
    responses = context.ask_questions(
        "loop",
        [
            (
                f"Slice {slice_id} has exhausted {current_limit} loop attempts. The latest critique summary is:\n"
                f"{review.summary}\n"
                "Continue with more loop attempts? Answer yes or no."
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
    slice_plan = load_slice_plan(Path(state.run_dir))
    artifacts: list[Path] = []
    spec_text = read_text(context.spec_path)
    review_schema = context.template_root / "ai_native" / "schemas" / "review-report.json"

    for slice_def in _target_slices(context, state, slice_plan):
        slice_dir = _slice_dir(state, slice_def.id)
        agent_slice_dir = _agent_slice_dir(context, state, slice_def.id)
        slice_dir.mkdir(parents=True, exist_ok=True)
        mirror_files(slice_dir, agent_slice_dir)
        _materialize_legacy_attempt(slice_dir)
        artifacts.extend(_existing_slice_artifacts(slice_dir))

        resume_state = _load_resume_state(slice_dir)
        review_history = _load_review_history(slice_dir)
        critique_history = _render_critique_history(review_history)
        blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
        artifacts.extend(_write_guidance_artifacts(slice_dir, critique_history, blocker_ledger))

        prior_summary = str(resume_state["prior_summary"]) if resume_state else None
        review = ReviewReport.model_validate(resume_state["prior_review"]) if resume_state else None
        if review is not None and review.verdict == "approved":
            context.emit_progress(f"[ainative] loop: slice {slice_def.id} already approved, skipping")
            state.active_slice = slice_def.id
            continue

        attempt_limit = max(1, context.config.workspace.loop_max_attempts)
        next_attempt = int(resume_state["last_attempt"]) + 1 if resume_state else 1
        if resume_state:
            context.emit_progress(
                f"[ainative] loop: slice {slice_def.id} resuming from previous critique at attempt {int(resume_state['last_attempt']) + 1}"
            )

        while True:
            if next_attempt > attempt_limit:
                if context.config.quality_gates.require_test_critique and review is not None:
                    context.emit_progress(f"[ainative] loop: slice {slice_def.id} attempt budget exhausted")
                    additional_attempts = _ask_to_continue_after_exhaustion(context, slice_def.id, attempt_limit, review)
                    if additional_attempts is None:
                        raise StageError(f"Test critique failed for slice {slice_def.id} after {attempt_limit} attempts: {review.summary}")
                    attempt_limit += additional_attempts
                    context.emit_progress(
                        f"[ainative] loop: slice {slice_def.id} continuing with {additional_attempts} additional attempts "
                        f"(new limit {attempt_limit})"
                    )
                else:
                    break

            attempt = next_attempt
            if attempt == 1 and review is None:
                context.emit_progress(f"[ainative] loop: slice {slice_def.id} synthesis attempt {attempt}/{attempt_limit}")
            else:
                context.emit_progress(f"[ainative] loop: slice {slice_def.id} revision attempt {attempt}/{attempt_limit}")

            prompt = _render_loop_prompt(
                context=context,
                spec_text=spec_text,
                slice_definition=slice_def.model_dump(mode="json"),
                run_dir=str(workspace_run_dir(state, repo_root=context.repo_root)),
                slice_dir=agent_slice_dir,
                critique_history=critique_history,
                blocker_ledger=blocker_ledger,
                prior_summary=prior_summary,
                critique=review,
            )
            builder_summary = context.builder.run(prompt, cwd=context.repo_root)
            summary_path = slice_dir / "builder-summary.md"
            write_text(summary_path, builder_summary.text or "# Builder Summary\n")
            artifacts.append(summary_path)
            artifacts.extend(mirror_files(agent_slice_dir, slice_dir))

            missing_files = [
                name
                for name in WORKSPACE_ARTIFACT_FILES
                if not (slice_dir / name).exists()
            ]
            artifacts.extend(_copy_attempt_artifacts(slice_dir, attempt))
            if missing_files:
                review = _missing_output_review(slice_def.id, missing_files)
            else:
                review_prompt = context.prompt_library.render(
                    "test_review.md",
                    spec_text=spec_text,
                    slice_definition=slice_def.model_dump(mode="json"),
                    slice_dir=agent_slice_dir,
                    critique_history=critique_history,
                    blocker_ledger=blocker_ledger,
                )
                review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
                review = ReviewReport.model_validate(review_response.json_data)

            review_md = slice_dir / "test-review.md"
            attempt_review_md = slice_dir / f"test-review-attempt-{attempt}.md"
            write_review(review_md, review)
            write_review(attempt_review_md, review)
            artifacts.extend([review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")])
            review_history = _load_review_history(slice_dir)
            critique_history = _render_critique_history(review_history)
            blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
            artifacts.extend(_write_guidance_artifacts(slice_dir, critique_history, blocker_ledger))
            if review.verdict == "approved":
                state.active_slice = slice_def.id
                break
            if attempt < attempt_limit:
                context.emit_progress(f"[ainative] loop: slice {slice_def.id} critique requested changes, retrying - {review.summary}")
            prior_summary = read_text(summary_path)
            next_attempt = attempt + 1

        if review is None or (context.config.quality_gates.require_test_critique and review.verdict != "approved"):
            raise StageError(f"Test critique failed for slice {slice_def.id}: {review.summary if review else 'unknown failure'}")

    return list(dict.fromkeys(artifacts))

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_native.models import DiagramArtifact, ReviewReport, RunState
from ai_native.specs import load_prompt_spec_text
from ai_native.stages.common import ExecutionContext, StageError, write_diagram_artifacts, write_review
from ai_native.utils import read_json, read_text, write_json

ATTEMPT_RE = re.compile(r"architecture-review-attempt-(?P<attempt>\d+)\.json$")
MERMAID_BROWSER_DEPENDENCY_ERRORS = (
    "Could not find Chrome",
    "chrome-headless-shell",
)
MERMAID_BROWSER_LAUNCH_ERRORS = (
    "Failed to launch the browser process",
    "Running as root without --no-sandbox is not supported",
    "No usable sandbox",
    "zygote_host_impl_linux.cc",
    "rosetta error",
)


def _existing_attempt_numbers(stage_dir: Path) -> list[int]:
    attempts: list[int] = []
    for review_path in stage_dir.glob("architecture-review-attempt-*.json"):
        match = ATTEMPT_RE.fullmatch(review_path.name)
        if not match:
            continue
        attempt = int(match.group("attempt"))
        if (stage_dir / f"architecture-attempt-{attempt}.json").exists():
            attempts.append(attempt)
    return sorted(attempts)


def _existing_architecture_artifacts(stage_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        stage_dir / "architecture.json",
        stage_dir / "architecture.mmd",
        stage_dir / "architecture.md",
        stage_dir / "architecture-review.json",
        stage_dir / "architecture-review.md",
        stage_dir / "validation.json",
        stage_dir / "critique-history.md",
        stage_dir / "blocker-ledger.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in (
        "architecture-attempt-*.json",
        "architecture-attempt-*.mmd",
        "architecture-attempt-*.md",
        "architecture-review-attempt-*.json",
        "architecture-review-attempt-*.md",
        "validation-attempt-*.json",
    ):
        paths.extend(sorted(stage_dir.glob(pattern)))
    return paths


def _materialize_legacy_attempt(stage_dir: Path) -> None:
    if _existing_attempt_numbers(stage_dir):
        return
    required = [
        stage_dir / "architecture.json",
        stage_dir / "architecture.mmd",
        stage_dir / "architecture.md",
        stage_dir / "architecture-review.json",
        stage_dir / "architecture-review.md",
    ]
    if not all(path.exists() for path in required):
        return
    shutil.copyfile(stage_dir / "architecture.json", stage_dir / "architecture-attempt-1.json")
    shutil.copyfile(stage_dir / "architecture.mmd", stage_dir / "architecture-attempt-1.mmd")
    shutil.copyfile(stage_dir / "architecture.md", stage_dir / "architecture-attempt-1.md")
    shutil.copyfile(stage_dir / "architecture-review.json", stage_dir / "architecture-review-attempt-1.json")
    shutil.copyfile(stage_dir / "architecture-review.md", stage_dir / "architecture-review-attempt-1.md")
    validation_path = stage_dir / "validation.json"
    if validation_path.exists():
        shutil.copyfile(validation_path, stage_dir / "validation-attempt-1.json")


def _load_resume_state(stage_dir: Path) -> dict[str, object] | None:
    attempts = _existing_attempt_numbers(stage_dir)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    return {
        "prior_artifact": DiagramArtifact.model_validate(read_json(stage_dir / f"architecture-attempt-{last_attempt}.json")),
        "prior_review": ReviewReport.model_validate(read_json(stage_dir / f"architecture-review-attempt-{last_attempt}.json")),
        "last_attempt": last_attempt,
    }


def _load_review_history(stage_dir: Path) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_attempt_numbers(stage_dir):
        history.append(
            (
                attempt,
                ReviewReport.model_validate(read_json(stage_dir / f"architecture-review-attempt-{attempt}.json")),
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
                "No prior critiques exist for this architecture run.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved architecture blockers unless the revised diagram makes them explicit or explicitly narrows scope to remove the ambiguity.",
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
            "These are the stable architecture blockers accumulated so far. Resolve or explicitly de-scope them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(stage_dir: Path, critique_history: str, blocker_ledger: str) -> list[Path]:
    critique_history_path = stage_dir / "critique-history.md"
    blocker_ledger_path = stage_dir / "blocker-ledger.md"
    critique_history_path.write_text(critique_history, encoding="utf-8")
    blocker_ledger_path.write_text(blocker_ledger, encoding="utf-8")
    return [critique_history_path, blocker_ledger_path]


def _copy_attempt_artifacts(stage_dir: Path, attempt: int) -> list[Path]:
    attempt_json = stage_dir / f"architecture-attempt-{attempt}.json"
    attempt_mmd = stage_dir / f"architecture-attempt-{attempt}.mmd"
    attempt_md = stage_dir / f"architecture-attempt-{attempt}.md"
    shutil.copyfile(stage_dir / "architecture.json", attempt_json)
    shutil.copyfile(stage_dir / "architecture.mmd", attempt_mmd)
    shutil.copyfile(stage_dir / "architecture.md", attempt_md)
    return [attempt_json, attempt_mmd, attempt_md]


def _validate_mermaid(context: ExecutionContext, diagram_path: Path) -> tuple[bool, str]:
    command = context.config.workspace.mermaid_validate_command
    if not command or shutil.which(command[0]) is None:
        return True, "Mermaid CLI not installed; validation skipped."
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "diagram.svg"
        full_command = [
            *command,
            *context.config.workspace.mermaid_validate_args,
            "-i",
            str(diagram_path),
            "-o",
            str(output_path),
        ]
        completed = subprocess.run(
            full_command,
            cwd=context.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Mermaid validation failed."
            if any(fragment in message for fragment in MERMAID_BROWSER_DEPENDENCY_ERRORS):
                return True, f"Mermaid CLI browser dependency missing; validation skipped. {message}"
            if any(fragment in message for fragment in MERMAID_BROWSER_LAUNCH_ERRORS):
                return True, f"Mermaid CLI browser launch unavailable; validation skipped. {message}"
            return False, message
        return True, "Mermaid validation passed."


def _render_architecture_prompt(
    context: ExecutionContext,
    spec_text: str,
    context_report: dict[str, object],
    plan: dict[str, object],
    critique_history: str,
    blocker_ledger: str,
    prior_artifact: DiagramArtifact | None = None,
    critique: ReviewReport | None = None,
) -> str:
    if prior_artifact and critique:
        return context.prompt_library.render(
            "architecture_revise.md",
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
            prior_architecture=prior_artifact.model_dump(mode="json"),
            critique=critique.model_dump(mode="json"),
        )
    return context.prompt_library.render(
        "architecture.md",
        spec_text=spec_text,
        context_report=context_report,
        plan=plan,
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
        "architecture",
        [
            (
                f"Architecture has exhausted {current_limit} attempts. The latest critique summary is:\n"
                f"{review.summary}\n"
                "Continue with more architecture attempts? Answer yes or no."
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
    stage_dir = context.state_store.stage_dir(state, "architecture")
    _materialize_legacy_attempt(stage_dir)
    spec_text = load_prompt_spec_text(Path(state.run_dir), context.spec_path)
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    artifacts = _existing_architecture_artifacts(stage_dir)
    resume_state = _load_resume_state(stage_dir)
    review_history = _load_review_history(stage_dir)
    critique_history = _render_critique_history(review_history)
    blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
    artifacts.extend(_write_guidance_artifacts(stage_dir, critique_history, blocker_ledger))

    schema_path = context.template_root / "schemas" / "diagram-artifact.json"
    review_schema = context.template_root / "schemas" / "review-report.json"
    attempt_limit = max(1, context.config.workspace.architecture_max_attempts)
    artifact = DiagramArtifact.model_validate(resume_state["prior_artifact"]) if resume_state else None
    review = ReviewReport.model_validate(resume_state["prior_review"]) if resume_state else None
    next_attempt = int(resume_state["last_attempt"]) + 1 if resume_state else 1

    if resume_state:
        context.emit_progress(
            f"[ainative] architecture: resuming from previous critique at attempt {int(resume_state['last_attempt']) + 1}"
        )

    while True:
        if next_attempt > attempt_limit:
            if context.config.quality_gates.require_diagram_approval and review is not None:
                context.emit_progress("[ainative] architecture: attempt budget exhausted")
                additional_attempts = _ask_to_continue_after_exhaustion(context, attempt_limit, review)
                if additional_attempts is None:
                    raise StageError(f"Architecture critique failed after {attempt_limit} attempts: {review.summary}")
                attempt_limit += additional_attempts
                context.emit_progress(
                    "[ainative] architecture: continuing with "
                    f"{additional_attempts} additional attempts (new limit {attempt_limit})"
                )
            else:
                return list(dict.fromkeys(artifacts))

        attempt = next_attempt
        if attempt == 1 and review is None:
            context.emit_progress(f"[ainative] architecture: synthesis attempt {attempt}/{attempt_limit}")
        else:
            context.emit_progress(f"[ainative] architecture: revision attempt {attempt}/{attempt_limit}")

        prompt = _render_architecture_prompt(
            context=context,
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
            prior_artifact=artifact,
            critique=review,
        )
        response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
        artifact = DiagramArtifact.model_validate(response.json_data)
        artifacts.extend(write_diagram_artifacts(stage_dir, artifact))
        artifacts.extend(_copy_attempt_artifacts(stage_dir, attempt))

        context.emit_progress("[ainative] architecture: validating mermaid")
        valid, validation_message = _validate_mermaid(context, stage_dir / "architecture.mmd")
        validation_path = stage_dir / "validation.json"
        validation_attempt_path = stage_dir / f"validation-attempt-{attempt}.json"
        validation_payload = {"valid": valid, "message": validation_message}
        write_json(validation_path, validation_payload)
        write_json(validation_attempt_path, validation_payload)
        artifacts.extend([validation_path, validation_attempt_path])
        if not valid:
            review = ReviewReport(
                verdict="changes_required",
                summary=f"Mermaid validation failed: {validation_message}",
                findings=[validation_message],
                required_changes=["Produce valid Mermaid syntax while preserving the approved architecture intent."],
            )
            review_md = stage_dir / "architecture-review.md"
            attempt_review_md = stage_dir / f"architecture-review-attempt-{attempt}.md"
            write_review(review_md, review)
            write_review(attempt_review_md, review)
            artifacts.extend([review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")])
            review_history = _load_review_history(stage_dir)
            critique_history = _render_critique_history(review_history)
            blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(review_history))
            artifacts.extend(_write_guidance_artifacts(stage_dir, critique_history, blocker_ledger))
            if attempt < attempt_limit:
                context.emit_progress(f"[ainative] architecture: validation failed, retrying - {review.summary}")
            next_attempt = attempt + 1
            continue

        review_prompt = context.prompt_library.render(
            "architecture_review.md",
            spec_text=spec_text,
            context_report=context_report,
            plan=plan,
            architecture=artifact.model_dump(mode="json"),
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
        )
        review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
        review = ReviewReport.model_validate(review_response.json_data)
        review_md = stage_dir / "architecture-review.md"
        attempt_review_md = stage_dir / f"architecture-review-attempt-{attempt}.md"
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
            context.emit_progress(f"[ainative] architecture: critique requested changes, retrying - {review.summary}")
        next_attempt = attempt + 1

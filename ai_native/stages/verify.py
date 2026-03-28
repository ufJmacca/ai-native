from __future__ import annotations

import re
import shutil
from pathlib import Path

from ai_native.browser import ImplementationCapture, capture_implementation_screenshots, preview_session
from ai_native.models import ReviewReport, RunState, VerificationReport
from ai_native.reference_workflow import (
    adapter_supports_image_inputs,
    append_reference_prompt_block,
    ensure_reference_workflow_supported,
    load_reference_context,
    load_reference_manifest_for_run,
    visual_review_prompt_block,
)
from ai_native.slice_runtime import load_slice_plan, selected_slices
from ai_native.specs import load_prompt_spec_text
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_verification_markdown, write_review
from ai_native.utils import ensure_dir, read_json, slugify, write_text
from ai_native.workspace_artifacts import mirror_files, workspace_slice_dir

VERIFY_ATTEMPT_RE = re.compile(r"(?P<slice_id>.+)-attempt-(?P<attempt>\d+)\.json$")
VISUAL_ATTEMPT_RE = re.compile(r"(?P<slice_id>.+)-visual-review-attempt-(?P<attempt>\d+)\.json$")


def _existing_verification_attempt_numbers(verification_dir: Path, slice_id: str) -> list[int]:
    attempts: list[int] = []
    for report_path in verification_dir.glob(f"{slice_id}-attempt-*.json"):
        match = VERIFY_ATTEMPT_RE.fullmatch(report_path.name)
        if not match or match.group("slice_id") != slice_id:
            continue
        attempts.append(int(match.group("attempt")))
    return sorted(attempts)


def _existing_visual_attempt_numbers(verification_dir: Path, slice_id: str) -> list[int]:
    attempts: list[int] = []
    for report_path in verification_dir.glob(f"{slice_id}-visual-review-attempt-*.json"):
        match = VISUAL_ATTEMPT_RE.fullmatch(report_path.name)
        if not match or match.group("slice_id") != slice_id:
            continue
        attempts.append(int(match.group("attempt")))
    return sorted(attempts)


def _existing_attempt_numbers(verification_dir: Path, slice_id: str) -> list[int]:
    return sorted(set(_existing_verification_attempt_numbers(verification_dir, slice_id)) | set(_existing_visual_attempt_numbers(verification_dir, slice_id)))


def _materialize_legacy_attempt(verification_dir: Path, slice_id: str) -> None:
    if _existing_verification_attempt_numbers(verification_dir, slice_id):
        return
    json_path = verification_dir / f"{slice_id}.json"
    md_path = verification_dir / f"{slice_id}.md"
    if not json_path.exists() or not md_path.exists():
        return
    shutil.copyfile(json_path, verification_dir / f"{slice_id}-attempt-1.json")
    shutil.copyfile(md_path, verification_dir / f"{slice_id}-attempt-1.md")


def _capture_dir(verification_dir: Path, slice_id: str, attempt: int) -> Path:
    return verification_dir / "visual" / slice_id / f"attempt-{attempt}"


def _existing_slice_artifacts(verification_dir: Path, slice_id: str) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        verification_dir / f"{slice_id}.json",
        verification_dir / f"{slice_id}.md",
        verification_dir / f"{slice_id}-critique-history.md",
        verification_dir / f"{slice_id}-blocker-ledger.md",
        verification_dir / f"{slice_id}-revision-summary.md",
        verification_dir / f"{slice_id}-visual-review.json",
        verification_dir / f"{slice_id}-visual-review.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in (
        f"{slice_id}-attempt-*.json",
        f"{slice_id}-attempt-*.md",
        f"{slice_id}-revision-summary-attempt-*.md",
        f"{slice_id}-visual-review-attempt-*.json",
        f"{slice_id}-visual-review-attempt-*.md",
    ):
        paths.extend(sorted(verification_dir.glob(pattern)))
    visual_root = verification_dir / "visual" / slice_id
    if visual_root.exists():
        paths.extend(sorted(path for path in visual_root.rglob("*") if path.is_file()))
    return paths


def _load_resume_state(verification_dir: Path, slice_id: str) -> dict[str, object] | None:
    attempts = _existing_attempt_numbers(verification_dir, slice_id)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    verification_path = verification_dir / f"{slice_id}-attempt-{last_attempt}.json"
    visual_path = verification_dir / f"{slice_id}-visual-review-attempt-{last_attempt}.json"
    return {
        "prior_verification": VerificationReport.model_validate(read_json(verification_path)) if verification_path.exists() else None,
        "prior_visual_review": ReviewReport.model_validate(read_json(visual_path)) if visual_path.exists() else None,
        "last_attempt": last_attempt,
    }


def _load_verification_history(verification_dir: Path, slice_id: str) -> list[tuple[int, VerificationReport]]:
    history: list[tuple[int, VerificationReport]] = []
    for attempt in _existing_verification_attempt_numbers(verification_dir, slice_id):
        history.append(
            (
                attempt,
                VerificationReport.model_validate(read_json(verification_dir / f"{slice_id}-attempt-{attempt}.json")),
            )
        )
    return history


def _load_visual_review_history(verification_dir: Path, slice_id: str) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_visual_attempt_numbers(verification_dir, slice_id):
        history.append(
            (
                attempt,
                ReviewReport.model_validate(read_json(verification_dir / f"{slice_id}-visual-review-attempt-{attempt}.json")),
            )
        )
    return history


def _normalize_blocker(text: str) -> str:
    return " ".join(text.lower().split())


def _collect_blocker_ledger(
    verification_history: list[tuple[int, VerificationReport]],
    visual_history: list[tuple[int, ReviewReport]],
) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for _attempt, review in visual_history:
        for blocker in review.required_changes:
            key = _normalize_blocker(blocker)
            if key in seen:
                continue
            seen.add(key)
            blockers.append(blocker)
    for _attempt, report in verification_history:
        for blocker in report.gaps:
            key = _normalize_blocker(blocker)
            if key in seen:
                continue
            seen.add(key)
            blockers.append(blocker)
    return blockers


def _render_critique_history(
    verification_history: list[tuple[int, VerificationReport]],
    visual_history: list[tuple[int, ReviewReport]],
) -> str:
    if not verification_history and not visual_history:
        return "\n".join(
            [
                "# Critique History",
                "",
                "No prior visual or verification failures exist for this slice.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved visual and verification blockers unless the revised slice resolves them explicitly.",
    ]
    if visual_history:
        lines.extend(["", "## Visual Review Attempts"])
        for attempt, review in visual_history:
            lines.extend(
                [
                    "",
                    f"### Attempt {attempt}",
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
    if verification_history:
        lines.extend(["", "## Verification Attempts"])
        for attempt, report in verification_history:
            lines.extend(
                [
                    "",
                    f"### Attempt {attempt}",
                    f"- Verdict: `{report.verdict}`",
                    f"- Summary: {report.summary}",
                    "",
                    "Gaps:",
                ]
            )
            if report.gaps:
                lines.extend(f"- {gap}" for gap in report.gaps)
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
            "These are the stable visual and verification blockers accumulated so far. Resolve them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(verification_dir: Path, slice_id: str, critique_history: str, blocker_ledger: str) -> list[Path]:
    critique_history_path = verification_dir / f"{slice_id}-critique-history.md"
    blocker_ledger_path = verification_dir / f"{slice_id}-blocker-ledger.md"
    write_text(critique_history_path, critique_history)
    write_text(blocker_ledger_path, blocker_ledger)
    return [critique_history_path, blocker_ledger_path]


def _render_verify_revision_prompt(
    context: ExecutionContext,
    spec_text: str,
    slice_definition: dict[str, object],
    slice_dir: Path,
    critique_history: str,
    blocker_ledger: str,
    prior_verification: VerificationReport | None,
    prior_visual_review: ReviewReport | None,
) -> str:
    prompt = context.prompt_library.render(
        "verify_revise.md",
        spec_text=spec_text,
        slice_definition=slice_definition,
        slice_dir=slice_dir,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
        verification=prior_verification.model_dump(mode="json") if prior_verification else "No prior verification report.",
    )
    prompt = append_reference_prompt_block(prompt, Path(context.run_dir))
    block = visual_review_prompt_block(prior_visual_review)
    if block:
        prompt = "\n\n".join([prompt.rstrip(), block])
    return prompt


def _render_implementation_capture_summary(captures: list[ImplementationCapture]) -> str:
    if not captures:
        return "- None"
    return "\n".join(
        f"- {capture.route} @ {capture.viewport_label} ({capture.viewport_width}x{capture.viewport_height}): {capture.path}"
        for capture in captures
    )


def _copy_reference_image_artifacts(manifest, capture_dir: Path) -> list[Path]:
    ensure_dir(capture_dir)
    copied: list[Path] = []
    for reference in manifest.references:
        if reference.kind != "image" or not reference.path:
            continue
        source = Path(reference.path)
        if not source.exists():
            continue
        target = capture_dir / f"{slugify(reference.id)}-reference{source.suffix.lower()}"
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


def _write_visual_review(verification_dir: Path, slice_id: str, attempt: int, review: ReviewReport) -> list[Path]:
    review_md = verification_dir / f"{slice_id}-visual-review.md"
    attempt_review_md = verification_dir / f"{slice_id}-visual-review-attempt-{attempt}.md"
    write_review(review_md, review)
    write_review(attempt_review_md, review)
    return [review_md, review_md.with_suffix(".json"), attempt_review_md, attempt_review_md.with_suffix(".json")]


def _run_visual_review(
    context: ExecutionContext,
    spec_text: str,
    verification_dir: Path,
    slice_definition: dict[str, object],
    attempt: int,
    critique_history: str,
    blocker_ledger: str,
) -> tuple[ReviewReport, list[Path], list[Path]]:
    manifest = load_reference_manifest_for_run(context)
    reference_context = load_reference_context(Path(context.run_dir))
    if manifest is None or reference_context is None:
        raise StageError("reference-driven verification requires recon/reference-context artifacts")
    ensure_reference_workflow_supported(manifest, context.critic, role_name="critic")

    capture_dir = _capture_dir(verification_dir, slice_definition["id"], attempt)
    with preview_session(manifest.preview, cwd=context.repo_root):
        captures = capture_implementation_screenshots(manifest.preview, manifest.references, capture_dir)
    if not captures:
        raise StageError(f"Visual review for slice {slice_definition['id']} did not produce implementation screenshots.")

    copied_references = _copy_reference_image_artifacts(manifest, capture_dir)
    prompt = context.prompt_library.render(
        "visual_review.md",
        spec_text=spec_text,
        slice_definition=slice_definition,
        reference_manifest=manifest.model_dump(mode="json"),
        reference_context=reference_context.model_dump(mode="json"),
        implementation_captures=_render_implementation_capture_summary(captures),
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )
    prompt = append_reference_prompt_block(prompt, Path(context.run_dir))
    review_schema = context.template_root / "schemas" / "review-report.json"
    image_paths = None
    if adapter_supports_image_inputs(context.critic):
        image_paths = [capture.path for capture in captures] + copied_references
    response = context.critic.run(prompt, cwd=context.repo_root, schema_path=review_schema, image_paths=image_paths)
    review = ReviewReport.model_validate(response.json_data)
    artifacts = list(capture.path for capture in captures) + copied_references + _write_visual_review(verification_dir, slice_definition["id"], attempt, review)
    return review, artifacts, [capture.path for capture in captures] + copied_references


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
    latest_summary: str,
) -> int | None:
    responses = context.ask_questions(
        "verify",
        [
            (
                f"Slice {slice_id} has exhausted {current_limit} verification attempts. The latest critique summary is:\n"
                f"{latest_summary}\n"
                "Continue with more verification attempts? Answer yes or no."
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
    verification_dir = context.state_store.stage_dir(state, "verify")
    slice_plan = load_slice_plan(Path(state.run_dir))
    artifacts: list[Path] = []
    spec_text = load_prompt_spec_text(Path(state.run_dir), context.spec_path)
    schema_path = context.template_root / "schemas" / "verification-report.json"
    reference_manifest = load_reference_manifest_for_run(context)
    reference_context = load_reference_context(Path(context.run_dir))
    if reference_manifest is not None and reference_context is None:
        raise StageError("reference-driven web workflow requires `recon/reference-context.json` before verify can run")
    reference_profile_active = reference_manifest is not None and reference_context is not None

    for slice_def in selected_slices(slice_plan, context.slice_id, state.active_slice):
        slice_dir = Path(state.run_dir) / "slices" / slice_def.id
        agent_slice_dir = workspace_slice_dir(state, slice_def.id, repo_root=context.repo_root)
        mirror_files(slice_dir, agent_slice_dir)
        _materialize_legacy_attempt(verification_dir, slice_def.id)
        artifacts.extend(_existing_slice_artifacts(verification_dir, slice_def.id))

        resume_state = _load_resume_state(verification_dir, slice_def.id)
        verification_history = _load_verification_history(verification_dir, slice_def.id)
        visual_history = _load_visual_review_history(verification_dir, slice_def.id)
        critique_history = _render_critique_history(verification_history, visual_history)
        blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(verification_history, visual_history))
        artifacts.extend(_write_guidance_artifacts(verification_dir, slice_def.id, critique_history, blocker_ledger))

        verification = (
            VerificationReport.model_validate(resume_state["prior_verification"])
            if resume_state and resume_state["prior_verification"] is not None
            else None
        )
        visual_review = (
            ReviewReport.model_validate(resume_state["prior_visual_review"])
            if resume_state and resume_state["prior_visual_review"] is not None
            else None
        )
        if verification is not None and verification.verdict == "passed":
            context.emit_progress(f"[ainative] verify: slice {slice_def.id} already passed, skipping")
            continue

        attempt_limit = max(1, context.config.workspace.verification_max_attempts)
        next_attempt = int(resume_state["last_attempt"]) + 1 if resume_state else 1
        if resume_state:
            context.emit_progress(
                f"[ainative] verify: slice {slice_def.id} resuming from previous critique at attempt {int(resume_state['last_attempt']) + 1}"
            )

        while True:
            if next_attempt > attempt_limit:
                latest_summary = verification.summary if verification is not None else (visual_review.summary if visual_review is not None else "unknown failure")
                context.emit_progress(f"[ainative] verify: slice {slice_def.id} attempt budget exhausted")
                additional_attempts = _ask_to_continue_after_exhaustion(context, slice_def.id, attempt_limit, latest_summary)
                if additional_attempts is None:
                    raise StageError(f"Verification failed for slice {slice_def.id} after {attempt_limit} attempts: {latest_summary}")
                attempt_limit += additional_attempts
                context.emit_progress(
                    f"[ainative] verify: slice {slice_def.id} continuing with {additional_attempts} additional attempts "
                    f"(new limit {attempt_limit})"
                )

            attempt = next_attempt
            if verification is None and visual_review is None:
                context.emit_progress(f"[ainative] verify: slice {slice_def.id} verification attempt {attempt}/{attempt_limit}")
            else:
                context.emit_progress(f"[ainative] verify: slice {slice_def.id} revision attempt {attempt}/{attempt_limit}")
                revise_prompt = _render_verify_revision_prompt(
                    context=context,
                    spec_text=spec_text,
                    slice_definition=slice_def.model_dump(mode="json"),
                    slice_dir=agent_slice_dir,
                    critique_history=critique_history,
                    blocker_ledger=blocker_ledger,
                    prior_verification=verification,
                    prior_visual_review=visual_review,
                )
                builder_summary = context.builder.run(revise_prompt, cwd=context.repo_root)
                artifacts.extend(mirror_files(agent_slice_dir, slice_dir))
                summary_path = verification_dir / f"{slice_def.id}-revision-summary.md"
                attempt_summary_path = verification_dir / f"{slice_def.id}-revision-summary-attempt-{attempt}.md"
                write_text(summary_path, builder_summary.text or "# Verification Revision Summary\n")
                write_text(attempt_summary_path, builder_summary.text or "# Verification Revision Summary\n")
                artifacts.extend([summary_path, attempt_summary_path])

            visual_image_paths: list[Path] = []
            if reference_profile_active:
                context.emit_progress(f"[ainative] verify: slice {slice_def.id} visual review attempt {attempt}/{attempt_limit}")
                visual_review, visual_artifacts, visual_image_paths = _run_visual_review(
                    context=context,
                    spec_text=spec_text,
                    verification_dir=verification_dir,
                    slice_definition=slice_def.model_dump(mode="json"),
                    attempt=attempt,
                    critique_history=critique_history,
                    blocker_ledger=blocker_ledger,
                )
                artifacts.extend(visual_artifacts)
                visual_history = _load_visual_review_history(verification_dir, slice_def.id)
                critique_history = _render_critique_history(verification_history, visual_history)
                blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(verification_history, visual_history))
                artifacts.extend(_write_guidance_artifacts(verification_dir, slice_def.id, critique_history, blocker_ledger))
                if visual_review.verdict != "approved":
                    verification = None
                    if attempt < attempt_limit:
                        context.emit_progress(
                            f"[ainative] verify: slice {slice_def.id} visual critique requested changes, retrying - {visual_review.summary}"
                        )
                    next_attempt = attempt + 1
                    continue

            prompt = context.prompt_library.render(
                "verify.md",
                spec_text=spec_text,
                slice_definition=slice_def.model_dump(mode="json"),
                slice_dir=agent_slice_dir,
                critique_history=critique_history,
                blocker_ledger=blocker_ledger,
            )
            prompt = append_reference_prompt_block(prompt, Path(context.run_dir))
            if visual_review is not None:
                prompt = "\n\n".join([prompt.rstrip(), visual_review_prompt_block(visual_review)])
            image_paths = visual_image_paths if visual_image_paths and adapter_supports_image_inputs(context.verifier) else None
            response = context.verifier.run(prompt, cwd=context.repo_root, schema_path=schema_path, image_paths=image_paths)
            verification = VerificationReport.model_validate(response.json_data)
            json_path = verification_dir / f"{slice_def.id}.json"
            md_path = verification_dir / f"{slice_def.id}.md"
            attempt_json_path = verification_dir / f"{slice_def.id}-attempt-{attempt}.json"
            attempt_md_path = verification_dir / f"{slice_def.id}-attempt-{attempt}.md"
            dump_model(json_path, verification)
            write_text(md_path, render_verification_markdown(verification))
            dump_model(attempt_json_path, verification)
            write_text(attempt_md_path, render_verification_markdown(verification))
            artifacts.extend([json_path, md_path, attempt_json_path, attempt_md_path])
            verification_history = _load_verification_history(verification_dir, slice_def.id)
            visual_history = _load_visual_review_history(verification_dir, slice_def.id)
            critique_history = _render_critique_history(verification_history, visual_history)
            blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(verification_history, visual_history))
            artifacts.extend(_write_guidance_artifacts(verification_dir, slice_def.id, critique_history, blocker_ledger))
            if verification.verdict == "passed":
                break
            if attempt < attempt_limit:
                context.emit_progress(
                    f"[ainative] verify: slice {slice_def.id} verification requested changes, retrying - {verification.summary}"
                )
            next_attempt = attempt + 1

        if verification is None or verification.verdict != "passed":
            raise StageError(
                f"Verification failed for slice {slice_def.id}: {verification.summary if verification else 'unknown failure'}"
            )

    return list(dict.fromkeys(artifacts))

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ai_native.models import RunState, SlicePlan, VerificationReport
from ai_native.slice_runtime import load_slice_plan, selected_slices
from ai_native.stages.common import ExecutionContext, StageError, dump_model, render_verification_markdown
from ai_native.utils import read_json, read_text, write_text
from ai_native.workspace_artifacts import mirror_files, workspace_slice_dir

ATTEMPT_RE = re.compile(r"(?P<slice_id>.+)-attempt-(?P<attempt>\d+)\.json$")


def _existing_attempt_numbers(verification_dir: Path, slice_id: str) -> list[int]:
    attempts: list[int] = []
    for report_path in verification_dir.glob(f"{slice_id}-attempt-*.json"):
        match = ATTEMPT_RE.fullmatch(report_path.name)
        if not match or match.group("slice_id") != slice_id:
            continue
        attempts.append(int(match.group("attempt")))
    return sorted(attempts)


def _materialize_legacy_attempt(verification_dir: Path, slice_id: str) -> None:
    if _existing_attempt_numbers(verification_dir, slice_id):
        return
    json_path = verification_dir / f"{slice_id}.json"
    md_path = verification_dir / f"{slice_id}.md"
    if not json_path.exists() or not md_path.exists():
        return
    shutil.copyfile(json_path, verification_dir / f"{slice_id}-attempt-1.json")
    shutil.copyfile(md_path, verification_dir / f"{slice_id}-attempt-1.md")


def _existing_slice_artifacts(verification_dir: Path, slice_id: str) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        verification_dir / f"{slice_id}.json",
        verification_dir / f"{slice_id}.md",
        verification_dir / f"{slice_id}-critique-history.md",
        verification_dir / f"{slice_id}-blocker-ledger.md",
        verification_dir / f"{slice_id}-revision-summary.md",
    ):
        if candidate.exists():
            paths.append(candidate)
    for pattern in (f"{slice_id}-attempt-*.json", f"{slice_id}-attempt-*.md", f"{slice_id}-revision-summary-attempt-*.md"):
        paths.extend(sorted(verification_dir.glob(pattern)))
    return paths


def _load_resume_state(verification_dir: Path, slice_id: str) -> dict[str, object] | None:
    attempts = _existing_attempt_numbers(verification_dir, slice_id)
    if not attempts:
        return None
    last_attempt = attempts[-1]
    return {
        "prior_verification": VerificationReport.model_validate(read_json(verification_dir / f"{slice_id}-attempt-{last_attempt}.json")),
        "last_attempt": last_attempt,
    }


def _load_verification_history(verification_dir: Path, slice_id: str) -> list[tuple[int, VerificationReport]]:
    history: list[tuple[int, VerificationReport]] = []
    for attempt in _existing_attempt_numbers(verification_dir, slice_id):
        history.append(
            (
                attempt,
                VerificationReport.model_validate(read_json(verification_dir / f"{slice_id}-attempt-{attempt}.json")),
            )
        )
    return history


def _normalize_blocker(text: str) -> str:
    return " ".join(text.lower().split())


def _collect_blocker_ledger(history: list[tuple[int, VerificationReport]]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for _attempt, report in history:
        for blocker in report.gaps:
            key = _normalize_blocker(blocker)
            if key in seen:
                continue
            seen.add(key)
            blockers.append(blocker)
    return blockers


def _render_critique_history(history: list[tuple[int, VerificationReport]]) -> str:
    if not history:
        return "\n".join(
            [
                "# Critique History",
                "",
                "No prior verification failures exist for this slice.",
            ]
        )
    lines = [
        "# Critique History",
        "",
        "Carry forward unresolved verification gaps unless the revised slice resolves them explicitly.",
    ]
    for attempt, report in history:
        lines.extend(
            [
                "",
                f"## Attempt {attempt}",
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
            "These are the stable verification blockers accumulated so far. Resolve them instead of silently bypassing them.",
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
    prior_verification: VerificationReport,
) -> str:
    return context.prompt_library.render(
        "verify_revise.md",
        spec_text=spec_text,
        slice_definition=slice_definition,
        slice_dir=slice_dir,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
        verification=prior_verification.model_dump(mode="json"),
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
    verification: VerificationReport,
) -> int | None:
    responses = context.ask_questions(
        "verify",
        [
            (
                f"Slice {slice_id} has exhausted {current_limit} verification attempts. The latest verification summary is:\n"
                f"{verification.summary}\n"
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
    spec_text = read_text(context.spec_path)
    schema_path = context.template_root / "ai_native" / "schemas" / "verification-report.json"

    for slice_def in selected_slices(slice_plan, context.slice_id, state.active_slice):
        slice_dir = Path(state.run_dir) / "slices" / slice_def.id
        agent_slice_dir = workspace_slice_dir(state, slice_def.id, repo_root=context.repo_root)
        mirror_files(slice_dir, agent_slice_dir)
        _materialize_legacy_attempt(verification_dir, slice_def.id)
        artifacts.extend(_existing_slice_artifacts(verification_dir, slice_def.id))

        resume_state = _load_resume_state(verification_dir, slice_def.id)
        history = _load_verification_history(verification_dir, slice_def.id)
        critique_history = _render_critique_history(history)
        blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(history))
        artifacts.extend(_write_guidance_artifacts(verification_dir, slice_def.id, critique_history, blocker_ledger))

        verification = VerificationReport.model_validate(resume_state["prior_verification"]) if resume_state else None
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
                if verification is not None:
                    context.emit_progress(f"[ainative] verify: slice {slice_def.id} attempt budget exhausted")
                    additional_attempts = _ask_to_continue_after_exhaustion(context, slice_def.id, attempt_limit, verification)
                    if additional_attempts is None:
                        raise StageError(
                            f"Verification failed for slice {slice_def.id} after {attempt_limit} attempts: {verification.summary}"
                        )
                    attempt_limit += additional_attempts
                    context.emit_progress(
                        f"[ainative] verify: slice {slice_def.id} continuing with {additional_attempts} additional attempts "
                        f"(new limit {attempt_limit})"
                    )
                else:
                    break

            attempt = next_attempt
            if verification is None:
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
                )
                builder_summary = context.builder.run(revise_prompt, cwd=context.repo_root)
                artifacts.extend(mirror_files(agent_slice_dir, slice_dir))
                summary_path = verification_dir / f"{slice_def.id}-revision-summary.md"
                attempt_summary_path = verification_dir / f"{slice_def.id}-revision-summary-attempt-{attempt}.md"
                write_text(summary_path, builder_summary.text or "# Verification Revision Summary\n")
                write_text(attempt_summary_path, builder_summary.text or "# Verification Revision Summary\n")
                artifacts.extend([summary_path, attempt_summary_path])

            prompt = context.prompt_library.render(
                "verify.md",
                spec_text=spec_text,
                slice_definition=slice_def.model_dump(mode="json"),
                slice_dir=agent_slice_dir,
                critique_history=critique_history,
                blocker_ledger=blocker_ledger,
            )
            response = context.verifier.run(prompt, cwd=context.repo_root, schema_path=schema_path)
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
            history = _load_verification_history(verification_dir, slice_def.id)
            critique_history = _render_critique_history(history)
            blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(history))
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

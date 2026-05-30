from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ai_native.gitops import (
    amend_all,
    commit_all,
    create_pull_request,
    ensure_branch,
    has_changes,
    push_branch,
)
from ai_native.models import ReviewReport, RunState, SliceDefinition, SlicePlan
from ai_native.slice_runtime import (
    branch_name_for_slice,
    load_slice_plan,
    selected_slices,
)
from ai_native.stages.common import ExecutionContext, StageError, write_review
from ai_native.stages.verify import run as run_verify
from ai_native.utils import read_json, read_text, write_text
from ai_native.workspace_artifacts import mirror_files, workspace_slice_dir

PR_REVIEW_ATTEMPT_RE = re.compile(
    r"(?P<slice_id>.+)-review-report-attempt-(?P<attempt>\d+)\.json$"
)


def _target_slice(
    context: ExecutionContext, state: RunState, slice_plan: SlicePlan
) -> SliceDefinition:
    slices = selected_slices(slice_plan, context.slice_id, state.active_slice)
    if not slices:
        raise RuntimeError("No slices were generated for this run.")
    return slices[-1]


def _target_slices(
    context: ExecutionContext, state: RunState, slice_plan: SlicePlan
) -> list[SliceDefinition]:
    return selected_slices(slice_plan, context.slice_id, state.active_slice)


def _transitive_dependencies(
    slice_plan: SlicePlan, slice_id: str, seen: set[str] | None = None
) -> set[str]:
    if seen is None:
        seen = set()
    if slice_id in seen:
        return set()
    seen.add(slice_id)
    by_id = {slice_def.id: slice_def for slice_def in slice_plan.slices}
    slice_def = by_id.get(slice_id)
    if slice_def is None:
        return set()
    resolved: set[str] = set(slice_def.dependencies)
    for dependency_id in slice_def.dependencies:
        resolved.update(_transitive_dependencies(slice_plan, dependency_id, seen))
    return resolved


def _pr_base_branch(
    context: ExecutionContext,
    state: RunState,
    slice_plan: SlicePlan,
    slice_def: SliceDefinition,
) -> str:
    if not slice_def.dependencies:
        return context.config.workspace.base_branch

    dependency_ids = slice_def.dependencies
    dependency_closure = {
        dependency_id: _transitive_dependencies(slice_plan, dependency_id)
        for dependency_id in dependency_ids
    }
    for dependency_id in reversed(dependency_ids):
        closure = dependency_closure[dependency_id]
        if all(
            other_id == dependency_id or other_id in closure
            for other_id in dependency_ids
        ):
            dependency_state = state.slice_states.get(dependency_id)
            if dependency_state and dependency_state.branch_name:
                return dependency_state.branch_name
            return branch_name_for_slice(
                context.config.git.branch_prefix, state.feature_slug, dependency_id
            )
    return context.config.workspace.base_branch


def _commit_artifact_path(
    context: ExecutionContext, state: RunState, slice_id: str
) -> Path:
    return context.state_store.stage_dir(state, "commit") / f"{slice_id}.txt"


def _commit_message(
    slice_def: SliceDefinition, slice_dir: Path, conventional_prefix: str
) -> tuple[str, str]:
    subject = f"{conventional_prefix}: {slice_def.name} [{slice_def.id}]"
    summary = ""
    summary_path = slice_dir / "builder-summary.md"
    if summary_path.exists():
        for line in read_text(summary_path).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            summary = stripped
            break
    acceptance_lines = [f"- {item}" for item in slice_def.acceptance_criteria] or [
        "- None"
    ]
    file_impact_lines = [f"- {item}" for item in slice_def.file_impact] or ["- None"]
    body_lines = [
        f"Goal: {slice_def.goal}",
        "",
        "Acceptance Criteria:",
        *acceptance_lines,
        "",
        "File Impact:",
        *file_impact_lines,
    ]
    if summary:
        body_lines = [summary, "", *body_lines]
    return subject, "\n".join(body_lines)


def _review_prompt_with_base_branch(prompt: str, base_branch: str | None) -> str:
    if not base_branch:
        return prompt
    return "\n\n".join(
        [
            f"Review the current branch against the git base branch `{base_branch}`.",
            prompt.rstrip(),
        ]
    )


def _existing_pr_review_attempt_numbers(pr_dir: Path, slice_id: str) -> list[int]:
    attempts: list[int] = []
    for report_path in pr_dir.glob(f"{slice_id}-review-report-attempt-*.json"):
        match = PR_REVIEW_ATTEMPT_RE.fullmatch(report_path.name)
        if not match or match.group("slice_id") != slice_id:
            continue
        attempts.append(int(match.group("attempt")))
    return sorted(attempts)


def _load_pr_review_history(
    pr_dir: Path, slice_id: str
) -> list[tuple[int, ReviewReport]]:
    history: list[tuple[int, ReviewReport]] = []
    for attempt in _existing_pr_review_attempt_numbers(pr_dir, slice_id):
        history.append(
            (
                attempt,
                ReviewReport.model_validate(
                    read_json(
                        pr_dir / f"{slice_id}-review-report-attempt-{attempt}.json"
                    )
                ),
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
                "# PR Critique History",
                "",
                "No prior PR review triage attempts exist for this slice.",
            ]
        )
    lines = [
        "# PR Critique History",
        "",
        "Carry forward unresolved PR review blockers unless the repaired slice resolves them explicitly.",
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
                "# PR Blocker Ledger",
                "",
                "No carried-forward PR review blockers exist yet.",
            ]
        )
    return "\n".join(
        [
            "# PR Blocker Ledger",
            "",
            "These are the stable PR review blockers accumulated so far. Resolve them instead of silently bypassing them.",
            "",
            *[f"- {blocker}" for blocker in blockers],
        ]
    )


def _write_guidance_artifacts(
    pr_dir: Path, slice_id: str, critique_history: str, blocker_ledger: str
) -> list[Path]:
    critique_history_path = pr_dir / f"{slice_id}-critique-history.md"
    blocker_ledger_path = pr_dir / f"{slice_id}-blocker-ledger.md"
    write_text(critique_history_path, critique_history)
    write_text(blocker_ledger_path, blocker_ledger)
    return [critique_history_path, blocker_ledger_path]


def _review_history_artifacts(
    pr_dir: Path, slice_id: str
) -> tuple[list[tuple[int, ReviewReport]], str, str, list[Path]]:
    history = _load_pr_review_history(pr_dir, slice_id)
    critique_history = _render_critique_history(history)
    blocker_ledger = _render_blocker_ledger(_collect_blocker_ledger(history))
    return (
        history,
        critique_history,
        blocker_ledger,
        _write_guidance_artifacts(pr_dir, slice_id, critique_history, blocker_ledger),
    )


def _write_raw_review(
    pr_dir: Path, slice_id: str, attempt: int, review_text: str
) -> list[Path]:
    review_path = pr_dir / f"{slice_id}-review.md"
    attempt_review_path = pr_dir / f"{slice_id}-review-attempt-{attempt}.md"
    write_text(review_path, review_text)
    write_text(attempt_review_path, review_text)
    return [review_path, attempt_review_path]


def _write_pr_review_report(
    pr_dir: Path, slice_id: str, attempt: int, review: ReviewReport
) -> list[Path]:
    review_md = pr_dir / f"{slice_id}-review-report.md"
    attempt_review_md = pr_dir / f"{slice_id}-review-report-attempt-{attempt}.md"
    write_review(review_md, review)
    write_review(attempt_review_md, review)
    return [
        review_md,
        review_md.with_suffix(".json"),
        attempt_review_md,
        attempt_review_md.with_suffix(".json"),
    ]


def _run_pr_review(
    context: ExecutionContext,
    pr_dir: Path,
    slice_id: str,
    attempt: int,
    review_prompt: str,
    pr_base: str,
) -> tuple[str, list[Path]]:
    review_method = getattr(context.pr_reviewer, "review", None)
    if callable(review_method):
        review_text = review_method(
            cwd=context.repo_root, prompt=review_prompt, base_branch=pr_base
        ).text
    else:
        review_text = context.pr_reviewer.run(
            _review_prompt_with_base_branch(review_prompt, pr_base),
            cwd=context.repo_root,
        ).text
    return review_text, _write_raw_review(pr_dir, slice_id, attempt, review_text)


def _triage_pr_review(
    context: ExecutionContext,
    slice_def: SliceDefinition,
    pr_body: str,
    prd: dict[str, object],
    raw_review: str,
    critique_history: str,
    blocker_ledger: str,
) -> ReviewReport:
    prompt = context.prompt_library.render(
        "pr_review_triage.md",
        slice_definition=slice_def.model_dump(mode="json"),
        pr_body=pr_body,
        prd=prd,
        raw_review=raw_review,
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )
    response = context.critic.run(
        prompt,
        cwd=context.repo_root,
        schema_path=context.template_root / "schemas" / "review-report.json",
    )
    payload = (
        response.json_data
        if response.json_data is not None
        else json.loads(response.text)
    )
    return ReviewReport.model_validate(payload)


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
        "pr",
        [
            (
                f"Slice {slice_id} has exhausted {current_limit} PR review attempts. The latest triage summary is:\n"
                f"{review.summary}\n"
                "Continue with more PR review repair attempts? Answer yes or no."
            ),
            f"If yes, how many additional attempts should be allowed? Press Enter to use {current_limit}.",
        ],
    )
    if not responses:
        return None
    if responses[0].strip().lower() not in {"y", "yes"}:
        return None
    return _parse_additional_attempts(
        responses[1] if len(responses) > 1 else "", current_limit
    )


def _write_repair_summary(
    pr_dir: Path, slice_id: str, attempt: int, summary: str
) -> list[Path]:
    summary_path = pr_dir / f"{slice_id}-repair-summary.md"
    attempt_summary_path = pr_dir / f"{slice_id}-repair-summary-attempt-{attempt}.md"
    write_text(summary_path, summary)
    write_text(attempt_summary_path, summary)
    return [summary_path, attempt_summary_path]


def _run_pr_repair(
    context: ExecutionContext,
    state: RunState,
    pr_dir: Path,
    slice_def: SliceDefinition,
    pr_body: str,
    prd: dict[str, object],
    raw_review: str,
    review: ReviewReport,
    critique_history: str,
    blocker_ledger: str,
    attempt: int,
) -> list[Path]:
    slice_dir = Path(state.run_dir) / "slices" / slice_def.id
    agent_slice_dir = workspace_slice_dir(
        state, slice_def.id, repo_root=context.repo_root
    )
    mirror_files(slice_dir, agent_slice_dir)
    prompt = context.prompt_library.render(
        "pr_repair.md",
        slice_dir=agent_slice_dir,
        slice_definition=slice_def.model_dump(mode="json"),
        pr_body=pr_body,
        prd=prd,
        raw_review=raw_review,
        review_report=review.model_dump(mode="json"),
        critique_history=critique_history,
        blocker_ledger=blocker_ledger,
    )
    result = context.builder.run(prompt, cwd=context.repo_root)
    artifacts = mirror_files(agent_slice_dir, slice_dir)
    summary = result.text or "# PR Repair Summary\n"
    artifacts.extend(_write_repair_summary(pr_dir, slice_def.id, attempt, summary))
    return artifacts


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.name}-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _move_to_archive(source: Path, archive_dir: Path) -> list[Path]:
    target = _unique_path(archive_dir / source.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    if target.is_dir():
        return sorted(path for path in target.rglob("*") if path.is_file())
    return [target]


def _archive_verification_artifacts_for_pr_repair(
    context: ExecutionContext,
    state: RunState,
    slice_id: str,
    attempt: int,
) -> list[Path]:
    verification_dir = context.state_store.stage_dir(state, "verify")
    sources: list[Path] = []
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
            sources.append(candidate)
    for pattern in (
        f"{slice_id}-attempt-*.json",
        f"{slice_id}-attempt-*.md",
        f"{slice_id}-revision-summary-attempt-*.md",
        f"{slice_id}-visual-review-attempt-*.json",
        f"{slice_id}-visual-review-attempt-*.md",
    ):
        sources.extend(
            sorted(path for path in verification_dir.glob(pattern) if path.exists())
        )
    visual_root = verification_dir / "visual" / slice_id
    if visual_root.exists():
        sources.append(visual_root)
    if not sources:
        return []

    pr_dir = context.state_store.stage_dir(state, "pr")
    archive_dir = _unique_path(
        pr_dir / f"{slice_id}-verification-before-pr-repair-attempt-{attempt}"
    )
    artifacts: list[Path] = []
    for source in sources:
        artifacts.extend(_move_to_archive(source, archive_dir))
    note_path = archive_dir / "README.md"
    write_text(
        note_path,
        "\n".join(
            [
                "# Archived Verification Artifacts",
                "",
                "These verification artifacts were archived before rerunning verification for a PR-stage repair.",
                "Archiving them prevents the verify stage from treating a previous passed report as still valid after repair edits.",
                "",
            ]
        ),
    )
    artifacts.append(note_path)
    return artifacts


def _rewrite_commit_artifact_sha(commit_path: Path, sha: str) -> None:
    if not commit_path.exists():
        write_text(commit_path, sha + "\n")
        return
    lines = read_text(commit_path).splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            lines[index] = sha
            break
    else:
        lines = [sha]
    write_text(commit_path, "\n".join(lines) + "\n")


def amend_slice_commit(
    context: ExecutionContext,
    state: RunState,
    slice_def: SliceDefinition,
    branch_name: str,
) -> list[Path]:
    if not has_changes(context.repo_root):
        return []
    ensure_branch(context.repo_root, branch_name)
    sha = amend_all(context.repo_root)
    commit_path = _commit_artifact_path(context, state, slice_def.id)
    _rewrite_commit_artifact_sha(commit_path, sha)
    slice_state = state.slice_states.get(slice_def.id)
    if slice_state is not None:
        slice_state.commit_sha = sha
    return [commit_path]


def _dry_run_repair_summary(review: ReviewReport) -> str:
    changes = "\n".join(f"- {change}" for change in review.required_changes) or "- None"
    return "\n".join(
        [
            "# PR Repair Summary",
            "",
            "Dry-run PR review found blocking changes. No repository edits, verification rerun, commit amend, push, or PR creation were performed.",
            "",
            "Required changes:",
            changes,
            "",
        ]
    )


def _run_gated_pr_review(
    context: ExecutionContext,
    state: RunState,
    pr_dir: Path,
    slice_def: SliceDefinition,
    pr_body: str,
    prd: dict[str, object],
    pr_base: str,
    review_prompt: str,
    branch_name: str,
    dry_run: bool,
) -> list[Path]:
    artifacts: list[Path] = []
    attempt_limit = max(1, context.config.workspace.pr_review_max_attempts)
    attempt = (
        max(_existing_pr_review_attempt_numbers(pr_dir, slice_def.id), default=0) + 1
    )
    latest_review: ReviewReport | None = None

    while True:
        if attempt > attempt_limit:
            if latest_review is None:
                raise StageError(
                    f"PR review failed for slice {slice_def.id} before any review attempt could run."
                )
            context.emit_progress(
                f"[ainative] pr: slice {slice_def.id} attempt budget exhausted"
            )
            additional_attempts = _ask_to_continue_after_exhaustion(
                context, slice_def.id, attempt_limit, latest_review
            )
            if additional_attempts is None:
                raise StageError(
                    f"PR review failed for slice {slice_def.id} after {attempt_limit} attempts: {latest_review.summary}"
                )
            attempt_limit += additional_attempts
            context.emit_progress(
                f"[ainative] pr: slice {slice_def.id} continuing with {additional_attempts} additional attempts "
                f"(new limit {attempt_limit})"
            )

        _, critique_history, blocker_ledger, guidance_artifacts = (
            _review_history_artifacts(pr_dir, slice_def.id)
        )
        artifacts.extend(guidance_artifacts)
        context.emit_progress(
            f"[ainative] pr: slice {slice_def.id} review attempt {attempt}/{attempt_limit}"
        )
        raw_review, raw_artifacts = _run_pr_review(
            context=context,
            pr_dir=pr_dir,
            slice_id=slice_def.id,
            attempt=attempt,
            review_prompt=review_prompt,
            pr_base=pr_base,
        )
        artifacts.extend(raw_artifacts)
        latest_review = _triage_pr_review(
            context=context,
            slice_def=slice_def,
            pr_body=pr_body,
            prd=prd,
            raw_review=raw_review,
            critique_history=critique_history,
            blocker_ledger=blocker_ledger,
        )
        artifacts.extend(
            _write_pr_review_report(pr_dir, slice_def.id, attempt, latest_review)
        )
        _, critique_history, blocker_ledger, guidance_artifacts = (
            _review_history_artifacts(pr_dir, slice_def.id)
        )
        artifacts.extend(guidance_artifacts)
        if latest_review.verdict == "approved":
            return list(dict.fromkeys(artifacts))

        context.emit_progress(
            f"[ainative] pr: slice {slice_def.id} review requested changes - {latest_review.summary}"
        )
        if dry_run:
            artifacts.extend(
                _write_repair_summary(
                    pr_dir,
                    slice_def.id,
                    attempt,
                    _dry_run_repair_summary(latest_review),
                )
            )
            raise StageError(
                f"PR review found required changes for slice {slice_def.id} during dry run: {latest_review.summary}"
            )

        if attempt >= attempt_limit:
            context.emit_progress(
                f"[ainative] pr: slice {slice_def.id} attempt budget exhausted"
            )
            additional_attempts = _ask_to_continue_after_exhaustion(
                context, slice_def.id, attempt_limit, latest_review
            )
            if additional_attempts is None:
                raise StageError(
                    f"PR review failed for slice {slice_def.id} after {attempt_limit} attempts: {latest_review.summary}"
                )
            attempt_limit += additional_attempts
            context.emit_progress(
                f"[ainative] pr: slice {slice_def.id} continuing with {additional_attempts} additional attempts "
                f"(new limit {attempt_limit})"
            )

        context.emit_progress(
            f"[ainative] pr: slice {slice_def.id} repair attempt {attempt}/{attempt_limit}"
        )
        artifacts.extend(
            _run_pr_repair(
                context=context,
                state=state,
                pr_dir=pr_dir,
                slice_def=slice_def,
                pr_body=pr_body,
                prd=prd,
                raw_review=raw_review,
                review=latest_review,
                critique_history=critique_history,
                blocker_ledger=blocker_ledger,
                attempt=attempt,
            )
        )
        context.emit_progress(
            f"[ainative] pr: slice {slice_def.id} re-running verification after PR repair"
        )
        verify_state = state.model_copy(deep=True)
        verify_state.active_slice = slice_def.id
        artifacts.extend(
            _archive_verification_artifacts_for_pr_repair(
                context=context,
                state=state,
                slice_id=slice_def.id,
                attempt=attempt,
            )
        )
        artifacts.extend(run_verify(context, verify_state))
        artifacts.extend(amend_slice_commit(context, state, slice_def, branch_name))
        attempt += 1


def commit_slice(
    context: ExecutionContext, state: RunState, slice_def: SliceDefinition
) -> list[Path]:
    commit_path = _commit_artifact_path(context, state, slice_def.id)
    if commit_path.exists():
        return [commit_path]
    if not has_changes(context.repo_root):
        return []

    slice_dir = Path(state.run_dir) / "slices" / slice_def.id
    subject, body = _commit_message(
        slice_def, slice_dir, context.config.git.conventional_prefix
    )
    branch_name = branch_name_for_slice(
        context.config.git.branch_prefix, state.feature_slug, slice_def.id
    )
    ensure_branch(context.repo_root, branch_name)
    sha = commit_all(context.repo_root, subject, body)
    write_text(commit_path, f"{subject}\n\n{body}\n\n{sha}\n")
    return [commit_path]


def commit_run(context: ExecutionContext, state: RunState) -> list[Path]:
    slice_plan = load_slice_plan(Path(state.run_dir))
    artifacts: list[Path] = []
    for slice_def in _target_slices(context, state, slice_plan):
        artifacts.extend(commit_slice(context, state, slice_def))
    return artifacts


def create_prs(
    context: ExecutionContext, state: RunState, dry_run: bool = False
) -> list[Path]:
    pr_dir = context.state_store.stage_dir(state, "pr")
    slice_plan = load_slice_plan(Path(state.run_dir))
    slice_def = _target_slice(context, state, slice_plan)
    prd = read_json(Path(state.run_dir) / "prd" / "prd.json")
    artifacts: list[Path] = []
    branch_name = branch_name_for_slice(
        context.config.git.branch_prefix, state.feature_slug, slice_def.id
    )
    body_path = pr_dir / f"{slice_def.id}-body.md"
    body = "\n".join(
        [
            f"# {slice_def.id}: {slice_def.name}",
            "",
            "## Goal",
            slice_def.goal,
            "",
            "## Acceptance Criteria",
            "\n".join(f"- {item}" for item in slice_def.acceptance_criteria)
            or "- None",
            "",
            "## PRD Summary",
            prd.get("user_value", ""),
        ]
    )
    write_text(body_path, body)
    artifacts.append(body_path)

    review_prompt = context.prompt_library.render(
        "pr_review.md",
        slice_definition=slice_def.model_dump(mode="json"),
        pr_body=body,
        prd=prd,
    )
    pr_base = _pr_base_branch(context, state, slice_plan, slice_def)
    if context.config.quality_gates.require_pr_review_approval:
        artifacts.extend(
            _run_gated_pr_review(
                context=context,
                state=state,
                pr_dir=pr_dir,
                slice_def=slice_def,
                pr_body=body,
                prd=prd,
                pr_base=pr_base,
                review_prompt=review_prompt,
                branch_name=branch_name,
                dry_run=dry_run,
            )
        )
    else:
        _, review_artifacts = _run_pr_review(
            context=context,
            pr_dir=pr_dir,
            slice_id=slice_def.id,
            attempt=1,
            review_prompt=review_prompt,
            pr_base=pr_base,
        )
        artifacts.extend(review_artifacts)

    if not dry_run:
        ensure_branch(context.repo_root, branch_name)
        push_branch(context.repo_root, branch_name)
        title = f"{slice_def.id}: {slice_def.name}"
        pr_url = create_pull_request(
            context.repo_root,
            title,
            body_path,
            context.config.git.pr_draft,
            base_branch=pr_base,
        )
        url_path = pr_dir / f"{slice_def.id}-url.txt"
        write_text(url_path, pr_url + "\n")
        artifacts.append(url_path)
    return artifacts

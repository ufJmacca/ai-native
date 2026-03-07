from __future__ import annotations

from pathlib import Path

from ai_native.gitops import commit_all, create_pull_request, ensure_branch, push_branch
from ai_native.models import RunState, SliceDefinition, SlicePlan
from ai_native.stages.common import ExecutionContext
from ai_native.utils import read_json, write_text


def _target_slice(state: RunState, slice_plan: SlicePlan) -> SliceDefinition:
    if state.active_slice:
        for slice_def in slice_plan.slices:
            if slice_def.id == state.active_slice:
                return slice_def
    if not slice_plan.slices:
        raise RuntimeError("No slices were generated for this run.")
    return slice_plan.slices[-1]


def commit_run(context: ExecutionContext, state: RunState) -> list[Path]:
    commit_dir = context.state_store.stage_dir(state, "commit")
    slice_plan = SlicePlan.model_validate(read_json(Path(state.run_dir) / "slice" / "slices.json"))
    slice_def = _target_slice(state, slice_plan)
    artifacts: list[Path] = []
    branch_name = f"{context.config.git.branch_prefix}/{state.feature_slug}-{slice_def.id}"
    ensure_branch(context.repo_root, branch_name)
    message = f"{context.config.git.conventional_prefix}: {state.feature_slug} [{slice_def.id}]"
    sha = commit_all(context.repo_root, message)
    commit_path = commit_dir / f"{slice_def.id}.txt"
    write_text(commit_path, f"{message}\n{sha}\n")
    artifacts.append(commit_path)
    return artifacts


def create_prs(context: ExecutionContext, state: RunState, dry_run: bool = False) -> list[Path]:
    pr_dir = context.state_store.stage_dir(state, "pr")
    slice_plan = SlicePlan.model_validate(read_json(Path(state.run_dir) / "slice" / "slices.json"))
    slice_def = _target_slice(state, slice_plan)
    prd = read_json(Path(state.run_dir) / "prd" / "prd.json")
    artifacts: list[Path] = []
    branch_name = f"{context.config.git.branch_prefix}/{state.feature_slug}-{slice_def.id}"
    body_path = pr_dir / f"{slice_def.id}-body.md"
    body = "\n".join(
        [
            f"# {slice_def.id}: {slice_def.name}",
            "",
            "## Goal",
            slice_def.goal,
            "",
            "## Acceptance Criteria",
            "\n".join(f"- {item}" for item in slice_def.acceptance_criteria) or "- None",
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
    review_text = context.pr_reviewer.run(review_prompt, cwd=context.repo_root).text
    review_path = pr_dir / f"{slice_def.id}-review.md"
    write_text(review_path, review_text)
    artifacts.append(review_path)

    if not dry_run:
        ensure_branch(context.repo_root, branch_name)
        push_branch(context.repo_root, branch_name)
        title = f"{slice_def.id}: {slice_def.name}"
        pr_url = create_pull_request(context.repo_root, title, body_path, context.config.git.pr_draft)
        url_path = pr_dir / f"{slice_def.id}-url.txt"
        write_text(url_path, pr_url + "\n")
        artifacts.append(url_path)
    return artifacts

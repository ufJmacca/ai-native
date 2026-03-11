from __future__ import annotations

from pathlib import Path

from ai_native.gitops import commit_all, create_pull_request, ensure_branch, has_changes, push_branch
from ai_native.models import RunState, SliceDefinition, SlicePlan
from ai_native.slice_runtime import branch_name_for_slice, load_slice_plan, selected_slices
from ai_native.stages.common import ExecutionContext
from ai_native.utils import read_json, read_text, write_text


def _target_slice(context: ExecutionContext, state: RunState, slice_plan: SlicePlan) -> SliceDefinition:
    slices = selected_slices(slice_plan, context.slice_id, state.active_slice)
    if not slices:
        raise RuntimeError("No slices were generated for this run.")
    return slices[-1]


def _target_slices(context: ExecutionContext, state: RunState, slice_plan: SlicePlan) -> list[SliceDefinition]:
    return selected_slices(slice_plan, context.slice_id, state.active_slice)


def _transitive_dependencies(slice_plan: SlicePlan, slice_id: str, seen: set[str] | None = None) -> set[str]:
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


def _pr_base_branch(context: ExecutionContext, state: RunState, slice_plan: SlicePlan, slice_def: SliceDefinition) -> str:
    if not slice_def.dependencies:
        return context.config.workspace.base_branch

    dependency_ids = slice_def.dependencies
    dependency_closure = {
        dependency_id: _transitive_dependencies(slice_plan, dependency_id)
        for dependency_id in dependency_ids
    }
    for dependency_id in reversed(dependency_ids):
        closure = dependency_closure[dependency_id]
        if all(other_id == dependency_id or other_id in closure for other_id in dependency_ids):
            dependency_state = state.slice_states.get(dependency_id)
            if dependency_state and dependency_state.branch_name:
                return dependency_state.branch_name
            return branch_name_for_slice(context.config.git.branch_prefix, state.feature_slug, dependency_id)
    return context.config.workspace.base_branch


def _commit_artifact_path(context: ExecutionContext, state: RunState, slice_id: str) -> Path:
    return context.state_store.stage_dir(state, "commit") / f"{slice_id}.txt"


def _commit_message(slice_def: SliceDefinition, slice_dir: Path, conventional_prefix: str) -> tuple[str, str]:
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
    acceptance_lines = [f"- {item}" for item in slice_def.acceptance_criteria] or ["- None"]
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


def commit_slice(context: ExecutionContext, state: RunState, slice_def: SliceDefinition) -> list[Path]:
    commit_path = _commit_artifact_path(context, state, slice_def.id)
    if commit_path.exists():
        return [commit_path]
    if not has_changes(context.repo_root):
        return []

    slice_dir = Path(state.run_dir) / "slices" / slice_def.id
    subject, body = _commit_message(slice_def, slice_dir, context.config.git.conventional_prefix)
    branch_name = branch_name_for_slice(context.config.git.branch_prefix, state.feature_slug, slice_def.id)
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


def create_prs(context: ExecutionContext, state: RunState, dry_run: bool = False) -> list[Path]:
    pr_dir = context.state_store.stage_dir(state, "pr")
    slice_plan = load_slice_plan(Path(state.run_dir))
    slice_def = _target_slice(context, state, slice_plan)
    prd = read_json(Path(state.run_dir) / "prd" / "prd.json")
    artifacts: list[Path] = []
    branch_name = branch_name_for_slice(context.config.git.branch_prefix, state.feature_slug, slice_def.id)
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
        pr_base = _pr_base_branch(context, state, slice_plan, slice_def)
        pr_url = create_pull_request(context.repo_root, title, body_path, context.config.git.pr_draft, base_branch=pr_base)
        url_path = pr_dir / f"{slice_def.id}-url.txt"
        write_text(url_path, pr_url + "\n")
        artifacts.append(url_path)
    return artifacts

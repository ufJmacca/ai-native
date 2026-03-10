from __future__ import annotations

from pathlib import Path, PurePosixPath

from ai_native.models import ReviewReport, RunState, SliceDefinition, SliceExecutionState, SlicePlan, VerificationReport
from ai_native.utils import read_json, read_text

SLICE_SPECIFIC_STAGES = {"loop", "verify", "commit", "pr"}


def load_slice_plan(run_dir: Path) -> SlicePlan:
    return SlicePlan.model_validate(read_json(run_dir / "slice" / "slices.json"))


def slice_by_id(slice_plan: SlicePlan, slice_id: str) -> SliceDefinition:
    for slice_def in slice_plan.slices:
        if slice_def.id == slice_id:
            return slice_def
    raise RuntimeError(f"Unknown slice id: {slice_id}")


def selected_slices(slice_plan: SlicePlan, slice_id: str | None, active_slice: str | None) -> list[SliceDefinition]:
    chosen = slice_id or active_slice
    if chosen is None:
        return slice_plan.slices
    return [slice_by_id(slice_plan, chosen)]


def branch_name_for_slice(branch_prefix: str, feature_slug: str, slice_id: str) -> str:
    return f"{branch_prefix}/{feature_slug}-{slice_id}"


def worktree_path_for_slice(worktrees_root: Path, run_id: str, slice_id: str) -> Path:
    return (worktrees_root / run_id / slice_id).resolve()


def read_commit_sha(commit_path: Path) -> str | None:
    if not commit_path.exists():
        return None
    lines = [line.strip() for line in read_text(commit_path).splitlines() if line.strip()]
    return lines[-1] if lines else None


def read_pr_url(url_path: Path) -> str | None:
    if not url_path.exists():
        return None
    text = read_text(url_path).strip()
    return text or None


def read_loop_review_verdict(slice_dir: Path) -> str | None:
    review_path = slice_dir / "test-review.json"
    if not review_path.exists():
        return None
    return ReviewReport.model_validate(read_json(review_path)).verdict


def read_verify_verdict(verify_dir: Path, slice_id: str) -> str | None:
    report_path = verify_dir / f"{slice_id}.json"
    if not report_path.exists():
        return None
    return VerificationReport.model_validate(read_json(report_path)).verdict


def normalize_repo_path(path_text: str) -> PurePosixPath | None:
    cleaned = path_text.strip().replace("\\", "/")
    if not cleaned or cleaned == ".":
        return PurePosixPath(".")
    path = PurePosixPath(cleaned)
    if path.is_absolute():
        parts = list(path.parts)
        if len(parts) > 1:
            path = PurePosixPath(*parts[1:])
        else:
            path = PurePosixPath(".")
    normalized_parts = [part for part in path.parts if part not in ("", ".")]
    if not normalized_parts:
        return PurePosixPath(".")
    return PurePosixPath(*normalized_parts)


def paths_conflict(left: str, right: str) -> bool:
    left_path = normalize_repo_path(left)
    right_path = normalize_repo_path(right)
    if left_path is None or right_path is None:
        return True
    if left_path == PurePosixPath(".") or right_path == PurePosixPath("."):
        return True
    left_parts = left_path.parts
    right_parts = right_path.parts
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def slice_conflict_reason(slice_def: SliceDefinition, running_slice: SliceDefinition) -> str | None:
    if not slice_def.file_impact or not running_slice.file_impact:
        return f"Conflicts with running slice {running_slice.id} because file impact is unspecified."
    for left in slice_def.file_impact:
        for right in running_slice.file_impact:
            if paths_conflict(left, right):
                return f"Conflicts with running slice {running_slice.id} on {left if len(left) <= len(right) else right}"
    return None


def infer_slice_state(
    state: RunState,
    slice_def: SliceDefinition,
    branch_prefix: str,
    worktrees_root: Path,
) -> SliceExecutionState:
    existing = state.slice_states.get(slice_def.id)
    branch_name = existing.branch_name if existing and existing.branch_name else branch_name_for_slice(branch_prefix, state.feature_slug, slice_def.id)
    worktree_path = existing.worktree_path if existing and existing.worktree_path else str(worktree_path_for_slice(worktrees_root, state.run_id, slice_def.id))
    commit_path = Path(state.run_dir) / "commit" / f"{slice_def.id}.txt"
    url_path = Path(state.run_dir) / "pr" / f"{slice_def.id}-url.txt"
    verify_dir = Path(state.run_dir) / "verify"
    slice_dir = Path(state.run_dir) / "slices" / slice_def.id
    commit_sha = existing.commit_sha if existing and existing.commit_sha else read_commit_sha(commit_path)
    pr_url = existing.pr_url if existing and existing.pr_url else read_pr_url(url_path)
    if pr_url:
        status = "pr_opened"
    elif commit_sha:
        status = "committed"
    elif read_verify_verdict(verify_dir, slice_def.id) == "passed":
        status = "verified"
    elif existing:
        status = existing.status
    elif read_loop_review_verdict(slice_dir) == "approved":
        status = "ready"
    else:
        status = "pending"
    if status == "running":
        status = "failed"
    return SliceExecutionState(
        slice_id=slice_def.id,
        branch_name=branch_name,
        worktree_path=worktree_path,
        status=status,
        current_stage=existing.current_stage if existing else None,
        block_reason=existing.block_reason if existing else None,
        commit_sha=commit_sha,
        pr_url=pr_url,
        attempt_counts=dict(existing.attempt_counts) if existing else {},
        started_at=existing.started_at if existing else None,
    )

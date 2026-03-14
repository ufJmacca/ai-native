from __future__ import annotations

from pathlib import Path

from ai_native.models import RunProjection, RunProjectionBlockedStep, RunState, SlicePlan
from ai_native.utils import read_json

PROJECTION_SCHEMA_VERSION = 1
SLICE_PIPELINE = ("loop", "verify", "commit", "pr")


def _pre_slice_stages() -> tuple[str, ...]:
    from ai_native.stages import ORDERED_STAGES

    return tuple(stage for stage in ORDERED_STAGES if stage not in SLICE_PIPELINE)


def _load_slice_plan(run_dir: Path) -> SlicePlan | None:
    path = run_dir / "slice" / "slices.json"
    if not path.exists():
        return None
    return SlicePlan.model_validate(read_json(path))


def _is_stage_completed(state: RunState, stage: str) -> bool:
    snapshot = state.stage_status.get(stage)
    return bool(snapshot and snapshot.status == "completed")




def _slice_stage_prefix(slice_id: str, stage: str) -> list[str]:
    completed: list[str] = []
    for pipeline_stage in SLICE_PIPELINE:
        if pipeline_stage == stage:
            break
        completed.append(f"{slice_id}:{pipeline_stage}")
    return completed

def _dependency_reason(state: RunState, dependency_ids: list[str]) -> str | None:
    for dependency_id in dependency_ids:
        dependency_state = state.slice_states.get(dependency_id)
        if dependency_state is None or dependency_state.status not in {"committed", "pr_opened"}:
            return f"Waiting for dependency {dependency_id} to reach commit stage"
    return None


def build_run_projection(state: RunState, slice_plan: SlicePlan | None = None) -> RunProjection:
    plan = slice_plan or _load_slice_plan(Path(state.run_dir))
    completed_steps: list[str] = []
    in_progress_steps: list[str] = []
    blocked_steps: list[RunProjectionBlockedStep] = []
    next_executable_steps: list[str] = []

    pre_slice_gate_open = True
    first_pending_pre_slice: str | None = None
    for stage in _pre_slice_stages():
        if _is_stage_completed(state, stage):
            completed_steps.append(stage)
            continue
        pre_slice_gate_open = False
        if state.current_stage == stage and state.status == "in_progress":
            in_progress_steps.append(stage)
        elif first_pending_pre_slice is None:
            first_pending_pre_slice = stage

    if first_pending_pre_slice:
        next_executable_steps.append(first_pending_pre_slice)

    if not plan:
        return RunProjection(
            schema_version=PROJECTION_SCHEMA_VERSION,
            completed_steps=completed_steps,
            in_progress_steps=in_progress_steps,
            blocked_steps=blocked_steps,
            next_executable_steps=next_executable_steps,
        )

    for slice_def in plan.slices:
        slice_state = state.slice_states.get(slice_def.id)
        if slice_state is None:
            blocked_steps.append(
                RunProjectionBlockedStep(
                    step=f"{slice_def.id}:loop",
                    reason="Slice execution state is not initialized.",
                )
            )
            continue

        if slice_state.status == "pr_opened":
            completed_steps.extend([f"{slice_def.id}:{stage}" for stage in SLICE_PIPELINE])
            continue
        if slice_state.status == "committed":
            completed_steps.extend([f"{slice_def.id}:loop", f"{slice_def.id}:verify", f"{slice_def.id}:commit"])
            next_executable_steps.append(f"{slice_def.id}:pr")
            continue
        if slice_state.status == "verified":
            completed_steps.extend([f"{slice_def.id}:loop", f"{slice_def.id}:verify"])
            next_executable_steps.append(f"{slice_def.id}:commit")
            continue

        if slice_state.status == "running" and slice_state.current_stage:
            in_progress_steps.append(f"{slice_def.id}:{slice_state.current_stage}")
            completed_steps.extend(_slice_stage_prefix(slice_def.id, slice_state.current_stage))
            continue

        if slice_state.status == "failed" and slice_state.current_stage in SLICE_PIPELINE:
            completed_steps.extend(_slice_stage_prefix(slice_def.id, slice_state.current_stage))
            next_executable_steps.append(f"{slice_def.id}:{slice_state.current_stage}")
            continue

        if not pre_slice_gate_open:
            blocked_steps.append(
                RunProjectionBlockedStep(
                    step=f"{slice_def.id}:loop",
                    reason="Waiting for pre-slice stages to complete.",
                )
            )
            continue

        dependency_reason = _dependency_reason(state, slice_def.dependencies)
        if dependency_reason:
            blocked_steps.append(RunProjectionBlockedStep(step=f"{slice_def.id}:loop", reason=dependency_reason))
            continue

        if slice_state.status == "blocked":
            blocked_steps.append(
                RunProjectionBlockedStep(
                    step=f"{slice_def.id}:{slice_state.current_stage or 'loop'}",
                    reason=slice_state.block_reason or "Slice is blocked by scheduler constraints.",
                )
            )
            continue

        next_executable_steps.append(f"{slice_def.id}:loop")

    return RunProjection(
        schema_version=PROJECTION_SCHEMA_VERSION,
        completed_steps=completed_steps,
        in_progress_steps=in_progress_steps,
        blocked_steps=blocked_steps,
        next_executable_steps=next_executable_steps,
    )

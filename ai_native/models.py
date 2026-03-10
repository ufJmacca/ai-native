from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


StageName = Literal[
    "intake",
    "recon",
    "plan",
    "architecture",
    "prd",
    "slice",
    "loop",
    "verify",
    "commit",
    "pr",
]

SliceExecutionStatus = Literal[
    "pending",
    "blocked",
    "ready",
    "running",
    "verified",
    "committed",
    "pr_opened",
    "failed",
]

SliceStageName = Literal["loop", "verify", "commit", "pr"]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextReport(BaseModel):
    repo_state: Literal["greenfield", "existing"]
    languages: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    architecture_summary: str
    risks: list[str] = Field(default_factory=list)
    touched_areas: list[str] = Field(default_factory=list)
    recommended_questions: list[str] = Field(default_factory=list)


class PlanArtifact(BaseModel):
    title: str
    summary: str
    implementation_steps: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    data_flow: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    test_strategy: list[str] = Field(default_factory=list)
    rollout_notes: list[str] = Field(default_factory=list)


class DiagramArtifact(BaseModel):
    title: str
    diagram: str
    legend: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class PRDArtifact(BaseModel):
    title: str
    user_value: str
    scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class SliceDefinition(BaseModel):
    id: str
    name: str
    goal: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    file_impact: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class SlicePlan(BaseModel):
    title: str
    summary: str
    slices: list[SliceDefinition] = Field(default_factory=list)


class ReviewReport(BaseModel):
    verdict: Literal["approved", "changes_required"]
    summary: str
    findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


class QuestionBatch(BaseModel):
    needs_user_input: bool = False
    summary: str = ""
    questions: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    verdict: Literal["passed", "failed"]
    summary: str
    acceptance_checks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class StageSnapshot(BaseModel):
    stage: StageName
    status: Literal["pending", "completed", "failed", "skipped"] = "pending"
    artifacts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SliceExecutionState(BaseModel):
    slice_id: str
    branch_name: str | None = None
    worktree_path: str | None = None
    status: SliceExecutionStatus = "pending"
    current_stage: SliceStageName | None = None
    block_reason: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    attempt_counts: dict[str, int] = Field(default_factory=dict)
    started_at: str | None = None
    updated_at: str = Field(default_factory=_timestamp)


class RunState(BaseModel):
    run_id: str
    feature_slug: str
    spec_path: str
    workspace_root: str
    spec_hash: str
    run_dir: str
    created_at: str
    updated_at: str
    current_stage: StageName = "intake"
    status: Literal["in_progress", "completed", "failed"] = "in_progress"
    stage_status: dict[str, StageSnapshot] = Field(default_factory=dict)
    active_slice: str | None = None
    slice_states: dict[str, SliceExecutionState] = Field(default_factory=dict)
    base_ref: str | None = None
    scheduler_status: Literal["idle", "running", "failed", "completed"] = "idle"
    metadata: dict[str, Any] = Field(default_factory=dict)

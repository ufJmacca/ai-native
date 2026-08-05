from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


RunStatus = Literal["in_progress", "completed", "failed"]
RunLiveness = Literal["active", "stale", "stopped"]

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

_SLICE_ID_MAX_LENGTH = 64
_PORTABLE_SLICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WINDOWS_RESERVED_COMPONENTS = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _validate_slice_id(value: str) -> str:
    reserved_stem = value.partition(".")[0].upper()
    if (
        not 1 <= len(value) <= _SLICE_ID_MAX_LENGTH
        or _PORTABLE_SLICE_ID_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
        or reserved_stem in _WINDOWS_RESERVED_COMPONENTS
    ):
        raise ValueError(
            "slice id must be a portable identifier of 1-64 ASCII letters, "
            "digits, dots, underscores, or hyphens"
        )
    return value


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


class ViewportConfig(BaseModel):
    width: int = Field(ge=320, le=7680)
    height: int = Field(ge=320, le=4320)
    label: str | None = None

    @property
    def resolved_label(self) -> str:
        return self.label or f"{self.width}x{self.height}"


class PreviewReadinessConfig(BaseModel):
    timeout_seconds: float = Field(default=60.0, gt=0)
    interval_seconds: float = Field(default=1.0, gt=0)
    expect_status: int = Field(default=200, ge=100, le=599)


class PreviewConfig(BaseModel):
    url: str
    command: str | list[str] | None = None
    readiness: PreviewReadinessConfig = Field(default_factory=PreviewReadinessConfig)


class ReferenceInput(BaseModel):
    id: str
    label: str
    kind: Literal["image", "html_export", "url"]
    route: str
    viewport: ViewportConfig
    notes: str | None = None
    path: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> "ReferenceInput":
        has_path = bool(self.path)
        has_url = bool(self.url)
        if self.kind in {"image", "html_export"} and not has_path:
            raise ValueError(
                f"reference `{self.id}` with kind `{self.kind}` requires `path`"
            )
        if self.kind == "url" and not has_url:
            raise ValueError(f"reference `{self.id}` with kind `url` requires `url`")
        if has_path and has_url:
            raise ValueError(
                f"reference `{self.id}` must define only one of `path` or `url`"
            )
        if not self.route.startswith("/"):
            raise ValueError(f"reference `{self.id}` route must start with `/`")
        return self


class ReferenceManifest(BaseModel):
    workflow_profile: Literal["reference_driven_web"]
    references: list[ReferenceInput] = Field(default_factory=list)
    preview: PreviewConfig

    @model_validator(mode="after")
    def _validate_references(self) -> "ReferenceManifest":
        if not self.references:
            raise ValueError(
                "reference-driven web workflow requires at least one reference"
            )
        seen: set[str] = set()
        for item in self.references:
            if item.id in seen:
                raise ValueError(f"duplicate reference id `{item.id}`")
            seen.add(item.id)
        return self


class ReferenceContext(BaseModel):
    workflow_profile: Literal["reference_driven_web"] = "reference_driven_web"
    summary: str
    design_intent: str
    stable_patterns: list[str] = Field(default_factory=list)
    typography: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    spacing: list[str] = Field(default_factory=list)
    layout_patterns: list[str] = Field(default_factory=list)
    repeated_components: list[str] = Field(default_factory=list)
    responsive_behaviors: list[str] = Field(default_factory=list)
    fidelity_constraints: list[str] = Field(default_factory=list)


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

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_slice_id(value)

    @field_validator("dependencies")
    @classmethod
    def _validate_dependency_ids(cls, values: list[str]) -> list[str]:
        return [_validate_slice_id(value) for value in values]


class SlicePlan(BaseModel):
    title: str
    summary: str
    slices: list[SliceDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dependency_graph(self) -> "SlicePlan":
        slices_by_id: dict[str, SliceDefinition] = {}
        casefolded_ids: dict[str, str] = {}
        for slice_def in self.slices:
            if slice_def.id in slices_by_id:
                raise ValueError(f"duplicate slice id `{slice_def.id}`")
            casefolded_id = slice_def.id.casefold()
            if casefolded_id in casefolded_ids:
                raise ValueError(
                    f"slice id `{slice_def.id}` conflicts with "
                    f"`{casefolded_ids[casefolded_id]}` on case-insensitive filesystems"
                )
            slices_by_id[slice_def.id] = slice_def
            casefolded_ids[casefolded_id] = slice_def.id

        for slice_def in self.slices:
            seen_dependencies: set[str] = set()
            for dependency_id in slice_def.dependencies:
                if dependency_id == slice_def.id:
                    raise ValueError(f"slice `{slice_def.id}` cannot depend on itself")
                if dependency_id in seen_dependencies:
                    raise ValueError(
                        f"slice `{slice_def.id}` repeats dependency `{dependency_id}`"
                    )
                if dependency_id not in slices_by_id:
                    raise ValueError(
                        f"slice `{slice_def.id}` references unknown dependency "
                        f"`{dependency_id}`"
                    )
                seen_dependencies.add(dependency_id)

        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(slice_id: str) -> None:
            if slice_id in visited:
                return
            if slice_id in visiting:
                cycle_start = path.index(slice_id)
                cycle = [*path[cycle_start:], slice_id]
                raise ValueError(
                    f"slice dependency cycle detected: {' -> '.join(cycle)}"
                )

            visiting.add(slice_id)
            path.append(slice_id)
            for dependency_id in slices_by_id[slice_id].dependencies:
                visit(dependency_id)
            path.pop()
            visiting.remove(slice_id)
            visited.add(slice_id)

        for slice_def in self.slices:
            visit(slice_def.id)
        return self


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


class RunProjectionBlockedStep(BaseModel):
    step: str
    reason: str


class RunProjection(BaseModel):
    schema_version: int = 1
    completed_steps: list[str] = Field(default_factory=list)
    in_progress_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[RunProjectionBlockedStep] = Field(default_factory=list)
    next_executable_steps: list[str] = Field(default_factory=list)


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
    status: RunStatus = "in_progress"
    stage_status: dict[str, StageSnapshot] = Field(default_factory=dict)
    active_slice: str | None = None
    slice_states: dict[str, SliceExecutionState] = Field(default_factory=dict)
    base_ref: str | None = None
    scheduler_status: Literal["idle", "running", "failed", "completed"] = "idle"
    run_projection: RunProjection | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunHeartbeat(BaseModel):
    run_id: str
    updated_at: str
    status: RunStatus
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunView(BaseModel):
    run_id: str
    feature_slug: str
    spec_path: str
    workspace_root: str
    run_dir: str
    created_at: str
    updated_at: str
    status: RunStatus
    liveness: RunLiveness


class RunDetailView(RunView):
    current_stage: StageName
    scheduler_status: Literal["idle", "running", "failed", "completed"]
    active_slice: str | None = None
    run_projection: RunProjection | None = None
    slice_states: dict[str, SliceExecutionState] = Field(default_factory=dict)
    stage_status: dict[str, StageSnapshot] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunRegistrySnapshot(BaseModel):
    workflow: str = "ai-native"
    feature_slug: str
    spec_path: str
    workspace_root: str
    run_dir: str
    status: RunStatus
    current_stage: StageName
    scheduler_status: Literal["idle", "running", "failed", "completed"]
    active_slice: str | None = None
    created_at: str
    updated_at: str
    last_heartbeat_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_projection: RunProjection | None = None
    stage_status: dict[str, StageSnapshot] = Field(default_factory=dict)
    slice_states: dict[str, SliceExecutionState] = Field(default_factory=dict)

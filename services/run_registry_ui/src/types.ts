export const ORDERED_STAGES = [
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
] as const;

export type RunStatus = "in_progress" | "completed" | "failed";
export type RunLiveness = "active" | "stale" | "stopped";
export type SliceStatus =
  | "pending"
  | "blocked"
  | "ready"
  | "running"
  | "verified"
  | "committed"
  | "pr_opened"
  | "failed";

export interface RegistrySession {
  apiBaseUrl: string;
  token: string;
}

export interface RunSummary {
  run_id: string;
  workflow: string;
  feature_slug: string | null;
  spec_path: string | null;
  workspace_root: string | null;
  status: RunStatus;
  current_stage: string | null;
  scheduler_status: string | null;
  active_slice: string | null;
  created_at: string;
  updated_at: string;
  last_heartbeat_at: string | null;
  expires_at: string;
  liveness: RunLiveness;
}

export interface RunProjectionBlockedStep {
  step: string;
  reason: string;
}

export interface RunProjection {
  schema_version: number;
  completed_steps: string[];
  in_progress_steps: string[];
  blocked_steps: RunProjectionBlockedStep[];
  next_executable_steps: string[];
}

export interface StageSnapshot {
  stage: string;
  status: "pending" | "completed" | "failed" | "skipped";
  artifacts: string[];
  notes: string[];
}

export interface SliceState {
  slice_id: string;
  branch_name: string | null;
  worktree_path: string | null;
  status: SliceStatus;
  current_stage: string | null;
  block_reason: string | null;
  commit_sha: string | null;
  pr_url: string | null;
  attempt_counts: Record<string, number>;
  started_at: string | null;
  updated_at: string;
}

export interface RunDetail extends RunSummary {
  run_dir: string | null;
  metadata: Record<string, unknown>;
  run_projection: RunProjection | null;
  stage_status: Record<string, StageSnapshot>;
  slice_states: Record<string, SliceState>;
}

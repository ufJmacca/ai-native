ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS feature_slug TEXT,
    ADD COLUMN IF NOT EXISTS spec_path TEXT,
    ADD COLUMN IF NOT EXISTS workspace_root TEXT,
    ADD COLUMN IF NOT EXISTS run_dir TEXT,
    ADD COLUMN IF NOT EXISTS current_stage TEXT,
    ADD COLUMN IF NOT EXISTS scheduler_status TEXT NOT NULL DEFAULT 'idle',
    ADD COLUMN IF NOT EXISTS active_slice TEXT,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS run_projection JSONB,
    ADD COLUMN IF NOT EXISTS stage_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS slice_states JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status);
CREATE INDEX IF NOT EXISTS runs_last_heartbeat_at_idx ON runs (last_heartbeat_at DESC);

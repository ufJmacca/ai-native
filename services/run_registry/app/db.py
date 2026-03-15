from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_LOCK_ID = 7321442009185123
RUN_COLUMNS = """
run_id,
workflow,
feature_slug,
spec_path,
workspace_root,
run_dir,
status,
current_stage,
scheduler_status,
active_slice,
metadata,
run_projection,
stage_status,
slice_states,
created_at,
updated_at,
last_heartbeat_at,
expires_at
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self._settings.database_dsn, row_factory=dict_row)

    def migrate(self) -> None:
        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migration_files:
            return

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
                try:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version TEXT PRIMARY KEY,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        """
                    )
                    cur.execute("SELECT version FROM schema_migrations")
                    applied = {row["version"] for row in cur.fetchall()}

                    for migration in migration_files:
                        if migration.name in applied:
                            continue
                        cur.execute(migration.read_text())
                        cur.execute(
                            """
                            INSERT INTO schema_migrations (version)
                            VALUES (%s)
                            ON CONFLICT (version) DO NOTHING
                            """,
                            (migration.name,),
                        )
                    conn.commit()
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))

    def upsert_run(
        self,
        run_id: str,
        *,
        workflow: str,
        feature_slug: str | None,
        spec_path: str | None,
        workspace_root: str | None,
        run_dir: str | None,
        status: str,
        current_stage: str | None,
        scheduler_status: str | None,
        active_slice: str | None,
        metadata: dict[str, Any],
        run_projection: dict[str, Any] | None,
        stage_status: dict[str, Any],
        slice_states: dict[str, Any],
        created_at: datetime,
        updated_at: datetime,
        last_heartbeat_at: datetime | None,
        expires_at: datetime,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO runs (
                        run_id,
                        workflow,
                        feature_slug,
                        spec_path,
                        workspace_root,
                        run_dir,
                        status,
                        current_stage,
                        scheduler_status,
                        active_slice,
                        metadata,
                        run_projection,
                        stage_status,
                        slice_states,
                        created_at,
                        updated_at,
                        last_heartbeat_at,
                        expires_at
                    )
                    VALUES (
                        %(run_id)s,
                        %(workflow)s,
                        %(feature_slug)s,
                        %(spec_path)s,
                        %(workspace_root)s,
                        %(run_dir)s,
                        %(status)s,
                        %(current_stage)s,
                        %(scheduler_status)s,
                        %(active_slice)s,
                        %(metadata)s::jsonb,
                        %(run_projection)s::jsonb,
                        %(stage_status)s::jsonb,
                        %(slice_states)s::jsonb,
                        %(created_at)s,
                        %(updated_at)s,
                        %(last_heartbeat_at)s,
                        %(expires_at)s
                    )
                    ON CONFLICT (run_id) DO UPDATE
                    SET workflow = EXCLUDED.workflow,
                        feature_slug = EXCLUDED.feature_slug,
                        spec_path = EXCLUDED.spec_path,
                        workspace_root = EXCLUDED.workspace_root,
                        run_dir = EXCLUDED.run_dir,
                        status = EXCLUDED.status,
                        current_stage = EXCLUDED.current_stage,
                        scheduler_status = EXCLUDED.scheduler_status,
                        active_slice = EXCLUDED.active_slice,
                        metadata = EXCLUDED.metadata,
                        run_projection = EXCLUDED.run_projection,
                        stage_status = EXCLUDED.stage_status,
                        slice_states = EXCLUDED.slice_states,
                        updated_at = EXCLUDED.updated_at,
                        last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                        expires_at = EXCLUDED.expires_at
                    RETURNING {RUN_COLUMNS}
                    """,
                    {
                        "run_id": run_id,
                        "workflow": workflow,
                        "feature_slug": feature_slug,
                        "spec_path": spec_path,
                        "workspace_root": workspace_root,
                        "run_dir": run_dir,
                        "status": status,
                        "current_stage": current_stage,
                        "scheduler_status": scheduler_status or "idle",
                        "active_slice": active_slice,
                        "metadata": Jsonb(metadata),
                        "run_projection": Jsonb(run_projection) if run_projection is not None else None,
                        "stage_status": Jsonb(stage_status),
                        "slice_states": Jsonb(slice_states),
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "last_heartbeat_at": last_heartbeat_at,
                        "expires_at": expires_at,
                    },
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Run upsert did not return a row.")
        return row

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {RUN_COLUMNS}
                    FROM runs
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                return cur.fetchone()

    def list_runs(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {RUN_COLUMNS}
                    FROM runs
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()

    def delete_run(self, run_id: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
            conn.commit()

    def purge_expired(self) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM runs WHERE expires_at < NOW()")
                deleted = cur.rowcount
            conn.commit()
        return deleted

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import Settings


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_LOCK_ID = 7321442009185123


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

    def purge_expired(self) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM runs WHERE expires_at < NOW()")
                deleted = cur.rowcount
            conn.commit()
        return deleted

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str
    auth_token: str
    cors_origins: list[str]
    retention_days: int
    liveness_ttl_seconds: int
    liveness_grace_period_seconds: int

    @property
    def database_dsn(self) -> str:
        encoded_user = quote(self.database_user, safe="")
        encoded_password = quote(self.database_password, safe="")
        return (
            "postgresql://"
            f"{encoded_user}:{encoded_password}@"
            f"{self.database_host}:{self.database_port}/{self.database_name}"
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    cors_raw = os.getenv("RUN_REGISTRY_CORS_ORIGINS", "*")
    cors_origins = [item.strip() for item in cors_raw.split(",") if item.strip()]
    retention_days = int(os.getenv("RUN_REGISTRY_RETENTION_DAYS", "30"))

    return Settings(
        app_host=os.getenv("RUN_REGISTRY_HOST", "0.0.0.0"),
        app_port=int(os.getenv("RUN_REGISTRY_PORT", "8080")),
        database_host=_required("RUN_REGISTRY_DB_HOST"),
        database_port=int(os.getenv("RUN_REGISTRY_DB_PORT", "5432")),
        database_name=_required("RUN_REGISTRY_DB_NAME"),
        database_user=_required("RUN_REGISTRY_DB_USER"),
        database_password=_required("RUN_REGISTRY_DB_PASSWORD"),
        auth_token=_required("RUN_REGISTRY_AUTH_TOKEN"),
        cors_origins=cors_origins,
        retention_days=retention_days,
        liveness_ttl_seconds=int(os.getenv("RUN_REGISTRY_LIVENESS_TTL_SECONDS", "60")),
        liveness_grace_period_seconds=int(os.getenv("RUN_REGISTRY_LIVENESS_GRACE_PERIOD_SECONDS", "120")),
    )

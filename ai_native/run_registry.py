from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote

from ai_native.config import RegistryConfig
from ai_native.models import RunRegistrySnapshot, RunState


def _last_heartbeat_at(state: RunState) -> str | None:
    heartbeat = state.metadata.get("heartbeat")
    if isinstance(heartbeat, dict):
        updated_at = heartbeat.get("updated_at")
        if isinstance(updated_at, str) and updated_at.strip():
            return updated_at
    return state.updated_at


def build_run_registry_snapshot(state: RunState) -> RunRegistrySnapshot:
    return RunRegistrySnapshot(
        feature_slug=state.feature_slug,
        spec_path=state.spec_path,
        workspace_root=state.workspace_root,
        run_dir=state.run_dir,
        status=state.status,
        current_stage=state.current_stage,
        scheduler_status=state.scheduler_status,
        active_slice=state.active_slice,
        created_at=state.created_at,
        updated_at=state.updated_at,
        last_heartbeat_at=_last_heartbeat_at(state),
        metadata=state.metadata,
        run_projection=state.run_projection,
        stage_status=state.stage_status,
        slice_states=state.slice_states,
    )


def publish_run_snapshot(config: RegistryConfig, state: RunState) -> None:
    if not config.remote_url or not config.auth_token:
        return

    snapshot = build_run_registry_snapshot(state)
    request = urllib.request.Request(
        f"{config.remote_url.rstrip('/')}/v1/runs/{quote(state.run_id, safe='')}",
        data=json.dumps(snapshot.model_dump(mode="json")).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {config.auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ai-native/run-registry",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds):
            return
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        detail = f"{exc.code} {exc.reason}".strip()
        if body:
            detail = f"{detail}: {body}"
        raise RuntimeError(f"Run registry request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Run registry request failed: {exc.reason}") from exc

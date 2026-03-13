from __future__ import annotations

import fcntl
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, TypeVar

from ai_native.config import RegistryConfig
from ai_native.models import RunDetailView, RunHeartbeat, RunLiveness, RunState, RunView, StageName, StageSnapshot
from ai_native.utils import ensure_dir, read_json, read_text, sha256_file, slugify, utc_now, write_json, write_text

T = TypeVar("T")
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class StateStore:
    def __init__(self, artifacts_root: Path):
        self.artifacts_root = artifacts_root
        ensure_dir(self.artifacts_root)

    def _state_path(self, run_dir: Path) -> Path:
        return run_dir / "state.json"

    def _lock_path(self, run_dir: Path) -> Path:
        return run_dir / "state.lock"

    def _thread_lock(self, run_dir: Path) -> threading.Lock:
        key = run_dir.resolve()
        with _LOCKS_GUARD:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _LOCKS[key] = lock
            return lock

    def _load_unlocked(self, run_dir: Path) -> RunState:
        data = read_json(self._state_path(run_dir))
        return RunState.model_validate(data)

    def _save_unlocked(self, state: RunState) -> None:
        run_dir = Path(state.run_dir)
        ensure_dir(run_dir)
        state.updated_at = utc_now()
        state_path = self._state_path(run_dir)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=run_dir)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            write_json(temp_path, state.model_dump(mode="json"))
            os.replace(temp_path, state_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def mutate(self, run_dir: Path, mutator: Callable[[RunState], T]) -> tuple[RunState, T]:
        resolved = run_dir.resolve()
        ensure_dir(resolved)
        lock_path = self._lock_path(resolved)
        ensure_dir(lock_path.parent)
        lock_path.touch(exist_ok=True)
        with self._thread_lock(resolved):
            with lock_path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    state = self._load_unlocked(resolved)
                    result = mutator(state)
                    self._save_unlocked(state)
                    return state, result
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create_run(self, spec_path: Path, workspace_root: Path) -> RunState:
        feature_slug = slugify(spec_path.stem)
        run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{run_stamp}-{feature_slug}"
        run_dir = ensure_dir(self.artifacts_root / run_id)
        copied_spec = run_dir / "spec.md"
        write_text(copied_spec, read_text(spec_path))
        state = RunState(
            run_id=run_id,
            feature_slug=feature_slug,
            spec_path=str(spec_path.resolve()),
            workspace_root=str(workspace_root.resolve()),
            spec_hash=sha256_file(spec_path),
            run_dir=str(run_dir),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.save(state)
        return state

    def save(self, state: RunState) -> None:
        run_dir = Path(state.run_dir).resolve()
        ensure_dir(run_dir)
        lock_path = self._lock_path(run_dir)
        ensure_dir(lock_path.parent)
        lock_path.touch(exist_ok=True)
        with self._thread_lock(run_dir):
            with lock_path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._save_unlocked(state)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self, run_dir: Path) -> RunState:
        resolved = run_dir.resolve()
        lock_path = self._lock_path(resolved)
        ensure_dir(lock_path.parent)
        lock_path.touch(exist_ok=True)
        with self._thread_lock(resolved):
            with lock_path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    return self._load_unlocked(resolved)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def find_latest_for_spec(self, spec_path: Path, workspace_root: Path | None = None) -> RunState | None:
        matches: list[RunState] = []
        for state_file in self.artifacts_root.glob("*/state.json"):
            try:
                state = RunState.model_validate(read_json(state_file))
            except Exception:
                continue
            if Path(state.spec_path) != spec_path.resolve():
                continue
            if workspace_root is not None and Path(state.workspace_root) != workspace_root.resolve():
                continue
            matches.append(state)
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.created_at)[-1]

    def stage_dir(self, state: RunState, stage: str) -> Path:
        return ensure_dir(Path(state.run_dir) / stage)

    def update_stage(
        self,
        state: RunState,
        stage: StageName,
        status: str,
        artifacts: list[Path] | None = None,
        notes: list[str] | None = None,
    ) -> RunState:
        def mutate_state(locked: RunState) -> RunState:
            snapshot = StageSnapshot(
                stage=stage,
                status=status,
                artifacts=[str(path) for path in (artifacts or [])],
                notes=notes or [],
            )
            locked.current_stage = stage
            locked.stage_status[stage] = snapshot
            if locked.status == "failed" and status != "failed":
                pass
            elif status == "failed":
                locked.status = "failed"
            elif stage == "pr" and status == "completed":
                locked.status = "completed" if locked.scheduler_status == "completed" else "in_progress"
            elif status == "completed":
                locked.status = "in_progress"
            state.current_stage = locked.current_stage
            state.stage_status = locked.stage_status
            state.status = locked.status
            state.updated_at = locked.updated_at
            state.slice_states = locked.slice_states
            state.base_ref = locked.base_ref
            state.scheduler_status = locked.scheduler_status
            return locked

        locked_state, _ = self.mutate(Path(state.run_dir), mutate_state)
        return locked_state

    def record_heartbeat(self, run_dir: Path, heartbeat: RunHeartbeat) -> RunState:
        def mutate_state(locked: RunState) -> None:
            locked.metadata["heartbeat"] = heartbeat.model_dump(mode="json")

        locked_state, _ = self.mutate(run_dir, mutate_state)
        return locked_state

    @staticmethod
    def _parse_timestamp(timestamp: str | None) -> datetime | None:
        if not timestamp:
            return None
        normalized = timestamp.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @classmethod
    def classify_liveness(cls, state: RunState, registry: RegistryConfig, now: datetime | None = None) -> RunLiveness:
        if state.status in {"completed", "failed"}:
            return "stopped"
        now_utc = now or datetime.now(UTC)
        heartbeat = RunHeartbeat.model_validate(state.metadata.get("heartbeat", {})) if state.metadata.get("heartbeat") else None
        observed = cls._parse_timestamp(heartbeat.updated_at) if heartbeat else cls._parse_timestamp(state.updated_at)
        if observed is None:
            return "stopped"
        age_seconds = (now_utc - observed).total_seconds()
        if age_seconds <= registry.liveness_ttl_seconds:
            return "active"
        if age_seconds <= registry.liveness_ttl_seconds + registry.liveness_grace_period_seconds:
            return "stale"
        return "stopped"

    def list_runs(self, registry: RegistryConfig) -> list[RunView]:
        states: list[RunState] = []
        for state_file in self.artifacts_root.glob("*/state.json"):
            try:
                states.append(RunState.model_validate(read_json(state_file)))
            except Exception:
                continue
        states.sort(key=lambda item: item.created_at, reverse=True)
        return [
            RunView(
                run_id=state.run_id,
                feature_slug=state.feature_slug,
                spec_path=state.spec_path,
                workspace_root=state.workspace_root,
                run_dir=state.run_dir,
                created_at=state.created_at,
                updated_at=state.updated_at,
                status=state.status,
                liveness=self.classify_liveness(state, registry),
            )
            for state in states
        ]

    def get_run_detail(self, run_dir: Path, registry: RegistryConfig) -> RunDetailView:
        state = self.load(run_dir)
        return RunDetailView(
            run_id=state.run_id,
            feature_slug=state.feature_slug,
            spec_path=state.spec_path,
            workspace_root=state.workspace_root,
            run_dir=state.run_dir,
            created_at=state.created_at,
            updated_at=state.updated_at,
            status=state.status,
            liveness=self.classify_liveness(state, registry),
            current_stage=state.current_stage,
            scheduler_status=state.scheduler_status,
            active_slice=state.active_slice,
            slice_states=state.slice_states,
            stage_status=state.stage_status,
            metadata=state.metadata,
        )

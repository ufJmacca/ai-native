from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ai_native.models import RunState, StageName, StageSnapshot
from ai_native.utils import ensure_dir, read_json, read_text, sha256_file, slugify, utc_now, write_json, write_text


class StateStore:
    def __init__(self, artifacts_root: Path):
        self.artifacts_root = artifacts_root
        ensure_dir(self.artifacts_root)

    def create_run(self, spec_path: Path) -> RunState:
        feature_slug = slugify(spec_path.stem)
        run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{run_stamp}-{feature_slug}"
        run_dir = ensure_dir(self.artifacts_root / run_id)
        copied_spec = run_dir / "spec.md"
        write_text(copied_spec, read_text(spec_path))
        state = RunState(
            run_id=run_id,
            feature_slug=feature_slug,
            spec_path=str(spec_path.resolve()),
            spec_hash=sha256_file(spec_path),
            run_dir=str(run_dir),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.save(state)
        return state

    def save(self, state: RunState) -> None:
        state.updated_at = utc_now()
        write_json(Path(state.run_dir) / "state.json", state.model_dump(mode="json"))

    def load(self, run_dir: Path) -> RunState:
        data = read_json(run_dir / "state.json")
        return RunState.model_validate(data)

    def find_latest_for_spec(self, spec_path: Path) -> RunState | None:
        matches: list[RunState] = []
        for state_file in self.artifacts_root.glob("*/state.json"):
            try:
                state = RunState.model_validate(read_json(state_file))
            except Exception:
                continue
            if Path(state.spec_path) == spec_path.resolve():
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
        snapshot = StageSnapshot(
            stage=stage,
            status=status,
            artifacts=[str(path) for path in (artifacts or [])],
            notes=notes or [],
        )
        state.current_stage = stage
        state.stage_status[stage] = snapshot
        if status == "failed":
            state.status = "failed"
        elif stage == "pr" and status == "completed":
            state.status = "completed"
        elif status == "completed":
            state.status = "in_progress"
        self.save(state)
        return state

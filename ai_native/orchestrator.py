from __future__ import annotations

from pathlib import Path
from typing import Callable

from ai_native.adapters import build_role_adapters
from ai_native.config import AppConfig
from ai_native.models import RunState
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages import ORDERED_STAGES, commit_run, create_prs, run_architecture, run_intake, run_loop, run_plan, run_prd, run_recon, run_slice, run_verify
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.utils import sha256_file


class WorkflowOrchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.state_store = StateStore(config.workspace.artifacts_dir)
        self.prompt_library = PromptLibrary(config.repo_root / "ai_native" / "prompts")
        adapters = build_role_adapters(config)
        self.adapters = adapters
        self.stage_handlers: dict[str, Callable[..., list[Path]]] = {
            "intake": run_intake,
            "recon": run_recon,
            "plan": run_plan,
            "architecture": run_architecture,
            "prd": run_prd,
            "slice": run_slice,
            "loop": run_loop,
            "verify": run_verify,
            "commit": commit_run,
            "pr": create_prs,
        }

    def _context(self, spec_path: Path, state: RunState) -> ExecutionContext:
        return ExecutionContext(
            config=self.config,
            prompt_library=self.prompt_library,
            state_store=self.state_store,
            repo_root=self.config.repo_root,
            spec_path=spec_path,
            run_dir=Path(state.run_dir),
            builder=self.adapters["builder"],
            critic=self.adapters["critic"],
            verifier=self.adapters["verifier"],
            pr_reviewer=self.adapters["pr_reviewer"],
        )

    def prepare_state(self, spec_path: Path, run_dir: Path | None = None) -> RunState:
        if run_dir:
            return self.state_store.load(run_dir)
        existing = self.state_store.find_latest_for_spec(spec_path.resolve())
        if existing and existing.status != "completed" and existing.spec_hash == sha256_file(spec_path.resolve()):
            return existing
        return self.state_store.create_run(spec_path.resolve())

    def run_until(self, spec_path: Path, target_stage: str, run_dir: Path | None = None, dry_run_pr: bool = False) -> RunState:
        state = self.prepare_state(spec_path, run_dir=run_dir)
        context = self._context(spec_path.resolve(), state)
        for stage in ORDERED_STAGES:
            handler = self.stage_handlers[stage]
            snapshot = state.stage_status.get(stage)
            if snapshot and snapshot.status == "completed":
                if stage == target_stage:
                    return state
                continue
            try:
                if stage == "pr":
                    artifacts = handler(context, state, dry_run=dry_run_pr)
                else:
                    artifacts = handler(context, state)
                self.state_store.update_stage(state, stage=stage, status="completed", artifacts=artifacts)
            except StageError as exc:
                self.state_store.update_stage(state, stage=stage, status="failed", notes=[str(exc)])
                raise
            if stage == target_stage:
                return state
        return state

    def run_all(self, spec_path: Path, run_dir: Path | None = None, dry_run_pr: bool = False) -> RunState:
        return self.run_until(spec_path=spec_path, target_stage="pr", run_dir=run_dir, dry_run_pr=dry_run_pr)

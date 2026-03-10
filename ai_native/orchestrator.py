from __future__ import annotations

from pathlib import Path
from typing import Callable

from ai_native.adapters import build_role_adapters
from ai_native.config import AppConfig
from ai_native.gitops import ensure_repo
from ai_native.models import RunState, SlicePlan
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.stages import ORDERED_STAGES, commit_run, create_prs, run_architecture, run_intake, run_loop, run_plan, run_prd, run_recon, run_slice, run_verify
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.utils import read_json, sha256_file


class WorkflowOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        progress: Callable[[str], None] | None = None,
        question_responder: Callable[[str, list[str]], list[str]] | None = None,
    ):
        self.config = config
        self.prompt_library = PromptLibrary(config.repo_root / "ai_native" / "prompts")
        self.progress = progress or (lambda _message: None)
        self.question_responder = question_responder or (lambda _stage, questions: [""] * len(questions))
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

    def _emit(self, message: str) -> None:
        self.progress(message)

    def _state_store(self, workspace_root: Path | None = None, run_dir: Path | None = None) -> StateStore:
        if run_dir is not None:
            return StateStore(run_dir.resolve().parent)
        resolved_workspace = workspace_root.resolve() if workspace_root is not None else self.config.repo_root
        return StateStore(self.config.resolve_artifacts_dir(resolved_workspace))

    def _context(self, spec_path: Path, state: RunState) -> ExecutionContext:
        state_store = self._state_store(run_dir=Path(state.run_dir))
        return ExecutionContext(
            config=self.config,
            prompt_library=self.prompt_library,
            state_store=state_store,
            template_root=self.config.repo_root,
            repo_root=Path(state.workspace_root),
            spec_path=spec_path,
            run_dir=Path(state.run_dir),
            builder=self.adapters["builder"],
            critic=self.adapters["critic"],
            verifier=self.adapters["verifier"],
            pr_reviewer=self.adapters["pr_reviewer"],
            emit_progress=self._emit,
            ask_questions=self.question_responder,
        )

    def _run_stage(
        self,
        context: ExecutionContext,
        state: RunState,
        stage: str,
        *,
        dry_run_pr: bool = False,
        skip_completed: bool = True,
    ) -> list[Path] | None:
        handler = self.stage_handlers[stage]
        snapshot = state.stage_status.get(stage)
        if skip_completed and snapshot and snapshot.status == "completed":
            self._emit(f"[ainative] {stage}: skipped (already completed)")
            return None
        try:
            self._emit(f"[ainative] {stage}: started")
            if stage == "pr":
                artifacts = handler(context, state, dry_run=dry_run_pr)
            else:
                artifacts = handler(context, state)
            context.state_store.update_stage(state, stage=stage, status="completed", artifacts=artifacts)
            self._emit(f"[ainative] {stage}: completed")
            return artifacts
        except StageError as exc:
            context.state_store.update_stage(state, stage=stage, status="failed", notes=[str(exc)])
            self._emit(f"[ainative] {stage}: failed - {exc}")
            raise

    def prepare_state(self, spec_path: Path, workspace_root: Path | None = None, run_dir: Path | None = None) -> RunState:
        effective_workspace_root = workspace_root.resolve() if workspace_root is not None else self.config.repo_root
        if run_dir:
            state = self._state_store(run_dir=run_dir).load(run_dir.resolve())
            ensure_repo(Path(state.workspace_root), self.config.workspace.base_branch)
            return state
        effective_workspace_root.mkdir(parents=True, exist_ok=True)
        ensure_repo(effective_workspace_root, self.config.workspace.base_branch)
        state_store = self._state_store(workspace_root=effective_workspace_root)
        existing = state_store.find_latest_for_spec(spec_path.resolve(), effective_workspace_root)
        if existing and existing.status != "completed" and existing.spec_hash == sha256_file(spec_path.resolve()):
            return existing
        return state_store.create_run(spec_path.resolve(), effective_workspace_root)

    def run_until(
        self,
        spec_path: Path,
        target_stage: str,
        run_dir: Path | None = None,
        dry_run_pr: bool = False,
        workspace_root: Path | None = None,
    ) -> RunState:
        state = self.prepare_state(spec_path, workspace_root=workspace_root, run_dir=run_dir)
        self._emit(f"[ainative] run-dir: {state.run_dir}")
        self._emit(f"[ainative] workspace-dir: {state.workspace_root}")
        context = self._context(spec_path.resolve(), state)
        for stage in ORDERED_STAGES:
            self._run_stage(context, state, stage, dry_run_pr=dry_run_pr, skip_completed=True)
            if stage == target_stage:
                return state
        return state

    def run_all(
        self,
        spec_path: Path,
        run_dir: Path | None = None,
        dry_run_pr: bool = False,
        workspace_root: Path | None = None,
    ) -> RunState:
        state = self.prepare_state(spec_path, workspace_root=workspace_root, run_dir=run_dir)
        self._emit(f"[ainative] run-dir: {state.run_dir}")
        self._emit(f"[ainative] workspace-dir: {state.workspace_root}")
        context = self._context(spec_path.resolve(), state)

        for stage in ("intake", "recon", "plan", "architecture", "prd", "slice"):
            self._run_stage(context, state, stage, skip_completed=True)

        slice_plan = SlicePlan.model_validate(read_json(Path(state.run_dir) / "slice" / "slices.json"))
        loop_artifacts = [Path(path) for path in state.stage_status.get("loop", {}).artifacts] if state.stage_status.get("loop") else []
        verify_artifacts = [Path(path) for path in state.stage_status.get("verify", {}).artifacts] if state.stage_status.get("verify") else []
        commit_dir = context.state_store.stage_dir(state, "commit")
        commit_artifacts = [Path(path) for path in state.stage_status.get("commit", {}).artifacts] if state.stage_status.get("commit") else []
        if not commit_artifacts:
            commit_artifacts = sorted(commit_dir.glob("*.txt"))
        completed_slice_ids = {path.stem for path in commit_dir.glob("*.txt")}

        for slice_def in slice_plan.slices:
            if slice_def.id in completed_slice_ids:
                self._emit(f"[ainative] commit: slice {slice_def.id} already committed, skipping")
                state.active_slice = slice_def.id
                context.state_store.save(state)
                continue

            state.active_slice = slice_def.id
            context.state_store.save(state)
            loop_result = self._run_stage(context, state, "loop", skip_completed=False) or []
            verify_result = self._run_stage(context, state, "verify", skip_completed=False) or []
            commit_result = self._run_stage(context, state, "commit", skip_completed=False) or []
            loop_artifacts.extend(loop_result)
            verify_artifacts.extend(verify_result)
            commit_artifacts.extend(commit_result)

        context.state_store.update_stage(state, stage="loop", status="completed", artifacts=list(dict.fromkeys(loop_artifacts)))
        context.state_store.update_stage(
            state,
            stage="verify",
            status="completed",
            artifacts=list(dict.fromkeys(verify_artifacts)),
        )
        context.state_store.update_stage(
            state,
            stage="commit",
            status="completed",
            artifacts=list(dict.fromkeys(commit_artifacts)),
        )
        self._run_stage(context, state, "pr", dry_run_pr=dry_run_pr, skip_completed=True)
        return state

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
from pathlib import Path
from typing import Callable

from ai_native.adapters import build_role_adapters
from ai_native.config import AppConfig
from ai_native.gitops import ensure_base_commit, ensure_repo, ensure_worktree, is_ancestor, non_ai_native_changes, resolve_base_ref
from ai_native.models import RunState, SliceDefinition, SliceExecutionState
from ai_native.prompting import PromptLibrary
from ai_native.slice_runtime import (
    SLICE_SPECIFIC_STAGES,
    infer_slice_state,
    load_slice_plan,
    read_commit_sha,
    read_loop_review_verdict,
    read_pr_url,
    read_verify_verdict,
    selected_slices,
    slice_by_id,
    slice_conflict_reason,
)
from ai_native.state import StateStore
from ai_native.stages import ORDERED_STAGES, commit_run, create_prs, run_architecture, run_intake, run_loop, run_plan, run_prd, run_recon, run_slice, run_verify
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.utils import read_json, sha256_file, utc_now, write_json, write_text

PRE_SLICE_STAGES = ("intake", "recon", "plan", "architecture", "prd", "slice")
SLICE_PIPELINE_STAGES = ("loop", "verify", "commit", "pr")
TERMINAL_SLICE_STATUSES = {"committed", "pr_opened"}
SLICE_STAGE_TERMINAL_STATUSES = {
    "loop": {"ready", "verified", "committed", "pr_opened"},
    "verify": {"verified", "committed", "pr_opened"},
    "commit": {"committed", "pr_opened"},
    "pr": {"pr_opened"},
}


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
        self._state_sync_lock = threading.Lock()
        self.adapters = build_role_adapters(config)
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

    def _context(
        self,
        spec_path: Path,
        state: RunState,
        *,
        repo_root: Path | None = None,
        slice_id: str | None = None,
    ) -> ExecutionContext:
        state_store = self._state_store(run_dir=Path(state.run_dir))
        return ExecutionContext(
            config=self.config,
            prompt_library=self.prompt_library,
            state_store=state_store,
            template_root=self.config.repo_root,
            repo_root=(repo_root or Path(state.workspace_root)).resolve(),
            spec_path=spec_path,
            run_dir=Path(state.run_dir),
            builder=self.adapters["builder"],
            critic=self.adapters["critic"],
            verifier=self.adapters["verifier"],
            pr_reviewer=self.adapters["pr_reviewer"],
            slice_id=slice_id,
            emit_progress=self._emit,
            ask_questions=self.question_responder,
        )

    @staticmethod
    def _copy_state(state: RunState) -> RunState:
        return RunState.model_validate(state.model_dump(mode="json"))

    def _sync_state(self, target: RunState, source: RunState) -> None:
        latest = source.model_copy(deep=True)
        with self._state_sync_lock:
            for key in latest.__class__.model_fields:
                setattr(target, key, getattr(latest, key))

    def _mutate_state(self, state: RunState, mutator: Callable[[RunState], None]) -> RunState:
        def apply_mutation(locked: RunState) -> None:
            mutator(locked)

        latest, _ = self._state_store(run_dir=Path(state.run_dir)).mutate(Path(state.run_dir), apply_mutation)
        self._sync_state(state, latest)
        return latest

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

    def _resolve_slice_id(self, state: RunState, target_stage: str, requested_slice_id: str | None) -> str | None:
        if target_stage not in SLICE_SPECIFIC_STAGES:
            return requested_slice_id
        slice_plan = load_slice_plan(Path(state.run_dir))
        if requested_slice_id is not None:
            slice_by_id(slice_plan, requested_slice_id)
            return requested_slice_id

        candidates: list[str] = []
        for slice_def in slice_plan.slices:
            slice_state = state.slice_states.get(slice_def.id)
            if slice_state is not None:
                if slice_state.status in SLICE_STAGE_TERMINAL_STATUSES[target_stage]:
                    continue
                candidates.append(slice_def.id)
                continue
            if target_stage == "loop":
                if read_loop_review_verdict(Path(state.run_dir) / "slices" / slice_def.id) == "approved":
                    continue
            elif target_stage == "verify":
                if read_verify_verdict(Path(state.run_dir) / "verify", slice_def.id) == "passed":
                    continue
            elif target_stage == "commit":
                if read_commit_sha(Path(state.run_dir) / "commit" / f"{slice_def.id}.txt"):
                    continue
            elif target_stage == "pr":
                if read_pr_url(Path(state.run_dir) / "pr" / f"{slice_def.id}-url.txt"):
                    continue
            candidates.append(slice_def.id)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise StageError(f"No candidate slices remain for stage {target_stage}.")
        raise StageError(
            f"Stage {target_stage} requires --slice-id/SLICE because multiple candidate slices remain: {', '.join(candidates)}"
        )

    def _slice_repo_root(self, state: RunState, slice_id: str) -> Path:
        repo_root = Path(state.workspace_root)
        ensure_base_commit(repo_root, self.config.workspace.base_branch)
        slice_state = state.slice_states.get(slice_id)
        if slice_state is None or not slice_state.worktree_path or not slice_state.branch_name:
            return repo_root
        worktree_path = ensure_worktree(repo_root, slice_state.branch_name, Path(slice_state.worktree_path), state.base_ref or self.config.workspace.base_branch)
        if str(worktree_path) != slice_state.worktree_path:
            self._mutate_state(state, lambda locked: setattr(locked.slice_states[slice_id], "worktree_path", str(worktree_path)))
        return worktree_path

    def run_until(
        self,
        spec_path: Path,
        target_stage: str,
        run_dir: Path | None = None,
        dry_run_pr: bool = False,
        workspace_root: Path | None = None,
        slice_id: str | None = None,
    ) -> RunState:
        state = self.prepare_state(spec_path, workspace_root=workspace_root, run_dir=run_dir)
        self._emit(f"[ainative] run-dir: {state.run_dir}")
        self._emit(f"[ainative] workspace-dir: {state.workspace_root}")
        resolved_slice_id = slice_id
        context = self._context(spec_path.resolve(), state)
        for stage in ORDERED_STAGES:
            stage_context = context
            skip_completed = True
            if stage in SLICE_SPECIFIC_STAGES:
                if resolved_slice_id is None:
                    resolved_slice_id = self._resolve_slice_id(state, target_stage, None)
                stage_slice_id = resolved_slice_id if stage in SLICE_SPECIFIC_STAGES else None
                repo_root = self._slice_repo_root(state, stage_slice_id) if stage_slice_id else Path(state.workspace_root)
                stage_context = self._context(spec_path.resolve(), state, repo_root=repo_root, slice_id=stage_slice_id)
                state.active_slice = stage_slice_id
                skip_completed = False
            self._run_stage(stage_context, state, stage, dry_run_pr=dry_run_pr, skip_completed=skip_completed)
            if stage == target_stage:
                return state
        return state

    def _initialize_slice_states(self, state: RunState, slice_plan) -> RunState:
        worktrees_root = self.config.resolve_worktrees_dir(Path(state.workspace_root))
        base_ref = resolve_base_ref(Path(state.workspace_root), self.config.workspace.base_branch)

        def mutate(locked: RunState) -> None:
            locked.base_ref = base_ref
            locked.scheduler_status = "running"
            locked.active_slice = None
            locked.slice_states = {
                slice_def.id: infer_slice_state(locked, slice_def, self.config.git.branch_prefix, worktrees_root)
                for slice_def in slice_plan.slices
            }
            for slice_state in locked.slice_states.values():
                if slice_state.status == "running":
                    slice_state.status = "failed"
                    slice_state.block_reason = "Interrupted while previously running."
                    slice_state.updated_at = utc_now()

        return self._mutate_state(state, mutate)

    def _ensure_parallel_preflight(self, state: RunState) -> None:
        changes = non_ai_native_changes(Path(state.workspace_root))
        if changes:
            lines = "\n".join(f"- {path}" for path in changes[:20])
            raise StageError(
                "Parallel worktree execution requires a clean target repo outside .ai-native/. "
                f"Pending changes:\n{lines}"
            )

    def _dependency_block_reason(self, state: RunState, slice_def: SliceDefinition) -> str | None:
        if not state.base_ref:
            return f"Waiting for base reference {self.config.workspace.base_branch} to be resolved."
        repo_root = Path(state.workspace_root)
        for dependency_id in slice_def.dependencies:
            dependency_state = state.slice_states.get(dependency_id)
            if dependency_state is None or not dependency_state.commit_sha:
                return f"Waiting for dependency {dependency_id} to merge into {self.config.workspace.base_branch}"
            if not is_ancestor(repo_root, dependency_state.commit_sha, state.base_ref):
                return f"Waiting for dependency {dependency_id} to merge into {self.config.workspace.base_branch}"
        return None

    def _evaluate_ready_slices(
        self,
        state: RunState,
        slice_plan,
        running_slice_ids: set[str],
        *,
        stop_launching: bool,
    ) -> tuple[list[SliceDefinition], dict[str, str]]:
        by_id = {slice_def.id: slice_def for slice_def in slice_plan.slices}
        ready: list[SliceDefinition] = []
        blocked: dict[str, str] = {}
        for slice_def in slice_plan.slices:
            slice_state = state.slice_states[slice_def.id]
            if slice_state.status == "pr_opened":
                continue
            if slice_def.id in running_slice_ids:
                continue
            if stop_launching and slice_state.status not in {"failed", "committed", "pr_opened"}:
                blocked[slice_def.id] = "Scheduler stopped after another slice failure."
                continue
            dependency_reason = self._dependency_block_reason(state, slice_def)
            if dependency_reason:
                blocked[slice_def.id] = dependency_reason
                continue
            conflict_reason: str | None = None
            for running_id in running_slice_ids:
                reason = slice_conflict_reason(slice_def, by_id[running_id])
                if reason:
                    conflict_reason = reason
                    break
            if conflict_reason:
                blocked[slice_def.id] = conflict_reason
                continue
            if slice_state.status in {"committed", "verified", "failed", "ready", "pending", "blocked"}:
                ready.append(slice_def)
        return ready, blocked

    def _persist_queue_state(
        self,
        state: RunState,
        ready_slices: list[SliceDefinition],
        blocked_reasons: dict[str, str],
        running_slice_ids: set[str],
    ) -> None:
        ready_ids = {slice_def.id for slice_def in ready_slices}

        def mutate(locked: RunState) -> None:
            for slice_id, slice_state in locked.slice_states.items():
                if slice_id in running_slice_ids or slice_state.status in {"committed", "pr_opened", "verified"}:
                    continue
                if slice_id in ready_ids:
                    slice_state.status = "ready"
                    slice_state.block_reason = None
                elif slice_id in blocked_reasons:
                    slice_state.status = "blocked"
                    slice_state.block_reason = blocked_reasons[slice_id]
                slice_state.updated_at = utc_now()

        self._mutate_state(state, mutate)

    def _mark_slice_stage_start(self, state: RunState, slice_id: str, stage: str) -> None:
        def mutate(locked: RunState) -> None:
            slice_state = locked.slice_states[slice_id]
            slice_state.status = "running"
            slice_state.current_stage = stage
            slice_state.block_reason = None
            slice_state.attempt_counts[stage] = slice_state.attempt_counts.get(stage, 0) + 1
            if slice_state.started_at is None:
                slice_state.started_at = utc_now()
            slice_state.updated_at = utc_now()

        self._mutate_state(state, mutate)

    def _mark_slice_success(self, state: RunState, slice_id: str, stage: str, artifacts: list[Path]) -> None:
        commit_sha = read_commit_sha(Path(state.run_dir) / "commit" / f"{slice_id}.txt") if stage in {"commit", "pr"} else None
        pr_url = read_pr_url(Path(state.run_dir) / "pr" / f"{slice_id}-url.txt") if stage == "pr" else None

        def mutate(locked: RunState) -> None:
            slice_state = locked.slice_states[slice_id]
            if stage == "verify":
                slice_state.status = "verified"
            elif stage == "commit":
                slice_state.status = "committed"
                slice_state.commit_sha = commit_sha or slice_state.commit_sha
            elif stage == "pr":
                slice_state.status = "pr_opened"
                slice_state.commit_sha = commit_sha or slice_state.commit_sha
                slice_state.pr_url = pr_url or slice_state.pr_url
            else:
                slice_state.status = "ready"
            slice_state.current_stage = stage
            slice_state.block_reason = None
            slice_state.updated_at = utc_now()

        self._mutate_state(state, mutate)

    def _mark_slice_failure(self, state: RunState, slice_id: str, stage: str, error: str) -> None:
        def mutate(locked: RunState) -> None:
            slice_state = locked.slice_states[slice_id]
            slice_state.status = "failed"
            slice_state.current_stage = stage
            slice_state.block_reason = error
            slice_state.updated_at = utc_now()
            locked.scheduler_status = "failed"
            locked.status = "failed"

        self._mutate_state(state, mutate)

    def _slice_pipeline_stages(self, slice_state: SliceExecutionState) -> tuple[str, ...]:
        if slice_state.status == "pr_opened":
            return ()
        if slice_state.status == "committed":
            return ("pr",)
        if slice_state.status == "verified":
            return ("commit", "pr")
        if slice_state.status == "failed" and slice_state.current_stage in SLICE_PIPELINE_STAGES:
            return SLICE_PIPELINE_STAGES[SLICE_PIPELINE_STAGES.index(slice_state.current_stage) :]
        return SLICE_PIPELINE_STAGES

    def _run_slice_pipeline(self, state: RunState, spec_path: Path, slice_def: SliceDefinition, dry_run_pr: bool) -> dict[str, list[Path]]:
        local_state = self._copy_state(state)
        local_state.active_slice = slice_def.id
        repo_root = self._slice_repo_root(state, slice_def.id)
        local_context = self._context(spec_path, local_state, repo_root=repo_root, slice_id=slice_def.id)
        self._emit(f"[ainative] slice {slice_def.id}: worktree ready at {repo_root}")
        slice_state = state.slice_states[slice_def.id]
        artifacts: dict[str, list[Path]] = {stage: [] for stage in SLICE_PIPELINE_STAGES}
        for stage in self._slice_pipeline_stages(slice_state):
            self._emit(f"[ainative] slice {slice_def.id}: {stage} started")
            self._mark_slice_stage_start(state, slice_def.id, stage)
            try:
                if stage == "pr":
                    result = self.stage_handlers[stage](local_context, local_state, dry_run=dry_run_pr)
                else:
                    result = self.stage_handlers[stage](local_context, local_state)
                artifacts[stage].extend(result)
                self._mark_slice_success(state, slice_def.id, stage, result)
                self._emit(f"[ainative] slice {slice_def.id}: {stage} completed")
            except StageError as exc:
                self._mark_slice_failure(state, slice_def.id, stage, str(exc))
                self._emit(f"[ainative] slice {slice_def.id}: {stage} failed - {exc}")
                raise
        return artifacts

    def _scheduler_summary_artifacts(self, state: RunState, slice_plan) -> list[Path]:
        scheduler_dir = Path(state.run_dir) / "scheduler"
        scheduler_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_ref": state.base_ref,
            "scheduler_status": state.scheduler_status,
            "slices": {
                slice_id: slice_state.model_dump(mode="json")
                for slice_id, slice_state in state.slice_states.items()
            },
        }
        json_path = scheduler_dir / "summary.json"
        md_path = scheduler_dir / "summary.md"
        write_json(json_path, payload)
        lines = [
            "# Scheduler Summary",
            "",
            f"- Base ref: `{state.base_ref or self.config.workspace.base_branch}`",
            f"- Scheduler status: `{state.scheduler_status}`",
            "",
            "## Slices",
        ]
        for slice_def in slice_plan.slices:
            slice_state = state.slice_states[slice_def.id]
            lines.extend(
                [
                    "",
                    f"### {slice_def.id}: {slice_def.name}",
                    f"- Status: `{slice_state.status}`",
                    f"- Stage: `{slice_state.current_stage or 'none'}`",
                    f"- Branch: `{slice_state.branch_name or 'none'}`",
                    f"- Worktree: `{slice_state.worktree_path or 'none'}`",
                    f"- Commit: `{slice_state.commit_sha or 'none'}`",
                    f"- PR URL: `{slice_state.pr_url or 'none'}`",
                    f"- Block reason: {slice_state.block_reason or 'none'}",
                ]
            )
        write_text(md_path, "\n".join(lines) + "\n")
        return [json_path, md_path]

    def _aggregate_stage_artifacts(self, state: RunState, stage: str, collected: dict[str, list[Path]]) -> list[Path]:
        existing = [Path(path) for path in state.stage_status.get(stage, {}).artifacts] if stage in state.stage_status else []
        merged = existing + collected.get(stage, [])
        return list(dict.fromkeys(merged))

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

        for stage in PRE_SLICE_STAGES:
            self._run_stage(context, state, stage, skip_completed=True)

        slice_plan = load_slice_plan(Path(state.run_dir))
        self._ensure_parallel_preflight(state)
        ensure_base_commit(Path(state.workspace_root), self.config.workspace.base_branch)
        self._initialize_slice_states(state, slice_plan)

        collected: dict[str, list[Path]] = {stage: [] for stage in SLICE_PIPELINE_STAGES}
        stop_launching = False
        running: dict[Future[dict[str, list[Path]]], str] = {}
        running_ids: set[str] = set()

        with ThreadPoolExecutor(max_workers=max(1, self.config.workspace.parallel_workers)) as executor:
            while True:
                refreshed = self._state_store(run_dir=Path(state.run_dir)).load(Path(state.run_dir))
                self._sync_state(state, refreshed)
                ready_slices, blocked_reasons = self._evaluate_ready_slices(state, slice_plan, running_ids, stop_launching=stop_launching)
                self._persist_queue_state(state, ready_slices, blocked_reasons, running_ids)
                if ready_slices:
                    self._emit(f"[ainative] scheduler: ready slices {','.join(slice_def.id for slice_def in ready_slices)}")
                for slice_def in ready_slices:
                    if stop_launching or len(running) >= max(1, self.config.workspace.parallel_workers):
                        break
                    if slice_def.id in running_ids:
                        continue
                    running_ids.add(slice_def.id)
                    future = executor.submit(self._run_slice_pipeline, state, spec_path.resolve(), slice_def, dry_run_pr)
                    running[future] = slice_def.id
                if not running:
                    break
                done, _ = wait(set(running), return_when=FIRST_COMPLETED)
                for future in done:
                    slice_id = running.pop(future)
                    running_ids.discard(slice_id)
                    try:
                        result = future.result()
                        for stage, paths in result.items():
                            collected[stage].extend(paths)
                    except StageError:
                        stop_launching = True
                    except Exception as exc:  # pragma: no cover - defensive fallback
                        stop_launching = True
                        self._mark_slice_failure(state, slice_id, state.slice_states[slice_id].current_stage or "loop", str(exc))
                        self._emit(f"[ainative] slice {slice_id}: failed - {exc}")

        refreshed = self._state_store(run_dir=Path(state.run_dir)).load(Path(state.run_dir))
        self._sync_state(state, refreshed)

        if any(slice_state.status == "failed" for slice_state in state.slice_states.values()):
            state.scheduler_status = "failed"
            state.status = "failed"
            self._mutate_state(state, lambda locked: (setattr(locked, "scheduler_status", "failed"), setattr(locked, "status", "failed")))
        elif all(slice_state.status == "pr_opened" for slice_state in state.slice_states.values()):
            state.scheduler_status = "completed"
            self._mutate_state(state, lambda locked: setattr(locked, "scheduler_status", "completed"))
        else:
            state.scheduler_status = "running"
            self._mutate_state(state, lambda locked: setattr(locked, "scheduler_status", "running"))

        summary_artifacts = self._scheduler_summary_artifacts(state, slice_plan)
        loop_artifacts = self._aggregate_stage_artifacts(state, "loop", collected)
        verify_artifacts = self._aggregate_stage_artifacts(state, "verify", collected)
        commit_artifacts = self._aggregate_stage_artifacts(state, "commit", collected)
        pr_artifacts = self._aggregate_stage_artifacts(state, "pr", collected) + summary_artifacts

        self._state_store(run_dir=Path(state.run_dir)).update_stage(state, stage="loop", status="completed", artifacts=loop_artifacts)
        self._state_store(run_dir=Path(state.run_dir)).update_stage(state, stage="verify", status="completed", artifacts=verify_artifacts)
        self._state_store(run_dir=Path(state.run_dir)).update_stage(state, stage="commit", status="completed", artifacts=commit_artifacts)
        self._state_store(run_dir=Path(state.run_dir)).update_stage(state, stage="pr", status="completed", artifacts=list(dict.fromkeys(pr_artifacts)))

        refreshed = self._state_store(run_dir=Path(state.run_dir)).load(Path(state.run_dir))
        self._sync_state(state, refreshed)
        return state

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from ai_native.config import AppConfig
from ai_native.orchestrator import WorkflowOrchestrator
from ai_native.state import StateStore


def _config_path() -> Path:
    return Path("ainative.yaml").resolve()


def _load_config() -> AppConfig:
    return AppConfig.load(_config_path())


def _state_store(config: AppConfig, workspace_root: Path | None = None, run_dir: Path | None = None) -> StateStore:
    if run_dir is not None:
        return StateStore(run_dir.resolve().parent)
    resolved_workspace = workspace_root.resolve() if workspace_root is not None else config.repo_root
    return StateStore(config.resolve_artifacts_dir(resolved_workspace))


def _resolve_workspace_root(config: AppConfig, workspace_dir: str | None) -> Path:
    return (Path(workspace_dir).resolve() if workspace_dir else config.repo_root)


def _resolve_spec_path(config: AppConfig, spec: str, workspace_root: Path) -> Path:
    spec_path = Path(spec)
    if spec_path.is_absolute():
        resolved = spec_path.resolve()
        if resolved.exists():
            return resolved
        raise SystemExit(f"Spec file not found: {resolved}")

    candidates: list[Path] = []
    for base in (workspace_root, config.repo_root):
        candidate = (base / spec_path).resolve()
        if candidate not in candidates:
            candidates.append(candidate)
        if candidate.exists():
            return candidate

    searched = "\n".join(f"- {candidate}" for candidate in candidates)
    raise SystemExit(
        "Spec file not found. Checked:\n"
        f"{searched}\n"
        "Pass an absolute path, place the spec under TARGET_DIR, or keep it in the template repo and pass the same relative path."
    )


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _ask_questions(stage: str, questions: list[str]) -> list[str]:
    if not questions:
        return []
    if not sys.stdin.isatty():
        raise SystemExit(f"{stage} requires user input, but stdin is not interactive.")
    answers: list[str] = []
    print(f"[ainative] {stage}: clarification needed", flush=True)
    for index, question in enumerate(questions, start=1):
        print(f"[ainative] {stage}: question {index}/{len(questions)}", flush=True)
        print(question, flush=True)
        answers.append(input("> ").strip())
    return answers


def command_doctor(_: argparse.Namespace) -> int:
    config = _load_config()
    checks = {
        "codex": shutil.which("codex"),
        "gh": shutil.which("gh"),
        "git": shutil.which("git"),
        "uv": shutil.which("uv"),
        "mmdc": shutil.which("mmdc"),
        "codex_auth": str(Path.home() / ".codex" / "auth.json"),
        "codex_config": str(Path.home() / ".codex" / "config.toml"),
        "ssh_dir": str(Path.home() / ".ssh"),
        "gitconfig": str(Path.home() / ".gitconfig"),
        "gh_config_dir": str(Path.home() / ".config" / "gh"),
        "artifacts_dir": str(config.workspace.artifacts_dir),
    }
    payload = {
        "commands": {name: bool(path) for name, path in checks.items() if name in {"codex", "gh", "git", "uv", "mmdc"}},
        "paths": {
            name: Path(path).exists()
            for name, path in checks.items()
            if name not in {"codex", "gh", "git", "uv", "mmdc", "artifacts_dir"}
        },
        "artifacts_dir": str(config.workspace.artifacts_dir),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = _load_config()
    orchestrator = WorkflowOrchestrator(config, progress=_print_progress, question_responder=_ask_questions)
    workspace_root = _resolve_workspace_root(config, args.workspace_dir)
    state = orchestrator.run_all(
        _resolve_spec_path(config, args.spec, workspace_root),
        run_dir=Path(args.run_dir).resolve() if args.run_dir else None,
        dry_run_pr=args.dry_run_pr,
        workspace_root=workspace_root,
    )
    print(Path(state.run_dir))
    return 0


def command_stage(args: argparse.Namespace) -> int:
    config = _load_config()
    orchestrator = WorkflowOrchestrator(config, progress=_print_progress, question_responder=_ask_questions)
    workspace_root = _resolve_workspace_root(config, args.workspace_dir)
    run_kwargs = {
        "spec_path": _resolve_spec_path(config, args.spec, workspace_root),
        "target_stage": args.stage,
        "run_dir": Path(args.run_dir).resolve() if args.run_dir else None,
        "dry_run_pr": args.dry_run_pr,
        "workspace_root": workspace_root,
    }
    if getattr(args, "slice_id", None):
        run_kwargs["slice_id"] = args.slice_id
    state = orchestrator.run_until(
        **run_kwargs,
    )
    print(Path(state.run_dir))
    return 0


def _run_slice_stage(args: argparse.Namespace, stage_name: str, *, dry_run_pr: bool = False) -> int:
    args.stage = stage_name
    args.dry_run_pr = dry_run_pr
    return command_stage(args)


def command_review(args: argparse.Namespace) -> int:
    config = _load_config()
    orchestrator = WorkflowOrchestrator(config, progress=_print_progress, question_responder=_ask_questions)
    workspace_root = _resolve_workspace_root(config, args.workspace_dir)
    spec_path = _resolve_spec_path(config, args.spec, workspace_root)
    explicit_run_dir = Path(args.run_dir).resolve() if args.run_dir else None
    state_store = _state_store(config, workspace_root=workspace_root, run_dir=explicit_run_dir)
    run_dir = explicit_run_dir or state_store.find_latest_for_spec(spec_path, workspace_root)
    if run_dir is None:
        raise SystemExit("No matching run found for spec.")
    if hasattr(run_dir, "run_dir"):
        run_dir = Path(run_dir.run_dir)
    state = _state_store(config, workspace_root=workspace_root, run_dir=Path(run_dir)).load(Path(run_dir))
    if args.target == "pr":
        orchestrator.run_until(spec_path, "pr", run_dir=Path(state.run_dir), dry_run_pr=True, workspace_root=workspace_root)
    else:
        orchestrator.run_until(spec_path, args.target, run_dir=Path(state.run_dir), dry_run_pr=True, workspace_root=workspace_root)
    print(Path(state.run_dir) / args.target)
    return 0


def command_pr(args: argparse.Namespace) -> int:
    config = _load_config()
    orchestrator = WorkflowOrchestrator(config, progress=_print_progress, question_responder=_ask_questions)
    workspace_root = _resolve_workspace_root(config, args.workspace_dir)
    run_kwargs = {
        "spec_path": _resolve_spec_path(config, args.spec, workspace_root),
        "target_stage": "pr",
        "run_dir": Path(args.run_dir).resolve() if args.run_dir else None,
        "dry_run_pr": args.dry_run,
        "workspace_root": workspace_root,
    }
    if args.slice_id:
        run_kwargs["slice_id"] = args.slice_id
    state = orchestrator.run_until(**run_kwargs)
    print(Path(state.run_dir) / "pr")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ainative")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(func=command_doctor)

    run = subparsers.add_parser("run")
    run.add_argument("--spec", required=True)
    run.add_argument("--workspace-dir")
    run.add_argument("--run-dir")
    run.add_argument("--dry-run-pr", action="store_true")
    run.set_defaults(func=command_run)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--spec", required=True)
    stage.add_argument("--workspace-dir")
    stage.add_argument("--stage", required=True, choices=["plan", "architecture", "prd", "slice", "loop", "verify", "commit", "pr"])
    stage.add_argument("--run-dir")
    stage.add_argument("--dry-run-pr", action="store_true")
    stage.add_argument("--slice-id")
    stage.set_defaults(func=command_stage)

    loop = subparsers.add_parser("loop")
    loop.add_argument("--spec", required=True)
    loop.add_argument("--workspace-dir")
    loop.add_argument("--run-dir")
    loop.add_argument("--slice-id")
    loop.set_defaults(func=lambda args: _run_slice_stage(args, "loop"))

    verify = subparsers.add_parser("verify")
    verify.add_argument("--spec", required=True)
    verify.add_argument("--workspace-dir")
    verify.add_argument("--run-dir")
    verify.add_argument("--slice-id")
    verify.set_defaults(func=lambda args: _run_slice_stage(args, "verify"))

    commit = subparsers.add_parser("commit")
    commit.add_argument("--spec", required=True)
    commit.add_argument("--workspace-dir")
    commit.add_argument("--run-dir")
    commit.add_argument("--slice-id")
    commit.set_defaults(func=lambda args: _run_slice_stage(args, "commit"))

    review = subparsers.add_parser("review")
    review.add_argument("--spec", required=True)
    review.add_argument("--workspace-dir")
    review.add_argument("--target", required=True, choices=["plan", "architecture", "prd", "slice", "verify", "pr"])
    review.add_argument("--run-dir")
    review.set_defaults(func=command_review)

    pr = subparsers.add_parser("pr")
    pr.add_argument("--spec", required=True)
    pr.add_argument("--workspace-dir")
    pr.add_argument("--run-dir")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--slice-id")
    pr.set_defaults(func=command_pr)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

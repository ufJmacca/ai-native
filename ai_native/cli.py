from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from ai_native.config import AppConfig
from ai_native.orchestrator import WorkflowOrchestrator
from ai_native.state import StateStore

_AUTH_TYPES = ("api_key", "bearer", "basic", "none")
_SECRET_KEYS = {"api_key", "token", "password"}


def _config_path() -> Path:
    return _discover_config_path()


def _discover_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    env_path = os.environ.get("AINATIVE_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()

    current = Path.cwd().resolve()
    for base in (current, *current.parents):
        candidate = base / "ainative.yaml"
        if candidate.exists():
            return candidate.resolve()
    return (current / "ainative.yaml").resolve()


def _load_config(config_path: str | None = None) -> AppConfig:
    return AppConfig.load(_discover_config_path(config_path))


def _state_store(config: AppConfig, workspace_root: Path | None = None, run_dir: Path | None = None) -> StateStore:
    if run_dir is not None:
        return StateStore(run_dir.resolve().parent)
    resolved_workspace = workspace_root.resolve() if workspace_root is not None else config.repo_root
    return StateStore(config.resolve_artifacts_dir(resolved_workspace))


def _resolve_workspace_root(_config: AppConfig, workspace_dir: str | None) -> Path:
    return (Path(workspace_dir).resolve() if workspace_dir else Path.cwd().resolve())


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


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _masked_telemetry_payload(config: AppConfig) -> dict[str, Any]:
    telemetry = config.telemetry
    payload = telemetry.model_dump()
    for key in _SECRET_KEYS:
        payload[key] = _mask_secret(payload.get(key))
    return payload


def _build_auth_header(auth_type: str, telemetry: dict[str, Any]) -> dict[str, str]:
    if auth_type == "api_key":
        key = telemetry.get("api_key")
        if not key:
            raise SystemExit("Telemetry auth_type=api_key requires an api_key.")
        return {"X-API-Key": key}
    if auth_type == "bearer":
        token = telemetry.get("token")
        if not token:
            raise SystemExit("Telemetry auth_type=bearer requires a token.")
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "basic":
        username = telemetry.get("username")
        password = telemetry.get("password")
        if not username or not password:
            raise SystemExit("Telemetry auth_type=basic requires both username and password.")
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {}


def _load_raw_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_raw_config_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _prompt_if_missing(current: str | None, prompt: str, *, secret: bool = False) -> str | None:
    if current is not None:
        return current
    if not sys.stdin.isatty():
        return None
    if secret:
        import getpass

        entered = getpass.getpass(f"{prompt}: ").strip()
    else:
        entered = input(f"{prompt}: ").strip()
    return entered or None


def _validate_telemetry_auth_credentials(auth_type: str, telemetry_data: dict[str, Any]) -> None:
    if auth_type == "api_key" and not telemetry_data.get("api_key"):
        raise SystemExit("Telemetry auth_type=api_key requires --api-key (or an existing stored api_key).")
    if auth_type == "bearer" and not telemetry_data.get("token"):
        raise SystemExit("Telemetry auth_type=bearer requires --token (or an existing stored token).")
    if auth_type == "basic":
        if not telemetry_data.get("username") or not telemetry_data.get("password"):
            raise SystemExit(
                "Telemetry auth_type=basic requires --username and --password (or existing stored credentials)."
            )


def command_telemetry_configure(args: argparse.Namespace) -> int:
    config_path = _discover_config_path(args.config)
    raw_config = _load_raw_config_file(config_path)
    telemetry_data: dict[str, Any] = dict((raw_config.get("telemetry") or {}))

    url = _prompt_if_missing(args.url, "Telemetry URL")
    auth_type = args.auth_type
    if auth_type is None and sys.stdin.isatty():
        auth_type = _prompt_if_missing(None, f"Auth type ({', '.join(_AUTH_TYPES)})")
    auth_type = auth_type or telemetry_data.get("auth_type") or "none"
    if auth_type not in _AUTH_TYPES:
        raise SystemExit(f"Invalid auth type: {auth_type}")

    telemetry_data["url"] = url or telemetry_data.get("url")
    telemetry_data["auth_type"] = auth_type
    telemetry_data["tenant"] = args.tenant if args.tenant is not None else telemetry_data.get("tenant")

    if auth_type == "api_key":
        api_key = _prompt_if_missing(args.api_key, "API key", secret=True)
        telemetry_data["api_key"] = api_key or telemetry_data.get("api_key")
        telemetry_data["token"] = None
        telemetry_data["username"] = None
        telemetry_data["password"] = None
    elif auth_type == "bearer":
        token = _prompt_if_missing(args.token, "Bearer token", secret=True)
        telemetry_data["token"] = token or telemetry_data.get("token")
        telemetry_data["api_key"] = None
        telemetry_data["username"] = None
        telemetry_data["password"] = None
    elif auth_type == "basic":
        username = _prompt_if_missing(args.username, "Username")
        password = _prompt_if_missing(args.password, "Password", secret=True)
        telemetry_data["username"] = username or telemetry_data.get("username")
        telemetry_data["password"] = password or telemetry_data.get("password")
        telemetry_data["api_key"] = None
        telemetry_data["token"] = None
    else:
        telemetry_data["api_key"] = None
        telemetry_data["token"] = None
        telemetry_data["username"] = None
        telemetry_data["password"] = None

    _validate_telemetry_auth_credentials(auth_type, telemetry_data)

    has_remote = bool(telemetry_data.get("url"))
    telemetry_data["enabled"] = args.enabled if args.enabled is not None else has_remote

    raw_config["telemetry"] = telemetry_data
    _write_raw_config_file(config_path, raw_config)

    loaded = AppConfig.load(config_path)
    masked = _masked_telemetry_payload(loaded)
    print(f"[ainative] telemetry configuration saved to {config_path}")
    print(json.dumps(masked, indent=2, sort_keys=True))
    return 0


def command_telemetry_show(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    print(json.dumps(_masked_telemetry_payload(config), indent=2, sort_keys=True))
    return 0


def command_telemetry_test(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    telemetry = config.telemetry
    if not telemetry.url:
        raise SystemExit("Telemetry URL is not configured. Set it with `ainative telemetry configure --url ...`.")

    headers = {"User-Agent": "ai-native/telemetry-test", "Accept": "application/json"}
    headers.update(_build_auth_header(telemetry.auth_type, telemetry.model_dump()))
    if telemetry.tenant:
        headers["X-Tenant"] = telemetry.tenant

    request = urllib.request.Request(telemetry.url, method="GET", headers=headers)
    timeout = args.timeout
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(512).decode("utf-8", errors="replace")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": response.status,
                        "url": telemetry.url,
                        "tenant": telemetry.tenant,
                        "body_preview": body,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except urllib.error.HTTPError as exc:
        preview = exc.read(512).decode("utf-8", errors="replace")
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": exc.code,
                    "url": telemetry.url,
                    "error": str(exc),
                    "body_preview": preview,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    except urllib.error.URLError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "url": telemetry.url,
                    "error": str(exc.reason),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1


def command_doctor(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
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
        "config_path": str(config.config_path),
        "config_exists": config.config_path.exists(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
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
    config = _load_config(args.config)
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
    config = _load_config(args.config)
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
    config = _load_config(args.config)
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
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config")

    doctor = subparsers.add_parser("doctor", parents=[common])
    doctor.set_defaults(func=command_doctor)

    run = subparsers.add_parser("run", parents=[common])
    run.add_argument("--spec", required=True)
    run.add_argument("--workspace-dir")
    run.add_argument("--run-dir")
    run.add_argument("--dry-run-pr", action="store_true")
    run.set_defaults(func=command_run)

    stage = subparsers.add_parser("stage", parents=[common])
    stage.add_argument("--spec", required=True)
    stage.add_argument("--workspace-dir")
    stage.add_argument("--stage", required=True, choices=["plan", "architecture", "prd", "slice", "loop", "verify", "commit", "pr"])
    stage.add_argument("--run-dir")
    stage.add_argument("--dry-run-pr", action="store_true")
    stage.add_argument("--slice-id")
    stage.set_defaults(func=command_stage)

    loop = subparsers.add_parser("loop", parents=[common])
    loop.add_argument("--spec", required=True)
    loop.add_argument("--workspace-dir")
    loop.add_argument("--run-dir")
    loop.add_argument("--slice-id")
    loop.set_defaults(func=lambda args: _run_slice_stage(args, "loop"))

    verify = subparsers.add_parser("verify", parents=[common])
    verify.add_argument("--spec", required=True)
    verify.add_argument("--workspace-dir")
    verify.add_argument("--run-dir")
    verify.add_argument("--slice-id")
    verify.set_defaults(func=lambda args: _run_slice_stage(args, "verify"))

    commit = subparsers.add_parser("commit", parents=[common])
    commit.add_argument("--spec", required=True)
    commit.add_argument("--workspace-dir")
    commit.add_argument("--run-dir")
    commit.add_argument("--slice-id")
    commit.set_defaults(func=lambda args: _run_slice_stage(args, "commit"))

    review = subparsers.add_parser("review", parents=[common])
    review.add_argument("--spec", required=True)
    review.add_argument("--workspace-dir")
    review.add_argument("--target", required=True, choices=["plan", "architecture", "prd", "slice", "verify", "pr"])
    review.add_argument("--run-dir")
    review.set_defaults(func=command_review)

    pr = subparsers.add_parser("pr", parents=[common])
    pr.add_argument("--spec", required=True)
    pr.add_argument("--workspace-dir")
    pr.add_argument("--run-dir")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--slice-id")
    pr.set_defaults(func=command_pr)

    telemetry = subparsers.add_parser("telemetry", parents=[common])
    telemetry_subparsers = telemetry.add_subparsers(dest="telemetry_command", required=True)

    telemetry_configure = telemetry_subparsers.add_parser("configure")
    telemetry_configure.add_argument("--url")
    telemetry_configure.add_argument("--auth-type", choices=_AUTH_TYPES)
    telemetry_configure.add_argument("--api-key")
    telemetry_configure.add_argument("--token")
    telemetry_configure.add_argument("--username")
    telemetry_configure.add_argument("--password")
    telemetry_configure.add_argument("--tenant")
    telemetry_configure.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    telemetry_configure.set_defaults(func=command_telemetry_configure)

    telemetry_show = telemetry_subparsers.add_parser("show")
    telemetry_show.set_defaults(func=command_telemetry_show)

    telemetry_test = telemetry_subparsers.add_parser("test")
    telemetry_test.add_argument("--timeout", type=float, default=10.0)
    telemetry_test.set_defaults(func=command_telemetry_test)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

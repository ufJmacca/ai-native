from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Iterator, Mapping

from tests.factory_runner.integration._support import (
    FactoryInvocation,
    build_invocation,
    factory_command,
    factory_environment,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "factory_runner" / "runtime-golden"
)
GOLDEN_CLI = (
    REPOSITORY_ROOT / "tests" / "factory_runner" / "integration" / "_golden_cli.py"
)
GOLDEN_VARIANTS = (
    ("author-success", "author"),
    ("author-no-change", "author-no-change"),
)
GIT_TIMESTAMP = "2026-07-31T00:00:00Z"
GOLDEN_WORK_ROOT = Path("/tmp/ai-native-factory-runner-runtime-golden-v1")
GOLDEN_WORK_LOCK = Path("/tmp/ai-native-factory-runner-runtime-golden-v1.lock")
_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": GIT_TIMESTAMP,
    "GIT_COMMITTER_DATE": GIT_TIMESTAMP,
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "commit.gpgsign",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "core.autocrlf",
    "GIT_CONFIG_VALUE_1": "false",
    "GIT_CONFIG_KEY_2": "core.excludesfile",
    "GIT_CONFIG_VALUE_2": str(REPOSITORY_ROOT / ".gitignore"),
    "GIT_CONFIG_KEY_3": "core.filemode",
    "GIT_CONFIG_VALUE_3": "true",
    "LC_ALL": "C",
    "PYTHONHASHSEED": "0",
    "TZ": "UTC",
}


@contextmanager
def _deterministic_process_environment() -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in _GIT_ENVIRONMENT}
    os.environ.update(_GIT_ENVIRONMENT)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _deterministic_work_root() -> Iterator[Path]:
    with GOLDEN_WORK_LOCK.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if GOLDEN_WORK_ROOT.is_symlink():
            raise RuntimeError("runtime golden work root must not be a symlink")
        shutil.rmtree(GOLDEN_WORK_ROOT, ignore_errors=True)
        GOLDEN_WORK_ROOT.mkdir(mode=0o700)
        try:
            yield GOLDEN_WORK_ROOT
        finally:
            shutil.rmtree(GOLDEN_WORK_ROOT, ignore_errors=True)


def _configure_declared_command(
    invocation: FactoryInvocation,
    *,
    no_change: bool,
) -> None:
    payload = json.loads(invocation.run_spec_path.read_bytes())
    commands = payload["policy"]["allowed_commands"]
    if len(commands) != 1:
        raise RuntimeError("golden fixture must declare exactly one command")
    command = commands[0]
    command[0] = "python3"
    if no_change:
        command[-1] = "raise SystemExit(0)"
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _execute_variant(
    root: Path,
    *,
    variant: str,
    agent_mode: str,
    python_executable: Path | str,
) -> dict[str, bytes]:
    invocation = build_invocation(root / variant, operation="author")
    for cache_directory in invocation.workspace.rglob("__pycache__"):
        if cache_directory.is_dir() and not cache_directory.is_symlink():
            shutil.rmtree(cache_directory)
    _configure_declared_command(
        invocation,
        no_change=variant == "author-no-change",
    )
    command = [
        os.fspath(python_executable),
        str(GOLDEN_CLI),
        *factory_command(invocation)[3:],
    ]
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=factory_environment(invocation, agent_mode=agent_mode),
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if completed.returncode != 0 or completed.stdout:
        diagnostic = completed.stdout + completed.stderr
        result_path = invocation.output_dir / "result" / "run-result.json"
        if result_path.is_file():
            diagnostic += "\n" + result_path.read_text(encoding="utf-8")
        raise RuntimeError(
            f"{variant} golden CLI execution failed "
            f"with exit code {completed.returncode}:\n{diagnostic}"
        )

    rendered: dict[str, bytes] = {}
    for path in sorted(invocation.output_dir.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                f"{variant} golden output contains a non-regular artifact"
            )
        relative = path.relative_to(invocation.output_dir).as_posix()
        rendered[f"{variant}/{relative}"] = path.read_bytes()
    if not rendered:
        raise RuntimeError(f"{variant} golden CLI execution emitted no artifacts")
    return rendered


def render_runtime_golden_artifacts(
    *,
    python_executable: Path | str = sys.executable,
) -> dict[str, bytes]:
    """Execute both deterministic author outcomes and retain their full trees."""

    with _deterministic_process_environment():
        with _deterministic_work_root() as root:
            rendered: dict[str, bytes] = {}
            for variant, agent_mode in GOLDEN_VARIANTS:
                variant_artifacts = _execute_variant(
                    root,
                    variant=variant,
                    agent_mode=agent_mode,
                    python_executable=python_executable,
                )
                overlap = rendered.keys() & variant_artifacts.keys()
                if overlap:
                    raise RuntimeError(
                        "runtime golden artifact paths overlap between variants"
                    )
                rendered.update(variant_artifacts)
    expected_prefixes = {f"{variant}/" for variant, _mode in GOLDEN_VARIANTS}
    actual_prefixes = {f"{path.split('/', 1)[0]}/" for path in rendered if "/" in path}
    if actual_prefixes != expected_prefixes:
        raise RuntimeError("runtime golden variant inventory is incomplete")
    return dict(sorted(rendered.items()))


def runtime_golden_drift(
    output_dir: Path,
    *,
    expected: Mapping[str, bytes] | None = None,
) -> tuple[str, ...]:
    rendered = (
        dict(expected) if expected is not None else render_runtime_golden_artifacts()
    )
    actual: dict[str, bytes] = {}
    if output_dir.is_dir():
        for path in sorted(output_dir.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            relative = path.relative_to(output_dir).as_posix()
            if stat.S_ISREG(metadata.st_mode):
                actual[relative] = path.read_bytes()
            else:
                actual[relative] = b""

    differences: list[str] = []
    for filename, content in rendered.items():
        if filename not in actual:
            differences.append(f"missing: {filename}")
        elif actual[filename] != content:
            differences.append(f"changed: {filename}")
    for filename in sorted(actual.keys() - rendered.keys()):
        differences.append(f"extra: {filename}")
    return tuple(differences)


def _write_runtime_golden_artifacts(
    output_dir: Path,
    artifacts: Mapping[str, bytes],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_paths = set(artifacts)
    for path in sorted(output_dir.rglob("*"), reverse=True):
        relative = path.relative_to(output_dir).as_posix()
        if path.is_symlink() or (path.is_file() and relative not in expected_paths):
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    for filename, content in artifacts.items():
        path = output_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        path.write_bytes(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic real-command AN-03 runtime goldens.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in runtime golden corpus differs",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="write the complete runtime golden corpus (the default)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="override the runtime golden corpus directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    expected = render_runtime_golden_artifacts()
    if not args.check:
        _write_runtime_golden_artifacts(output_dir, expected)

    differences = runtime_golden_drift(output_dir, expected=expected)
    if differences:
        print("factory runner runtime golden drift detected:")
        for difference in differences:
            print(f"- {difference}")
        return 1

    action = "verified" if args.check else "generated"
    print(
        f"{action} {len(expected)} runtime golden artifacts "
        f"across {len(GOLDEN_VARIANTS)} author outcomes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

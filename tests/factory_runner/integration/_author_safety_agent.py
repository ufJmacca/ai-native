from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

from _fake_agent import AUTHORED_APP, _schema_payload


def _replace_path(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("safety fixture target unexpectedly became a directory")
    path.unlink()


def _attempt_secret() -> str:
    direct = os.environ.get("SERVICE_TOKEN")
    if direct is not None:
        return direct
    source = os.environ.get("ATTEMPT_GATEWAY_TOKEN_FILE")
    if source is None:
        raise RuntimeError("attempt secret fixture has no configured source")
    return Path(source).read_text(encoding="utf-8").rstrip("\r\n")


def _write_slice_artifacts(prompt: str) -> None:
    match = re.search(r"Slice artifact directory:\n(?P<path>.+)", prompt)
    if match is None:
        return
    slice_dir = Path(match.group("path").strip())
    slice_dir.mkdir(parents=True, exist_ok=True)
    (slice_dir / "red.log").write_text(
        "expected greeting assertion failed\n",
        encoding="utf-8",
    )
    (slice_dir / "green.log").write_text(
        "declared verification command passed\n",
        encoding="utf-8",
    )
    (slice_dir / "refactor-notes.md").write_text(
        "# Refactor Notes\n\nNo refactor was needed.\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "author",
            "hardlink",
            "invalid-mode",
            "secret-binary-repository",
            "secret-model-output",
            "secret-repository",
            "special-file",
            "symlink",
        ),
        required=True,
    )
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args()

    args.marker.parent.mkdir(parents=True, exist_ok=True)
    with args.marker.open("a", encoding="utf-8") as marker:
        marker.write(f"{args.mode}\n")

    output_path = Path(os.environ["AINATIVE_OUTPUT_FILE"])
    schema_value = os.environ.get("AINATIVE_SCHEMA_FILE")
    if schema_value:
        payload = _schema_payload(Path(schema_value).name, blocked=False)
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return 0

    prompt = Path(os.environ["AINATIVE_PROMPT_FILE"]).read_text(encoding="utf-8")
    workspace = Path.cwd()
    target = workspace / "app.py"
    if args.mode == "hardlink":
        outside = args.marker.with_name("outside-authored-app.py")
        outside.write_text(AUTHORED_APP, encoding="utf-8")
        _replace_path(target)
        os.link(outside, target)
    elif args.mode == "symlink":
        outside = args.marker.with_name("outside-authored-app.py")
        outside.write_text(AUTHORED_APP, encoding="utf-8")
        _replace_path(target)
        target.symlink_to(outside)
    elif args.mode == "special-file":
        _replace_path(target)
        os.mkfifo(target)
    elif args.mode == "invalid-mode":
        target.write_text(AUTHORED_APP, encoding="utf-8")
        target.chmod(0o600)
    elif args.mode == "secret-repository":
        target.write_text(
            AUTHORED_APP + f"# {_attempt_secret()}\n",
            encoding="utf-8",
        )
    elif args.mode == "secret-binary-repository":
        target.write_bytes(b"\x00binary-secret:" + _attempt_secret().encode())
    else:
        target.write_text(AUTHORED_APP, encoding="utf-8")

    _write_slice_artifacts(prompt)
    response = "# Safety acceptance fixture\n\nApplied the requested local change.\n"
    if args.mode == "secret-model-output":
        response += _attempt_secret() + "\n"
    output_path.write_text(response, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time


AUTHORED_APP = """def greeting(name: str) -> str:
    return f"Hello, {name}!"
"""


def _schema_payload(schema_name: str, *, blocked: bool) -> dict[str, object]:
    if schema_name == "context-report.json":
        return {
            "repo_state": "existing",
            "languages": ["python"],
            "manifests": [],
            "test_frameworks": [],
            "architecture_summary": "A deterministic single-module Python fixture.",
            "risks": [],
            "touched_areas": ["app.py"],
            "recommended_questions": [],
        }
    if schema_name == "question-batch.json":
        return {
            "needs_user_input": blocked,
            "summary": (
                "Acceptance criteria are missing."
                if blocked
                else "The supplied factory context is complete."
            ),
            "questions": (
                ["What observable behavior must the change provide?"] if blocked else []
            ),
        }
    if schema_name == "plan-artifact.json":
        return {
            "title": "Deterministic greeting change",
            "summary": "Change the tracked greeting punctuation.",
            "implementation_steps": ["Update app.py"],
            "interfaces": ["greeting(name: str) -> str"],
            "data_flow": ["name -> greeting -> formatted string"],
            "edge_cases": ["Empty names remain representable"],
            "test_strategy": ["Run the declared deterministic command"],
            "rollout_notes": ["No publication from the attempt sandbox"],
        }
    if schema_name == "diagram-artifact.json":
        return {
            "title": "Greeting flow",
            "diagram": "flowchart LR\n  Name --> Greeting",
            "legend": ["Greeting formats the supplied name"],
            "assumptions": ["The function remains synchronous"],
        }
    if schema_name == "prd-artifact.json":
        return {
            "title": "Greeting punctuation",
            "user_value": "The greeting uses the requested punctuation.",
            "scope": ["Update app.py"],
            "constraints": ["Do not commit or publish"],
            "acceptance_criteria": ["greeting('Codex') returns 'Hello, Codex!'"],
            "out_of_scope": ["Additional greeting formats"],
        }
    if schema_name == "slice-plan.json":
        return {
            "title": "Greeting implementation",
            "summary": "One deterministic vertical slice.",
            "slices": [
                {
                    "id": "S001",
                    "name": "Update greeting punctuation",
                    "goal": "Return the requested greeting.",
                    "acceptance_criteria": [
                        "greeting('Codex') returns 'Hello, Codex!'"
                    ],
                    "file_impact": ["app.py"],
                    "test_plan": ["Run the declared verification command"],
                    "dependencies": [],
                }
            ],
        }
    if schema_name == "review-report.json":
        return {
            "verdict": "approved",
            "summary": "The deterministic fixture satisfies its criterion.",
            "findings": [],
            "required_changes": [],
        }
    if schema_name == "verification-report.json":
        return {
            "verdict": "passed",
            "summary": "The declared deterministic verification passed.",
            "acceptance_checks": ["greeting('Codex') returns 'Hello, Codex!'"],
            "evidence": ["declared verification command"],
            "gaps": [],
        }
    raise ValueError(f"unsupported fake-agent schema: {schema_name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "assert-first-prompt-context",
            "author",
            "author-add",
            "author-binary",
            "author-delete",
            "author-mode",
            "author-no-change",
            "author-pause-verify",
            "author-rename",
            "author-secret",
            "blocked",
            "fail-if-called",
            "mutate-git-config",
            "sleep",
        ),
        required=True,
    )
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument(
        "--required-first-prompt-text",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    first_call = not args.marker.exists()
    args.marker.parent.mkdir(parents=True, exist_ok=True)
    with args.marker.open("a", encoding="utf-8") as marker:
        marker.write(f"{args.mode}\n")

    schema_value = os.environ.get("AINATIVE_SCHEMA_FILE")
    if args.mode == "fail-if-called":
        return 97
    if args.mode == "sleep":
        time.sleep(30)
        return 98
    if (
        args.mode == "author-pause-verify"
        and schema_value
        and Path(schema_value).name == "verification-report.json"
    ):
        args.marker.with_name("verification-agent.started").write_text("started")
        time.sleep(30)
        return 98
    if args.mode == "mutate-git-config":
        with (Path.cwd() / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write("\n[factory-escape]\n\tattempted = true\n")

    prompt_path = Path(os.environ["AINATIVE_PROMPT_FILE"])
    output_path = Path(os.environ["AINATIVE_OUTPUT_FILE"])
    prompt = prompt_path.read_text(encoding="utf-8")
    if args.mode == "assert-first-prompt-context" and first_call:
        missing = [
            value for value in args.required_first_prompt_text if value not in prompt
        ]
        if missing:
            return 96

    if schema_value:
        payload = _schema_payload(
            Path(schema_value).name,
            blocked=args.mode == "blocked",
        )
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return 0

    workspace = Path.cwd()
    if args.mode in {
        "author",
        "assert-first-prompt-context",
        "author-pause-verify",
        "mutate-git-config",
    }:
        (workspace / "app.py").write_text(AUTHORED_APP, encoding="utf-8")
    elif args.mode == "author-add":
        (workspace / "added.txt").write_text("factory addition\n", encoding="utf-8")
    elif args.mode == "author-delete":
        (workspace / "app.py").unlink(missing_ok=True)
    elif args.mode == "author-rename":
        source = workspace / "app.py"
        target = workspace / "renamed.py"
        if source.exists():
            source.rename(target)
    elif args.mode == "author-binary":
        (workspace / "app.py").write_bytes(b"\x00factory-binary\xff\n")
    elif args.mode == "author-mode":
        (workspace / "app.py").chmod(0o755)
    elif args.mode == "author-secret":
        (workspace / "app.py").write_text(
            "FACTORY_SECRET_CANARY_AN03_8f4d1c7e\n",
            encoding="utf-8",
        )

    match = re.search(r"Slice artifact directory:\n(?P<path>.+)", prompt)
    if match:
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

    output_path.write_text(
        "# Deterministic factory agent\n\nUpdated app.py without publication.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


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
        choices=("author", "blocked", "fail-if-called"),
        required=True,
    )
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args()

    args.marker.parent.mkdir(parents=True, exist_ok=True)
    with args.marker.open("a", encoding="utf-8") as marker:
        marker.write(f"{args.mode}\n")

    if args.mode == "fail-if-called":
        return 97

    prompt_path = Path(os.environ["AINATIVE_PROMPT_FILE"])
    output_path = Path(os.environ["AINATIVE_OUTPUT_FILE"])
    prompt = prompt_path.read_text(encoding="utf-8")
    schema_value = os.environ.get("AINATIVE_SCHEMA_FILE")

    if schema_value:
        payload = _schema_payload(
            Path(schema_value).name,
            blocked=args.mode == "blocked",
        )
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return 0

    workspace = Path.cwd()
    (workspace / "app.py").write_text(AUTHORED_APP, encoding="utf-8")

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

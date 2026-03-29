from __future__ import annotations

import json
import re
from pathlib import Path

from ai_native.adapters.base import AgentResult


class FakeWorkflowAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls.append(
            {"mode": "run", "prompt": prompt, "cwd": cwd, "schema_path": schema_path, "image_paths": image_paths or []}
        )
        if schema_path:
            payload = self._payload_for_schema(schema_path.name)
            return AgentResult(text=json.dumps(payload), json_data=payload)

        return self._text_result(prompt)

    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        self.calls.append({"mode": "review", "prompt": prompt, "cwd": cwd, "base_branch": base_branch})
        return self._text_result(prompt)

    @staticmethod
    def _text_result(prompt: str) -> AgentResult:
        match = re.search(r"Slice artifact directory:\n(?P<path>.+)", prompt)
        if match:
            slice_dir = Path(match.group("path").strip())
            slice_dir.mkdir(parents=True, exist_ok=True)
            (slice_dir / "red.log").write_text("failing test output\n", encoding="utf-8")
            (slice_dir / "green.log").write_text("passing test output\n", encoding="utf-8")
            (slice_dir / "refactor-notes.md").write_text("# Refactor Notes\n- none\n", encoding="utf-8")
            return AgentResult(text="# Builder Summary\nImplemented the slice.")

        return AgentResult(text="# Review\nNo blocking issues found.")

    @staticmethod
    def _payload_for_schema(name: str) -> dict[str, object]:
        if name == "context-report.json":
            return {
                "repo_state": "existing",
                "languages": ["python"],
                "manifests": ["pyproject.toml"],
                "test_frameworks": ["pytest"],
                "architecture_summary": "The repository uses a Python workflow engine with prompt and schema assets.",
                "risks": ["Live agent execution depends on local Codex auth being mounted."],
                "touched_areas": ["ai_native", "tests", "docs"],
                "recommended_questions": [],
            }
        if name == "plan-artifact.json":
            return {
                "title": "Todo API Plan",
                "summary": "Implement a small JSON API with strong test coverage and documented developer ergonomics.",
                "implementation_steps": ["Add API module", "Add persistence", "Add tests"],
                "interfaces": ["POST /todos", "GET /todos", "PATCH /todos/{id}", "DELETE /todos/{id}"],
                "data_flow": ["Request enters API", "Service updates persistence", "Response returns JSON"],
                "edge_cases": ["Missing todo IDs", "Concurrent updates"],
                "test_strategy": ["Unit tests for service logic", "Integration tests for HTTP behavior"],
                "rollout_notes": ["Ship behind example feature branch"],
            }
        if name == "question-batch.json":
            return {
                "needs_user_input": False,
                "summary": "The spec and repo context are sufficient for planning.",
                "questions": [],
            }
        if name == "diagram-artifact.json":
            return {
                "title": "Todo API Architecture",
                "diagram": "flowchart TD\n  Client-->API\n  API-->Store",
                "legend": ["Client sends HTTP requests", "Store is a lightweight persistence layer"],
                "assumptions": ["Single-process deployment for v1"],
            }
        if name == "prd-artifact.json":
            return {
                "title": "Todo API PRD",
                "user_value": "Developers can evaluate the template against a concrete API feature.",
                "scope": ["CRUD todo endpoints", "Simple persistence", "Documentation"],
                "constraints": ["Keep implementation intentionally small"],
                "acceptance_criteria": ["CRUD endpoints exist", "Automated tests exist"],
                "out_of_scope": ["Multi-user collaboration"],
            }
        if name == "slice-plan.json":
            return {
                "title": "Todo API Slices",
                "summary": "One vertical slice to prove the workflow.",
                "slices": [
                    {
                        "id": "S001",
                        "name": "Create and list todos",
                        "goal": "Implement the first todo endpoints.",
                        "acceptance_criteria": ["Can create a todo", "Can list todos"],
                        "file_impact": ["app/api.py", "tests/test_api.py"],
                        "test_plan": ["Write request-level tests first"],
                        "dependencies": [],
                    }
                ],
            }
        if name == "review-report.json":
            return {
                "verdict": "approved",
                "summary": "The artifact is concrete and implementable.",
                "findings": [],
                "required_changes": [],
            }
        if name == "reference-context.json":
            return {
                "workflow_profile": "reference_driven_web",
                "summary": "Reference-driven landing page with bold type and consistent card rhythm.",
                "design_intent": "Translate the supplied references into a faithful implementation.",
                "stable_patterns": ["Large hero headline", "Alternating content sections"],
                "typography": ["Display headline with tight leading", "Body copy uses medium-weight sans serif"],
                "colors": ["Warm neutral base", "Dark text on light cards"],
                "spacing": ["Generous vertical rhythm", "Tight card padding"],
                "layout_patterns": ["Wide desktop grid", "Single-column mobile stacking"],
                "repeated_components": ["Feature cards", "Primary CTA buttons"],
                "responsive_behaviors": ["Collapse to one column on mobile", "Preserve hero hierarchy across breakpoints"],
                "fidelity_constraints": ["Do not reorder major sections", "Keep typography scale close to the reference"],
            }
        if name == "verification-report.json":
            return {
                "verdict": "passed",
                "summary": "The slice has the expected evidence and acceptance checks.",
                "acceptance_checks": ["Artifact evidence exists", "No known blockers remain"],
                "evidence": ["red.log", "green.log", "refactor-notes.md"],
                "gaps": [],
            }
        raise AssertionError(f"Unexpected schema requested: {name}")

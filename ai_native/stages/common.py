from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai_native.adapters.base import AgentAdapter, ReviewAdapter
from ai_native.config import AppConfig
from ai_native.models import DiagramArtifact, PlanArtifact, PRDArtifact, ReviewReport, SlicePlan, VerificationReport, ContextReport
from ai_native.prompting import PromptLibrary
from ai_native.state import StateStore
from ai_native.utils import render_bullets, write_json, write_text


class StageError(RuntimeError):
    pass


@dataclass
class ExecutionContext:
    config: AppConfig
    prompt_library: PromptLibrary
    state_store: StateStore
    template_root: Path
    repo_root: Path
    spec_path: Path
    run_dir: Path
    builder: AgentAdapter
    critic: AgentAdapter
    verifier: AgentAdapter
    pr_reviewer: AgentAdapter | ReviewAdapter
    emit_progress: Callable[[str], None] = field(default=lambda _message: None, repr=False)


def dump_model(path: Path, model: Any) -> None:
    write_json(path, model.model_dump(mode="json"))


def render_context_markdown(report: ContextReport) -> str:
    return "\n".join(
        [
            "# Context Report",
            "",
            f"- Repo state: `{report.repo_state}`",
            f"- Languages: {', '.join(report.languages) or 'none'}",
            f"- Manifests: {', '.join(report.manifests) or 'none'}",
            f"- Test frameworks: {', '.join(report.test_frameworks) or 'none'}",
            "",
            "## Architecture Summary",
            report.architecture_summary,
            "",
            "## Risks",
            render_bullets(report.risks),
            "",
            "## Touched Areas",
            render_bullets(report.touched_areas),
            "",
            "## Recommended Questions",
            render_bullets(report.recommended_questions),
        ]
    )


def render_plan_markdown(plan: PlanArtifact) -> str:
    return "\n".join(
        [
            f"# {plan.title}",
            "",
            "## Summary",
            plan.summary,
            "",
            "## Implementation Steps",
            render_bullets(plan.implementation_steps),
            "",
            "## Interfaces",
            render_bullets(plan.interfaces),
            "",
            "## Data Flow",
            render_bullets(plan.data_flow),
            "",
            "## Edge Cases",
            render_bullets(plan.edge_cases),
            "",
            "## Test Strategy",
            render_bullets(plan.test_strategy),
            "",
            "## Rollout Notes",
            render_bullets(plan.rollout_notes),
        ]
    )


def render_review_markdown(review: ReviewReport) -> str:
    return "\n".join(
        [
            "# Review",
            "",
            f"- Verdict: `{review.verdict}`",
            "",
            "## Summary",
            review.summary,
            "",
            "## Findings",
            render_bullets(review.findings),
            "",
            "## Required Changes",
            render_bullets(review.required_changes),
        ]
    )


def render_prd_markdown(prd: PRDArtifact) -> str:
    return "\n".join(
        [
            f"# {prd.title}",
            "",
            "## User Value",
            prd.user_value,
            "",
            "## Scope",
            render_bullets(prd.scope),
            "",
            "## Constraints",
            render_bullets(prd.constraints),
            "",
            "## Acceptance Criteria",
            render_bullets(prd.acceptance_criteria),
            "",
            "## Out Of Scope",
            render_bullets(prd.out_of_scope),
        ]
    )


def render_slice_markdown(plan: SlicePlan) -> str:
    lines = [
        f"# {plan.title}",
        "",
        "## Summary",
        plan.summary,
        "",
        "## Slices",
    ]
    for item in plan.slices:
        lines.extend(
            [
                "",
                f"### {item.id}: {item.name}",
                "",
                item.goal,
                "",
                "Acceptance criteria:",
                render_bullets(item.acceptance_criteria),
                "",
                "File impact:",
                render_bullets(item.file_impact),
                "",
                "Test plan:",
                render_bullets(item.test_plan),
                "",
                "Dependencies:",
                render_bullets(item.dependencies),
            ]
        )
    return "\n".join(lines)


def render_verification_markdown(report: VerificationReport) -> str:
    return "\n".join(
        [
            "# Verification",
            "",
            f"- Verdict: `{report.verdict}`",
            "",
            "## Summary",
            report.summary,
            "",
            "## Acceptance Checks",
            render_bullets(report.acceptance_checks),
            "",
            "## Evidence",
            render_bullets(report.evidence),
            "",
            "## Gaps",
            render_bullets(report.gaps),
        ]
    )


def write_review(review_path: Path, review: ReviewReport) -> None:
    dump_model(review_path.with_suffix(".json"), review)
    write_text(review_path, render_review_markdown(review))


def write_diagram_artifacts(base_dir: Path, artifact: DiagramArtifact) -> list[Path]:
    json_path = base_dir / "architecture.json"
    diagram_path = base_dir / "architecture.mmd"
    doc_path = base_dir / "architecture.md"
    dump_model(json_path, artifact)
    write_text(diagram_path, artifact.diagram.rstrip() + "\n")
    lines = [
        f"# {artifact.title}",
        "",
        "## Legend",
        render_bullets(artifact.legend),
        "",
        "## Assumptions",
        render_bullets(artifact.assumptions),
    ]
    write_text(doc_path, "\n".join(lines))
    return [json_path, diagram_path, doc_path]

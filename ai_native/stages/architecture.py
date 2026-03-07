from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_native.models import DiagramArtifact, ReviewReport, RunState
from ai_native.stages.common import ExecutionContext, StageError, write_diagram_artifacts, write_review
from ai_native.utils import read_json, read_text, write_json


def _validate_mermaid(context: ExecutionContext, diagram_path: Path) -> tuple[bool, str]:
    command = context.config.workspace.mermaid_validate_command
    if not command or shutil.which(command[0]) is None:
        return True, "Mermaid CLI not installed; validation skipped."
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "diagram.svg"
        full_command = [
            *command,
            *context.config.workspace.mermaid_validate_args,
            "-i",
            str(diagram_path),
            "-o",
            str(output_path),
        ]
        completed = subprocess.run(
            full_command,
            cwd=context.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Mermaid validation failed."
            if "Could not find Chrome" in message or "chrome-headless-shell" in message:
                return True, f"Mermaid CLI browser dependency missing; validation skipped. {message}"
            return False, message
        return True, "Mermaid validation passed."


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    stage_dir = context.state_store.stage_dir(state, "architecture")
    context_report = read_json(Path(state.run_dir) / "recon" / "context.json")
    plan = read_json(Path(state.run_dir) / "plan" / "plan.json")
    prompt = context.prompt_library.render(
        "architecture.md",
        spec_text=read_text(context.spec_path),
        context_report=context_report,
        plan=plan,
    )
    schema_path = context.repo_root / "ai_native" / "schemas" / "diagram-artifact.json"
    response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
    artifact = DiagramArtifact.model_validate(response.json_data)
    artifacts = write_diagram_artifacts(stage_dir, artifact)

    valid, validation_message = _validate_mermaid(context, stage_dir / "architecture.mmd")
    validation_path = stage_dir / "validation.json"
    write_json(validation_path, {"valid": valid, "message": validation_message})
    if not valid:
        raise StageError(validation_message)

    review_prompt = context.prompt_library.render(
        "architecture_review.md",
        spec_text=read_text(context.spec_path),
        plan=plan,
        architecture=artifact.model_dump(mode="json"),
    )
    review_schema = context.repo_root / "ai_native" / "schemas" / "review-report.json"
    review_response = context.critic.run(review_prompt, cwd=context.repo_root, schema_path=review_schema)
    review = ReviewReport.model_validate(review_response.json_data)
    review_md = stage_dir / "architecture-review.md"
    write_review(review_md, review)
    if context.config.quality_gates.require_diagram_approval and review.verdict != "approved":
        raise StageError(f"Architecture critique failed: {review.summary}")
    return [*artifacts, validation_path, review_md, review_md.with_suffix(".json")]

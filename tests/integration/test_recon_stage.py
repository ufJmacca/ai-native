from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_native.adapters.base import AgentResult
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.stages.recon import run as run_recon
from ai_native.state import StateStore
from tests.helpers import FakeWorkflowAdapter


class ReferenceReconBuilder:
    def __init__(self, *, supports_images: bool) -> None:
        self.prompts: list[str] = []
        self._supports_images = supports_images

    def supports_image_inputs(self) -> bool:
        return self._supports_images

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        if schema_path and schema_path.name == "context-report.json":
            payload = {
                "repo_state": "existing",
                "languages": ["javascript"],
                "manifests": ["package.json"],
                "test_frameworks": ["pytest"],
                "architecture_summary": "Existing frontend app.",
                "risks": [],
                "touched_areas": ["src"],
                "recommended_questions": [],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        if schema_path and schema_path.name == "reference-context.json":
            payload = {
                "workflow_profile": "reference_driven_web",
                "summary": "Faithful landing page recreation.",
                "design_intent": "Keep the stitched design structure and type scale.",
                "stable_patterns": ["Hero and card grid"],
                "typography": ["Clash Display", "Inter body"],
                "colors": ["#112233", "#ffeecc"],
                "spacing": ["16px", "32px"],
                "layout_patterns": ["Two-column hero", "Three-card grid"],
                "repeated_components": ["CTA buttons", "Feature cards"],
                "responsive_behaviors": ["Collapse to one column on mobile"],
                "fidelity_constraints": ["Keep section order", "Preserve hero hierarchy"],
            }
            return AgentResult(text=json.dumps(payload), json_data=payload)
        raise AssertionError(f"Unexpected schema requested: {schema_path}")


def _write_reference_spec(tmp_path: Path, reference_body: str, *, kind: str, reference_filename: str) -> Path:
    reference_path = tmp_path / reference_filename
    reference_path.write_text(reference_body, encoding="utf-8")
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        f"""
---
ainative:
  workflow_profile: reference_driven_web
  references:
    - id: landing
      label: Landing reference
      kind: {kind}
      path: {reference_filename}
      route: /
      viewport:
        width: 1440
        height: 1024
        label: desktop
  preview:
    url: http://127.0.0.1:3000
---
# Visual Spec

Recreate the landing page faithfully.
""".lstrip(),
        encoding="utf-8",
    )
    return spec_path


def test_recon_stage_writes_reference_context_for_html_export(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "package.json").write_text('{"name":"web-app"}\n', encoding="utf-8")
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
    (tmp_path / "landing.css").write_text(
        ".hero { color: #112233; padding: 32px; font-family: 'Clash Display'; }\n",
        encoding="utf-8",
    )
    spec_path = _write_reference_spec(
        tmp_path,
        '<html><head><link rel="stylesheet" href="landing.css"></head><body><section><h1>Launch Faster</h1></section></body></html>\n',
        kind="html_export",
        reference_filename="landing.html",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, workspace_root)
    run_dir = Path(state.run_dir)
    builder = ReferenceReconBuilder(supports_images=False)
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=workspace_root,
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
    )

    artifacts = run_recon(context, state)

    assert (run_dir / "recon" / "reference-context.json").exists()
    assert (run_dir / "recon" / "reference-context.md").exists()
    scan_text = (run_dir / "recon" / "reference-scan.json").read_text(encoding="utf-8")
    assert "#112233" in scan_text
    assert "Clash Display" in scan_text
    assert any("Launch Faster" in prompt for prompt in builder.prompts)
    assert any(path.name == "reference-context.md" for path in artifacts)


def test_recon_stage_rejects_image_only_references_without_image_capability(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "package.json").write_text('{"name":"web-app"}\n', encoding="utf-8")
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
    spec_path = _write_reference_spec(tmp_path, "fake-image\n", kind="image", reference_filename="landing.png")
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, workspace_root)
    run_dir = Path(state.run_dir)
    builder = ReferenceReconBuilder(supports_images=False)
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=workspace_root,
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
    )

    with pytest.raises(StageError, match="supports image inputs"):
        run_recon(context, state)


def test_recon_stage_reports_missing_html_export_reference_file(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "package.json").write_text('{"name":"web-app"}\n', encoding="utf-8")
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
    spec_path = _write_reference_spec(tmp_path, "<html></html>\n", kind="html_export", reference_filename="missing.html")
    (tmp_path / "missing.html").unlink()
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, workspace_root)
    run_dir = Path(state.run_dir)
    builder = ReferenceReconBuilder(supports_images=False)
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=workspace_root,
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=FakeWorkflowAdapter(),
        pr_reviewer=FakeWorkflowAdapter(),
    )

    with pytest.raises(StageError, match="Missing html_export reference file"):
        run_recon(context, state)

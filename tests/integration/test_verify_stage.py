from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

import pytest

from ai_native.adapters.base import AgentResult
from ai_native.prompting import PromptLibrary
from ai_native.stages.common import ExecutionContext, StageError
from ai_native.stages.verify import ImplementationCapture, run as run_verify
from ai_native.state import StateStore
from ai_native.utils import write_json
from tests.helpers import FakeWorkflowAdapter

SLICE_DIR_RE = re.compile(r"Slice artifact directory:\n(?P<path>.+)")


def _extract_slice_dir(prompt: str) -> Path:
    match = SLICE_DIR_RE.search(prompt)
    if not match:
        raise AssertionError("Slice artifact directory not found in prompt.")
    return Path(match.group("path").strip())


class VerificationRevisionBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        slice_dir = _extract_slice_dir(prompt)
        slice_dir.mkdir(parents=True, exist_ok=True)
        (slice_dir / "green.log").write_text(f"updated green output {self.calls}\n", encoding="utf-8")
        (slice_dir / "refactor-notes.md").write_text(f"# Refactor Notes\n- verify attempt {self.calls}\n", encoding="utf-8")
        return AgentResult(text=f"# Verification Revision Summary\nAttempt {self.calls}\n")


class OneFailingVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            payload = {
                "verdict": "failed",
                "summary": "Acceptance checks are incomplete.",
                "acceptance_checks": ["Can create a task"],
                "evidence": ["red.log", "green.log"],
                "gaps": ["Add explicit verification for list behavior"],
            }
        else:
            payload = {
                "verdict": "passed",
                "summary": "Acceptance checks are satisfied.",
                "acceptance_checks": ["Can create a task", "Can list tasks"],
                "evidence": ["red.log", "green.log", "refactor-notes.md"],
                "gaps": [],
            }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class PassingVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        payload = {
            "verdict": "passed",
            "summary": "Acceptance checks are satisfied.",
            "acceptance_checks": ["Can create a task", "Can list tasks"],
            "evidence": ["red.log", "green.log", "refactor-notes.md"],
            "gaps": [],
        }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ExhaustionVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def supports_image_inputs(self) -> bool:
        return False

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        if self.calls < 3:
            payload = {
                "verdict": "failed",
                "summary": "Still missing list verification.",
                "acceptance_checks": ["Can create a task"],
                "evidence": ["red.log", "green.log"],
                "gaps": ["Add explicit verification for list behavior"],
            }
        else:
            payload = {
                "verdict": "passed",
                "summary": "Acceptance checks are satisfied.",
                "acceptance_checks": ["Can create a task", "Can list tasks"],
                "evidence": ["red.log", "green.log", "refactor-notes.md"],
                "gaps": [],
            }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class OneRejectingVisualCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.image_paths: list[list[Path]] = []

    def supports_image_inputs(self) -> bool:
        return True

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.image_paths.append(list(image_paths or []))
        if self.calls == 1:
            payload = {
                "verdict": "changes_required",
                "summary": "Hero spacing and typography still drift from the reference.",
                "findings": ["Hero heading is too small"],
                "required_changes": ["Increase hero type scale", "Restore vertical spacing rhythm"],
            }
        else:
            payload = {
                "verdict": "approved",
                "summary": "Visual fidelity is materially aligned with the reference.",
                "findings": [],
                "required_changes": [],
            }
        return AgentResult(text=json.dumps(payload), json_data=payload)


class ImageAwarePassingVerifier(PassingVerifier):
    def __init__(self) -> None:
        super().__init__()
        self.image_paths: list[list[Path]] = []

    def supports_image_inputs(self) -> bool:
        return True

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.image_paths.append(list(image_paths or []))
        return super().run(prompt, cwd=cwd, schema_path=schema_path)


class ApprovingVisualCritic:
    def __init__(self) -> None:
        self.calls = 0
        self.image_paths: list[list[Path]] = []

    def supports_image_inputs(self) -> bool:
        return True

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.image_paths.append(list(image_paths or []))
        payload = {
            "verdict": "approved",
            "summary": "Visual fidelity is materially aligned with the reference.",
            "findings": [],
            "required_changes": [],
        }
        return AgentResult(text=json.dumps(payload), json_data=payload)


def _seed_slice_plan_and_artifacts(run_dir: Path) -> None:
    (run_dir / "slice").mkdir(parents=True, exist_ok=True)
    write_json(
        run_dir / "slice" / "slices.json",
        {
            "title": "Slices",
            "summary": "One slice",
            "slices": [
                {
                    "id": "S001",
                    "name": "Create and list tasks",
                    "goal": "Implement the first slice.",
                    "acceptance_criteria": ["Can create a task", "Can list tasks"],
                    "file_impact": ["app/tasks.py", "tests/test_tasks.py"],
                    "test_plan": ["Write failing tests first"],
                    "dependencies": [],
                }
            ],
        },
    )
    slice_dir = run_dir / "slices" / "S001"
    slice_dir.mkdir(parents=True, exist_ok=True)
    (slice_dir / "red.log").write_text("red output\n", encoding="utf-8")
    (slice_dir / "green.log").write_text("green output\n", encoding="utf-8")
    (slice_dir / "refactor-notes.md").write_text("# Refactor Notes\n- initial\n", encoding="utf-8")


def _seed_reference_context(run_dir: Path) -> None:
    recon_dir = run_dir / "recon"
    recon_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        recon_dir / "reference-context.json",
        {
            "workflow_profile": "reference_driven_web",
            "summary": "Faithful landing page recreation.",
            "design_intent": "Preserve the bold hero, card rhythm, and section order.",
            "stable_patterns": ["Hero then card grid"],
            "typography": ["Large display headline"],
            "colors": ["#112233"],
            "spacing": ["32px"],
            "layout_patterns": ["Two-column hero"],
            "repeated_components": ["CTA buttons"],
            "responsive_behaviors": ["Single-column mobile stack"],
            "fidelity_constraints": ["Keep section order", "Preserve hero hierarchy"],
        },
    )


def _single_matching_file(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one file for pattern {pattern!r} in {directory}, found {matches!r}")
    return matches[0]


def test_verify_stage_revises_after_failed_verification(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    builder = VerificationRevisionBuilder()
    verifier = OneFailingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    artifacts = run_verify(context, state)
    verify_dir = run_dir / "verify"

    assert verifier.calls == 2
    assert builder.calls == 1
    assert any("verification requested changes" in event for event in progress)
    assert any("Add explicit verification for list behavior" in prompt for prompt in builder.prompts)
    assert any("Blocker ledger:" in prompt for prompt in verifier.prompts)
    assert (verify_dir / "S001-attempt-2.json").exists()
    assert (verify_dir / "S001-revision-summary-attempt-2.md").exists()
    assert (verify_dir / "S001-critique-history.md").exists()
    assert (verify_dir / "S001-blocker-ledger.md").exists()
    assert any(path.name == "S001-attempt-2.md" for path in artifacts)


def test_verify_stage_resumes_from_latest_attempt(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 4
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001-attempt-2.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001-attempt-2.md").write_text("# Prior Verification\n", encoding="utf-8")
    builder = VerificationRevisionBuilder()
    verifier = PassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_verify(context, state)

    assert builder.calls == 1
    assert verifier.calls == 1
    assert any("resuming from previous critique at attempt 3" in event for event in progress)
    assert any("Add explicit verification for list behavior" in prompt for prompt in builder.prompts)
    assert (verify_dir / "S001-attempt-3.json").exists()


def test_verify_stage_resumes_pending_verification_attempt_without_revision(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_config.workspace.verification_max_attempts = 2
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"reference-image")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001-attempt-1.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001-attempt-1.md").write_text("# Prior Verification\n", encoding="utf-8")
    write_json(
        verify_dir / "S001-visual-review-attempt-2.json",
        {
            "verdict": "approved",
            "summary": "Visual fidelity is materially aligned with the reference.",
            "findings": [],
            "required_changes": [],
        },
    )
    (verify_dir / "S001-visual-review-attempt-2.md").write_text("# Prior Visual Review\n", encoding="utf-8")

    visual_attempt_dir = verify_dir / "visual" / "S001" / "attempt-2"
    visual_attempt_dir.mkdir(parents=True, exist_ok=True)
    implementation_capture = visual_attempt_dir / "hero-desktop-implementation.png"
    implementation_capture.write_bytes(b"implementation-image")
    reference_capture = visual_attempt_dir / "hero-reference.png"
    reference_capture.write_bytes(b"reference-image")

    builder = VerificationRevisionBuilder()
    critic = ApprovingVisualCritic()
    verifier = ImageAwarePassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_verify(context, state)

    assert builder.calls == 0
    assert critic.calls == 0
    assert verifier.calls == 1
    assert verifier.image_paths == [[implementation_capture, reference_capture]]
    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert any("resuming verification attempt 2/2 after completed visual review" in event for event in progress)
    assert (verify_dir / "S001-attempt-2.json").exists()


def test_verify_stage_resume_reuses_extensionless_reference_images(
    app_config, tmp_path: Path
) -> None:
    app_config.workspace.verification_max_attempts = 2
    reference_image = tmp_path / "reference"
    reference_image.write_bytes(b"reference-image")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001-attempt-1.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001-attempt-1.md").write_text("# Prior Verification\n", encoding="utf-8")
    write_json(
        verify_dir / "S001-visual-review-attempt-2.json",
        {
            "verdict": "approved",
            "summary": "Visual fidelity is materially aligned with the reference.",
            "findings": [],
            "required_changes": [],
        },
    )
    (verify_dir / "S001-visual-review-attempt-2.md").write_text("# Prior Visual Review\n", encoding="utf-8")
    visual_attempt_dir = verify_dir / "visual" / "S001" / "attempt-2"
    visual_attempt_dir.mkdir(parents=True, exist_ok=True)
    implementation_capture = visual_attempt_dir / "hero-desktop-implementation.png"
    implementation_capture.write_bytes(b"implementation-image")
    reference_capture = visual_attempt_dir / "hero-reference"
    reference_capture.write_bytes(b"reference-image")

    builder = VerificationRevisionBuilder()
    critic = ApprovingVisualCritic()
    verifier = ImageAwarePassingVerifier()
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    run_verify(context, state)

    assert builder.calls == 0
    assert critic.calls == 0
    assert verifier.calls == 1
    assert verifier.image_paths == [[implementation_capture, reference_capture]]


def test_verify_stage_can_continue_after_attempt_budget_is_exhausted(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 1
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    builder = VerificationRevisionBuilder()
    verifier = ExhaustionVerifier()
    progress: list[str] = []
    asked_questions: list[list[str]] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
        ask_questions=lambda stage, questions: asked_questions.append(questions) or ["yes", "2"],
    )

    run_verify(context, state)

    assert verifier.calls == 3
    assert builder.calls == 2
    assert any("attempt budget exhausted" in event for event in progress)
    assert any("continuing with 2 additional attempts" in event for event in progress)
    assert len(asked_questions) == 1
    assert "Continue with more verification attempts?" in asked_questions[0][0]


def test_verify_stage_resumes_legacy_failed_run(app_config, tmp_spec: Path, tmp_path: Path) -> None:
    app_config.workspace.verification_max_attempts = 3
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(tmp_spec, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        verify_dir / "S001.json",
        {
            "verdict": "failed",
            "summary": "Need explicit list verification.",
            "acceptance_checks": ["Can create a task"],
            "evidence": ["red.log", "green.log"],
            "gaps": ["Add explicit verification for list behavior"],
        },
    )
    (verify_dir / "S001.md").write_text("# Legacy Verification\n", encoding="utf-8")
    builder = VerificationRevisionBuilder()
    verifier = PassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=tmp_spec,
        run_dir=run_dir,
        builder=builder,
        critic=FakeWorkflowAdapter(),
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    run_verify(context, state)

    assert any("resuming from previous critique at attempt 2" in event for event in progress)
    assert (verify_dir / "S001-attempt-1.json").exists()
    assert (verify_dir / "S001-attempt-2.json").exists()


def test_verify_stage_runs_visual_review_before_final_verification(app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_config.workspace.verification_max_attempts = 3
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"reference-image",)
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    capture_path = run_dir / "verify" / "captured-hero.png"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_bytes(b"implementation-image")
    capture = ImplementationCapture(
        route="/",
        viewport_label="desktop",
        viewport_width=1440,
        viewport_height=1200,
        path=capture_path,
    )

    monkeypatch.setattr("ai_native.stages.verify.preview_session", lambda preview, cwd: contextlib.nullcontext())
    monkeypatch.setattr("ai_native.stages.verify.capture_implementation_screenshots", lambda preview, references, output_dir: [capture])

    builder = VerificationRevisionBuilder()
    critic = OneRejectingVisualCritic()
    verifier = ImageAwarePassingVerifier()
    progress: list[str] = []
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=builder,
        critic=critic,
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=progress.append,
    )

    artifacts = run_verify(context, state)
    verify_dir = run_dir / "verify"

    assert critic.calls == 2
    assert builder.calls == 1
    assert verifier.calls == 1
    assert any("visual critique requested changes" in event for event in progress)
    assert (verify_dir / "S001-visual-review.json").exists()
    assert (verify_dir / "S001-visual-review-attempt-1.json").exists()
    assert (verify_dir / "S001-visual-review-attempt-2.json").exists()
    assert (verify_dir / "S001-attempt-2.json").exists()
    assert any(path.name == "S001-visual-review-attempt-2.md" for path in artifacts)
    attempt_1_reference = _single_matching_file(verify_dir / "visual" / "S001" / "attempt-1", "hero-*-reference.png")
    attempt_2_reference = _single_matching_file(verify_dir / "visual" / "S001" / "attempt-2", "hero-*-reference.png")
    assert critic.image_paths[0] == [
        capture_path,
        attempt_1_reference,
    ]
    assert critic.image_paths[1] == [
        capture_path,
        attempt_2_reference,
    ]
    assert verifier.image_paths == [
        [
            capture_path,
            attempt_2_reference,
        ]
    ]


def test_verify_stage_fails_when_visual_review_captures_are_missing(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_export = tmp_path / "reference.html"
    html_export.write_text("<html><body><main><h1>Reference</h1></main></body></html>\n", encoding="utf-8")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: homepage",
                "      label: Homepage export",
                "      kind: html_export",
                f"      path: {html_export.name}",
                "      route: /",
                "      viewport:",
                "        width: 1280",
                "        height: 960",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    monkeypatch.setattr("ai_native.stages.verify.preview_session", lambda preview, cwd: contextlib.nullcontext())
    monkeypatch.setattr("ai_native.stages.verify.capture_implementation_screenshots", lambda preview, references, output_dir: [])

    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=VerificationRevisionBuilder(),
        critic=OneRejectingVisualCritic(),
        verifier=PassingVerifier(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    with pytest.raises(StageError, match="did not produce implementation screenshots"):
        run_verify(context, state)


def test_verify_stage_slugifies_reference_image_artifact_names(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"reference-image")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero/mobile",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    capture_path = run_dir / "verify" / "captured-hero.png"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_bytes(b"implementation-image")
    capture = ImplementationCapture(
        route="/",
        viewport_label="desktop",
        viewport_width=1440,
        viewport_height=1200,
        path=capture_path,
    )

    monkeypatch.setattr("ai_native.stages.verify.preview_session", lambda preview, cwd: contextlib.nullcontext())
    monkeypatch.setattr("ai_native.stages.verify.capture_implementation_screenshots", lambda preview, references, output_dir: [capture])

    critic = OneRejectingVisualCritic()
    verifier = ImageAwarePassingVerifier()
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=VerificationRevisionBuilder(),
        critic=critic,
        verifier=verifier,
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    run_verify(context, state)

    expected_reference = _single_matching_file(
        run_dir / "verify" / "visual" / "S001" / "attempt-1", "hero-mobile-*-reference.png"
    )
    assert expected_reference.exists()
    assert critic.image_paths[0] == [capture_path, expected_reference]


def test_verify_stage_disambiguates_copied_reference_image_filenames(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_image_a = tmp_path / "reference-a.png"
    reference_image_b = tmp_path / "reference-b.png"
    reference_image_a.write_bytes(b"reference-image-a")
    reference_image_b.write_bytes(b"reference-image-b")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero/mobile",
                "      label: Hero mobile",
                "      kind: image",
                f"      path: {reference_image_a.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "    - id: hero-mobile",
                "      label: Hero mobile alt",
                "      kind: image",
                f"      path: {reference_image_b.name}",
                "      route: /alternate",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    capture_path = run_dir / "verify" / "captured-hero.png"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_bytes(b"implementation-image")
    capture = ImplementationCapture(
        route="/",
        viewport_label="desktop",
        viewport_width=1440,
        viewport_height=1200,
        path=capture_path,
    )

    monkeypatch.setattr("ai_native.stages.verify.preview_session", lambda preview, cwd: contextlib.nullcontext())
    monkeypatch.setattr(
        "ai_native.stages.verify.capture_implementation_screenshots", lambda preview, references, output_dir: [capture]
    )

    critic = ApprovingVisualCritic()
    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=VerificationRevisionBuilder(),
        critic=critic,
        verifier=PassingVerifier(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    run_verify(context, state)

    reference_dir = run_dir / "verify" / "visual" / "S001" / "attempt-1"
    reference_files = sorted(reference_dir.glob("hero-mobile-*-reference.png"))
    assert len(reference_files) == 2
    assert reference_files[0] != reference_files[1]
    assert critic.image_paths[0][0] == capture_path
    assert sorted(critic.image_paths[0][1:]) == reference_files


def test_verify_stage_rejects_image_only_references_without_image_capable_critic(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"reference-image")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    monkeypatch.setattr(
        "ai_native.stages.verify.preview_session",
        lambda preview, cwd: (_ for _ in ()).throw(AssertionError("preview session should not start")),
    )
    monkeypatch.setattr(
        "ai_native.stages.verify.capture_implementation_screenshots",
        lambda preview, references, output_dir: (_ for _ in ()).throw(AssertionError("captures should not run")),
    )

    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=VerificationRevisionBuilder(),
        critic=FakeWorkflowAdapter(),
        verifier=PassingVerifier(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    with pytest.raises(StageError, match="critic that supports image inputs"):
        run_verify(context, state)


def test_verify_stage_reports_missing_image_reference_file(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"reference-image")
    spec_path = tmp_path / "reference-spec.md"
    spec_path.write_text(
        "\n".join(
            [
                "---",
                "ainative:",
                "  workflow_profile: reference_driven_web",
                "  references:",
                "    - id: hero",
                "      label: Hero reference",
                "      kind: image",
                f"      path: {reference_image.name}",
                "      route: /",
                "      viewport:",
                "        width: 1440",
                "        height: 1200",
                "        label: desktop",
                "  preview:",
                "    url: http://localhost:4173",
                "---",
                "# Reference Landing Page",
                "",
                "Recreate the supplied landing page faithfully.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_store = StateStore(tmp_path / "artifacts")
    state = state_store.create_run(spec_path, Path(__file__).resolve().parents[2])
    run_dir = Path(state.run_dir)
    _seed_slice_plan_and_artifacts(run_dir)
    _seed_reference_context(run_dir)

    capture_path = run_dir / "verify" / "captured-hero.png"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_bytes(b"implementation-image")
    capture = ImplementationCapture(
        route="/",
        viewport_label="desktop",
        viewport_width=1440,
        viewport_height=1200,
        path=capture_path,
    )

    reference_image.unlink()
    monkeypatch.setattr("ai_native.stages.verify.preview_session", lambda preview, cwd: contextlib.nullcontext())
    monkeypatch.setattr(
        "ai_native.stages.verify.capture_implementation_screenshots", lambda preview, references, output_dir: [capture]
    )

    context = ExecutionContext(
        config=app_config,
        prompt_library=PromptLibrary(Path(__file__).resolve().parents[2] / "ai_native" / "prompts"),
        state_store=state_store,
        template_root=Path(__file__).resolve().parents[2] / "ai_native",
        repo_root=Path(__file__).resolve().parents[2],
        spec_path=spec_path,
        run_dir=run_dir,
        builder=VerificationRevisionBuilder(),
        critic=OneRejectingVisualCritic(),
        verifier=PassingVerifier(),
        pr_reviewer=FakeWorkflowAdapter(),
        emit_progress=lambda _message: None,
    )

    with pytest.raises(StageError, match="Missing image reference file"):
        run_verify(context, state)

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ai_native.models import ContextReport, RunState
from ai_native.stages.common import ExecutionContext, dump_model, render_context_markdown
from ai_native.utils import read_text, write_json, write_text

IGNORE_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "artifacts",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    "dist",
    "build",
}
IGNORE_FILES: set[str] = set()
MANIFEST_NAMES = {
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "Dockerfile",
    "compose.yaml",
}


def _is_ignored(relative: Path) -> bool:
    return any(part in IGNORE_DIRS for part in relative.parts) or (len(relative.parts) == 1 and relative.name in IGNORE_FILES)


def _is_test_file(relative: Path) -> bool:
    return "tests" in relative.parts or relative.name.startswith("test_")


def _scan_repository(repo_root: Path) -> dict[str, object]:
    manifests: list[str] = []
    language_counter: Counter[str] = Counter()
    tests_present: set[str] = set()
    touched_areas: Counter[str] = Counter()
    source_files = 0

    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root)
        if _is_ignored(relative):
            continue
        if path.name in MANIFEST_NAMES:
            manifests.append(str(relative))
        if _is_test_file(relative):
            tests_present.add("pytest")
            continue
        suffix = path.suffix.lower()
        if suffix == ".py":
            language_counter["python"] += 1
            source_files += 1
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            language_counter["javascript"] += 1
            source_files += 1
        elif suffix in {".go"}:
            language_counter["go"] += 1
            source_files += 1
        elif suffix in {".rs"}:
            language_counter["rust"] += 1
            source_files += 1
        if "test" in path.parts or path.name.startswith("test_"):
            tests_present.add("pytest")
        if relative.parts:
            touched_areas[relative.parts[0]] += 1

    repo_state = "greenfield" if source_files == 0 else "existing"
    return {
        "repo_state": repo_state,
        "languages": [name for name, _ in language_counter.most_common()],
        "manifests": sorted(manifests),
        "test_frameworks": sorted(tests_present),
        "source_file_count": source_files,
        "top_level_areas": [name for name, _ in touched_areas.most_common(10)],
        "ignored_paths": sorted(IGNORE_DIRS | IGNORE_FILES),
        "analysis_focus": "Infer architecture from the target repository as it exists. Consider app code, infrastructure, CI, docs, and configuration when they materially affect the feature. Ignore generated/runtime noise, and do not let tests alone define product architecture.",
    }


def run(context: ExecutionContext, state: RunState) -> list[Path]:
    recon_dir = context.state_store.stage_dir(state, "recon")
    context.emit_progress("[ainative] recon: scanning repository")
    scan = _scan_repository(context.repo_root)
    scan_path = recon_dir / "scan.json"
    write_json(scan_path, scan)

    if scan["repo_state"] == "greenfield":
        report = ContextReport(
            repo_state="greenfield",
            languages=[],
            manifests=[],
            test_frameworks=[],
            architecture_summary="The repository does not contain product source code yet. Treat this as a greenfield implementation seeded from the template.",
            risks=[
                "No product code exists yet, so interfaces and folder structure must be established from scratch.",
                "The first feature implementation must define the initial testing and deployment conventions.",
            ],
            touched_areas=["Initial application structure", "Testing harness", "Developer documentation"],
            recommended_questions=[],
        )
    else:
        context.emit_progress("[ainative] recon: generating context report")
        prompt = context.prompt_library.render(
            "recon.md",
            spec_text=read_text(context.spec_path),
            scan_summary=scan,
        )
        schema_path = context.template_root / "ai_native" / "schemas" / "context-report.json"
        response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path)
        report = ContextReport.model_validate(response.json_data)

    json_path = recon_dir / "context.json"
    md_path = recon_dir / "context.md"
    dump_model(json_path, report)
    write_text(md_path, render_context_markdown(report))
    return [scan_path, json_path, md_path]

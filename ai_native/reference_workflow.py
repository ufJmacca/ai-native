from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ai_native.adapters.base import AgentAdapter
from ai_native.models import ReferenceContext, ReferenceManifest, ReferenceInput, ReviewReport
from ai_native.specs import load_reference_manifest
from ai_native.stages.common import ExecutionContext, StageError, dump_model
from ai_native.utils import read_json, read_text, render_bullets, write_json, write_text

_CSS_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]+\)|hsla?\([^)]+\)")
_CSS_FONT_RE = re.compile(r"font-family\s*:\s*([^;]+);", re.IGNORECASE)
_CSS_SPACING_RE = re.compile(r"\b(?:margin|padding|gap|row-gap|column-gap)\b[^:]*:\s*([^;]+);", re.IGNORECASE)
_NUMERIC_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?(?:px|rem|em|%)")


class _HTMLReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: str = ""
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_buffer: list[str] = []
        self.headings: list[str] = []
        self.section_count = 0
        self.class_counter: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in {"section", "header", "footer", "main", "nav"}:
            self.section_count += 1
        if tag in {"h1", "h2", "h3"}:
            self._heading_tag = tag
            self._heading_buffer = []
        attr_map = dict(attrs)
        class_value = attr_map.get("class")
        if class_value:
            for name in class_value.split():
                self.class_counter[name] += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._heading_tag == tag:
            heading = "".join(self._heading_buffer).strip()
            if heading:
                self.headings.append(heading)
            self._heading_tag = None
            self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        if self._heading_tag is not None:
            self._heading_buffer.append(data)


def adapter_supports_image_inputs(adapter: AgentAdapter) -> bool:
    supports = getattr(adapter, "supports_image_inputs", None)
    if callable(supports):
        return bool(supports())
    return False


def reference_context_path(run_dir: Path) -> Path:
    return run_dir / "recon" / "reference-context.json"


def reference_context_markdown_path(run_dir: Path) -> Path:
    return run_dir / "recon" / "reference-context.md"


def reference_scan_path(run_dir: Path) -> Path:
    return run_dir / "recon" / "reference-scan.json"


def load_reference_context(run_dir: Path) -> ReferenceContext | None:
    path = reference_context_path(run_dir)
    if not path.exists():
        return None
    return ReferenceContext.model_validate(read_json(path))


def render_reference_context_markdown(context: ReferenceContext) -> str:
    return "\n".join(
        [
            "# Reference Context",
            "",
            "## Summary",
            context.summary,
            "",
            "## Design Intent",
            context.design_intent,
            "",
            "## Stable Patterns",
            render_bullets(context.stable_patterns),
            "",
            "## Typography",
            render_bullets(context.typography),
            "",
            "## Colors",
            render_bullets(context.colors),
            "",
            "## Spacing",
            render_bullets(context.spacing),
            "",
            "## Layout Patterns",
            render_bullets(context.layout_patterns),
            "",
            "## Repeated Components",
            render_bullets(context.repeated_components),
            "",
            "## Responsive Behaviors",
            render_bullets(context.responsive_behaviors),
            "",
            "## Fidelity Constraints",
            render_bullets(context.fidelity_constraints),
        ]
    )


def render_reference_prompt_block(run_dir: Path) -> str:
    manifest = load_reference_manifest(run_dir, run_dir / "spec.md")
    context = load_reference_context(run_dir)
    if manifest is None or context is None:
        return ""
    manifest_lines = [
        "Reference-driven web fidelity profile is active for this spec.",
        "Treat the supplied references as a concrete implementation target, not loose inspiration.",
        "Reuse the stable primitives across sections and preserve the established product language where the repository already has one.",
        "",
        "Reference manifest:",
        manifest.model_dump_json(indent=2),
        "",
        "Reference context:",
        context.model_dump_json(indent=2),
    ]
    return "\n".join(manifest_lines)


def append_reference_prompt_block(prompt: str, run_dir: Path) -> str:
    block = render_reference_prompt_block(run_dir)
    if not block:
        return prompt
    return "\n\n".join([prompt.rstrip(), block])


def load_reference_manifest_for_run(context: ExecutionContext) -> ReferenceManifest | None:
    return load_reference_manifest(Path(context.run_dir), context.spec_path)


def _css_tokens(css_text: str) -> dict[str, list[str]]:
    fonts = [match.strip().strip("\"'") for match in _CSS_FONT_RE.findall(css_text)]
    colors = _CSS_COLOR_RE.findall(css_text)
    spacing_tokens: list[str] = []
    for raw in _CSS_SPACING_RE.findall(css_text):
        spacing_tokens.extend(_NUMERIC_TOKEN_RE.findall(raw))
    return {
        "fonts": sorted(dict.fromkeys(fonts))[:20],
        "colors": sorted(dict.fromkeys(colors))[:30],
        "spacing_values": sorted(dict.fromkeys(spacing_tokens))[:30],
    }


def _linked_css_paths(html_text: str, html_path: Path) -> list[Path]:
    linked: list[Path] = []
    for tag in re.findall(r"<link\b[^>]*>", html_text, flags=re.IGNORECASE):
        rel_match = re.search(r'\brel\s*=\s*["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        href_match = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        if href_match is None:
            continue
        rel_values = {value.strip().lower() for value in (rel_match.group(1).split() if rel_match else [])}
        if "stylesheet" not in rel_values:
            continue
        href = href_match.group(1)
        if href.startswith(("http://", "https://", "//")):
            continue
        path = (html_path.parent / href).resolve()
        if path.exists():
            linked.append(path)
    return linked


def _scan_html_export(reference: ReferenceInput) -> dict[str, Any]:
    if reference.path is None:
        return {}
    html_path = Path(reference.path)
    html_text = read_text(html_path)
    parser = _HTMLReferenceParser()
    parser.feed(html_text)
    css_text = "\n".join(read_text(path) for path in _linked_css_paths(html_text, html_path))
    inline_style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if inline_style_blocks:
        css_text = "\n".join([css_text, *inline_style_blocks]).strip()
    tokens = _css_tokens(css_text)
    return {
        "title": parser.title.strip(),
        "headings": parser.headings[:10],
        "section_count": parser.section_count,
        "class_frequency": parser.class_counter.most_common(12),
        "fonts": tokens["fonts"],
        "colors": tokens["colors"],
        "spacing_values": tokens["spacing_values"],
        "linked_stylesheets": [str(path) for path in _linked_css_paths(html_text, html_path)],
    }


def build_reference_scan(manifest: ReferenceManifest) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    for item in manifest.references:
        entry: dict[str, Any] = {
            "id": item.id,
            "label": item.label,
            "kind": item.kind,
            "route": item.route,
            "viewport": item.viewport.model_dump(mode="json"),
            "notes": item.notes,
        }
        if item.path:
            path = Path(item.path)
            entry["path"] = str(path)
            if path.exists():
                entry["file"] = {"name": path.name, "suffix": path.suffix.lower(), "size_bytes": path.stat().st_size}
        if item.url:
            entry["url"] = item.url
        if item.kind == "html_export":
            entry["html_export"] = _scan_html_export(item)
        references.append(entry)
    return {
        "workflow_profile": manifest.workflow_profile,
        "preview": manifest.preview.model_dump(mode="json"),
        "reference_count": len(references),
        "references": references,
    }


def _supports_non_image_reference_inputs(manifest: ReferenceManifest) -> bool:
    return any(item.kind in {"html_export", "url"} for item in manifest.references)


def ensure_reference_workflow_supported(
    manifest: ReferenceManifest, adapter: AgentAdapter, *, role_name: str = "builder"
) -> None:
    if adapter_supports_image_inputs(adapter):
        return
    if _supports_non_image_reference_inputs(manifest):
        return
    raise StageError(
        "Reference-driven web workflow requires "
        f"a {role_name} that supports image inputs when all references are image-only. "
        f"Use Codex for the {role_name} role or add an html_export/url reference."
    )


def reference_image_paths(manifest: ReferenceManifest) -> list[Path]:
    return [Path(item.path) for item in manifest.references if item.kind == "image" and item.path]


def generate_reference_context(
    context: ExecutionContext,
    spec_text: str,
    manifest: ReferenceManifest,
    context_report: dict[str, Any],
) -> list[Path]:
    ensure_reference_workflow_supported(manifest, context.builder)
    scan = build_reference_scan(manifest)
    scan_artifact = reference_scan_path(Path(context.run_dir))
    write_json(scan_artifact, scan)

    prompt = context.prompt_library.render(
        "reference_context.md",
        spec_text=spec_text,
        context_report=context_report,
        reference_manifest=manifest.model_dump(mode="json"),
        reference_scan=scan,
    )
    image_paths = reference_image_paths(manifest) if adapter_supports_image_inputs(context.builder) else None
    schema_path = context.template_root / "schemas" / "reference-context.json"
    response = context.builder.run(prompt, cwd=context.repo_root, schema_path=schema_path, image_paths=image_paths)
    artifact = ReferenceContext.model_validate(response.json_data)

    json_path = reference_context_path(Path(context.run_dir))
    md_path = reference_context_markdown_path(Path(context.run_dir))
    dump_model(json_path, artifact)
    write_text(md_path, render_reference_context_markdown(artifact))
    return [scan_artifact, json_path, md_path]


def visual_review_prompt_block(review: ReviewReport | None) -> str:
    if review is None:
        return ""
    return "\n".join(
        [
            "Latest visual fidelity review:",
            review.model_dump_json(indent=2),
        ]
    )

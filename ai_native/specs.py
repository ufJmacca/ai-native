from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ai_native.models import ReferenceManifest
from ai_native.utils import read_json, read_text, write_json, write_text

_FRONTMATTER_DELIMITER = "---"
_AINATIVE_FRONTMATTER_RE = re.compile(r"(^|\r?\n)\s*ainative\s*:", re.MULTILINE)


class ParsedSpec(BaseModel):
    body: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    reference_manifest: ReferenceManifest | None = None


def _split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    lines = raw_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return {}, raw_text
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        return {}, raw_text
    frontmatter_text = "".join(lines[1:closing_index])
    if _AINATIVE_FRONTMATTER_RE.search(frontmatter_text) is None:
        return {}, raw_text
    loaded = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(loaded, dict):
        return {}, raw_text
    if "ainative" not in loaded:
        return {}, raw_text
    body = "".join(lines[closing_index + 1 :])
    return loaded, body


def _resolve_reference_path(spec_path: Path, raw_path: str | None) -> str | None:
    if raw_path is None:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (spec_path.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return str(candidate)


def _normalize_reference_manifest(spec_path: Path, payload: Any) -> ReferenceManifest | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("`ainative` frontmatter must be a mapping")
    workflow_profile = payload.get("workflow_profile")
    if workflow_profile is None:
        return None
    if workflow_profile != "reference_driven_web":
        raise ValueError(f"unsupported ainative workflow_profile `{workflow_profile}`")

    references = payload.get("references")
    if not isinstance(references, list):
        raise ValueError("reference-driven web workflow requires `ainative.references`")
    normalized_references: list[dict[str, Any]] = []
    for item in references:
        if not isinstance(item, dict):
            raise ValueError("each ainative reference must be a mapping")
        normalized = dict(item)
        normalized["path"] = _resolve_reference_path(spec_path, normalized.get("path"))
        normalized_references.append(normalized)

    preview = payload.get("preview")
    if not isinstance(preview, dict):
        raise ValueError("reference-driven web workflow requires `ainative.preview`")

    manifest_payload = {
        "workflow_profile": workflow_profile,
        "references": normalized_references,
        "preview": preview,
    }
    return ReferenceManifest.model_validate(manifest_payload)


def parse_spec(spec_path: Path) -> ParsedSpec:
    raw_text = read_text(spec_path)
    frontmatter, body = _split_frontmatter(raw_text)
    manifest = _normalize_reference_manifest(spec_path, frontmatter.get("ainative"))
    body_text = body if body.endswith("\n") else f"{body}\n"
    return ParsedSpec(body=body_text, frontmatter=frontmatter, reference_manifest=manifest)


def prompt_spec_path(run_dir: Path) -> Path:
    return run_dir / "spec.md"


def reference_manifest_path(run_dir: Path) -> Path:
    return run_dir / "reference-manifest.json"


def write_parsed_spec_artifacts(run_dir: Path, parsed_spec: ParsedSpec) -> list[Path]:
    artifacts: list[Path] = []
    spec_body_path = prompt_spec_path(run_dir)
    write_text(spec_body_path, parsed_spec.body)
    artifacts.append(spec_body_path)
    if parsed_spec.reference_manifest is not None:
        manifest_path = reference_manifest_path(run_dir)
        write_json(manifest_path, parsed_spec.reference_manifest.model_dump(mode="json"))
        artifacts.append(manifest_path)
    return artifacts


def load_prompt_spec_text(run_dir: Path, spec_path: Path) -> str:
    stored = prompt_spec_path(run_dir)
    if stored.exists():
        return read_text(stored)
    return parse_spec(spec_path).body


def load_reference_manifest(run_dir: Path, _spec_path: Path) -> ReferenceManifest | None:
    stored = reference_manifest_path(run_dir)
    if stored.exists():
        return ReferenceManifest.model_validate(read_json(stored))
    return None

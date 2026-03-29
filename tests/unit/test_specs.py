from __future__ import annotations

from pathlib import Path

from ai_native.specs import parse_spec


def test_parse_spec_preserves_plain_markdown_body(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Plain Spec\n\nShip the thing.\n", encoding="utf-8")

    parsed = parse_spec(spec_path)

    assert parsed.body == "# Plain Spec\n\nShip the thing.\n"
    assert parsed.frontmatter == {}
    assert parsed.reference_manifest is None


def test_parse_spec_normalizes_reference_workflow_frontmatter(tmp_path: Path) -> None:
    export_path = tmp_path / "stitch.html"
    export_path.write_text("<html></html>\n", encoding="utf-8")
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        """
---
ainative:
  workflow_profile: reference_driven_web
  references:
    - id: landing
      label: Landing export
      kind: html_export
      path: stitch.html
      route: /
      viewport:
        width: 1440
        height: 1024
        label: desktop
  preview:
    url: http://127.0.0.1:3000
---
# Visual Spec

Implement the page faithfully.
""".lstrip(),
        encoding="utf-8",
    )

    parsed = parse_spec(spec_path)

    assert parsed.body == "# Visual Spec\n\nImplement the page faithfully.\n"
    assert parsed.reference_manifest is not None
    assert parsed.reference_manifest.workflow_profile == "reference_driven_web"
    assert parsed.reference_manifest.references[0].path == str(export_path.resolve())
    assert parsed.reference_manifest.preview.url == "http://127.0.0.1:3000"


def test_parse_spec_accepts_crlf_frontmatter(tmp_path: Path) -> None:
    export_path = tmp_path / "stitch.html"
    export_path.write_text("<html></html>\n", encoding="utf-8")
    spec_path = tmp_path / "spec-crlf.md"
    spec_path.write_bytes(
        (
            "---\r\n"
            "ainative:\r\n"
            "  workflow_profile: reference_driven_web\r\n"
            "  references:\r\n"
            "    - id: landing\r\n"
            "      label: Landing export\r\n"
            "      kind: html_export\r\n"
            "      path: stitch.html\r\n"
            "      route: /\r\n"
            "      viewport:\r\n"
            "        width: 1440\r\n"
            "        height: 1024\r\n"
            "        label: desktop\r\n"
            "  preview:\r\n"
            "    url: http://127.0.0.1:3000\r\n"
            "---\r\n"
            "# Visual Spec\r\n"
            "\r\n"
            "Implement the page faithfully.\r\n"
        ).encode("utf-8")
    )

    parsed = parse_spec(spec_path)

    assert parsed.reference_manifest is not None
    assert parsed.reference_manifest.workflow_profile == "reference_driven_web"
    assert parsed.reference_manifest.references[0].path == str(export_path.resolve())
    assert parsed.reference_manifest.preview.url == "http://127.0.0.1:3000"


def test_parse_spec_preserves_leading_delimiters_without_ainative_frontmatter(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.md"
    spec_text = "---\nWelcome\n---\n# Plain Spec\n\nShip the thing.\n"
    spec_path.write_text(spec_text, encoding="utf-8")

    parsed = parse_spec(spec_path)

    assert parsed.body == spec_text
    assert parsed.frontmatter == {}
    assert parsed.reference_manifest is None


def test_parse_spec_preserves_non_ainative_yaml_block(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.md"
    spec_text = "---\ntitle: Visual Spec\n---\n# Plain Spec\n\nShip the thing.\n"
    spec_path.write_text(spec_text, encoding="utf-8")

    parsed = parse_spec(spec_path)

    assert parsed.body == spec_text
    assert parsed.frontmatter == {}
    assert parsed.reference_manifest is None

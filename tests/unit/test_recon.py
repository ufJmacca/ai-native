from __future__ import annotations

from ai_native.stages.recon import _scan_repository


def test_scan_repository_does_not_special_case_ai_native_named_directories(tmp_path) -> None:
    (tmp_path / "ai_native").mkdir()
    (tmp_path / "ai_native" / "cli.py").write_text("print('workflow')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")

    scan = _scan_repository(tmp_path)

    assert scan["repo_state"] == "existing"
    assert scan["source_file_count"] == 1
    assert scan["test_frameworks"] == ["pytest"]
    assert "ai_native" in scan["top_level_areas"]


def test_scan_repository_ignores_runtime_noise_but_keeps_real_repo_context(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('app')\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").mkdir()
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text("# architecture\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "left-pad.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    scan = _scan_repository(tmp_path)

    assert scan["repo_state"] == "existing"
    assert scan["languages"] == ["python"]
    assert scan["source_file_count"] == 1
    assert "app" in scan["top_level_areas"]
    assert ".github" in scan["top_level_areas"]
    assert "docs" in scan["top_level_areas"]
    assert "node_modules" not in scan["top_level_areas"]
    assert "artifacts" not in scan["top_level_areas"]

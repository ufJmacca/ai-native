from __future__ import annotations

from pathlib import Path

from ai_native.config import AppConfig


def test_load_config_resolves_paths_from_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = AppConfig.load(repo_root / "ainative.yaml")

    assert config.repo_root == repo_root
    assert config.workspace.artifacts_dir == Path(".ai-native/runs")
    assert config.workspace.specs_dir == repo_root / "specs"
    assert config.git.branch_prefix == "codex"
    assert config.package_root == repo_root / "ai_native"


def test_load_config_uses_defaults_when_file_is_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"

    config = AppConfig.load(config_path)

    assert config.config_path == config_path.resolve()
    assert config.repo_root == tmp_path.resolve()
    assert config.workspace.specs_dir == (tmp_path / "specs").resolve()
    assert set(config.agents) == {"builder", "critic", "verifier", "pr_reviewer"}

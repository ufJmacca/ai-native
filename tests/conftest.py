from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.config import AppConfig


@pytest.fixture()
def app_config(tmp_path: Path) -> AppConfig:
    repo_root = Path(__file__).resolve().parents[1]
    config = AppConfig.load(repo_root / "ainative.yaml")
    config.workspace.artifacts_dir = tmp_path / "artifacts"
    return config


@pytest.fixture()
def tmp_spec(tmp_path: Path) -> Path:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        "# Sample Spec\n\nBuild a sample feature with test-first discipline.\n",
        encoding="utf-8",
    )
    return spec_path

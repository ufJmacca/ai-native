from __future__ import annotations

from pathlib import Path

import pytest

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


def test_telemetry_defaults_to_disabled() -> None:
    config = AppConfig.load(Path(__file__).resolve().parents[2] / "ainative.yaml")

    assert config.telemetry.enabled is False
    assert config.telemetry.profile is None
    assert config.telemetry.destinations == {}


def test_load_config_parses_named_telemetry_destinations(tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: true
  profile: datadog
  destinations:
    datadog:
      url: https://example.com/ingest
      auth_type: bearer
      credentials_ref: env:DATADOG_TOKEN
      headers:
        x-env: dev
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = AppConfig.load(config_path)

    assert config.telemetry.enabled is True
    assert config.telemetry.profile == "datadog"
    assert config.telemetry.destinations["datadog"].url == "https://example.com/ingest"
    assert config.telemetry.destinations["datadog"].auth_type == "bearer"
    assert config.telemetry.destinations["datadog"].credentials_ref == "env:DATADOG_TOKEN"
    assert config.telemetry.destinations["datadog"].headers == {"x-env": "dev"}


def test_load_config_applies_telemetry_environment_overrides(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: false
  url: https://old.example.com
  auth_type: none
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AINATIVE_TELEMETRY_URL", "https://telemetry.example.com")
    monkeypatch.setenv("AINATIVE_TELEMETRY_AUTH_TYPE", "bearer")
    monkeypatch.setenv("AINATIVE_TELEMETRY_TOKEN", "secret-token")
    monkeypatch.setenv("AINATIVE_TELEMETRY_TENANT", "demo")
    monkeypatch.setenv("AINATIVE_TELEMETRY_ENABLED", "true")

    config = AppConfig.load(config_path)

    assert config.telemetry.enabled is True
    assert config.telemetry.url == "https://telemetry.example.com"
    assert config.telemetry.auth_type == "bearer"
    assert config.telemetry.token == "secret-token"
    assert config.telemetry.tenant == "demo"


def test_load_config_normalizes_telemetry_auth_type_environment_override(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry:\n  auth_type: none\n", encoding="utf-8")
    monkeypatch.setenv("AINATIVE_TELEMETRY_AUTH_TYPE", "Bearer")

    config = AppConfig.load(config_path)

    assert config.telemetry.auth_type == "bearer"


def test_load_config_rejects_invalid_telemetry_auth_type_environment_override(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry:\n  auth_type: none\n", encoding="utf-8")
    monkeypatch.setenv("AINATIVE_TELEMETRY_AUTH_TYPE", "invalid")

    with pytest.raises(ValueError, match="Invalid AINATIVE_TELEMETRY_AUTH_TYPE"):
        AppConfig.load(config_path)

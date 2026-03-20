from __future__ import annotations

from pathlib import Path

import pytest

from ai_native.adapters import build_adapter
from ai_native.adapters.copilot import CopilotCLIAdapter
from ai_native.config import AppConfig


def test_load_config_resolves_paths_from_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = AppConfig.load(repo_root / "ainative.yaml")

    assert config.repo_root == repo_root
    assert config.workspace.artifacts_dir == Path(".ai-native/runs")
    assert config.workspace.specs_dir == repo_root / "specs"
    assert config.git.branch_prefix == "codex"
    assert config.package_root == repo_root / "ai_native"


def test_load_config_prefers_codex_defaults_when_no_provider_is_ready(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    monkeypatch.setattr("ai_native.config.shutil.which", lambda _name: None)

    config = AppConfig.load(config_path)

    assert config.config_path == config_path.resolve()
    assert config.repo_root == tmp_path.resolve()
    assert config.workspace.specs_dir == (tmp_path / "specs").resolve()
    assert set(config.agents) == {"builder", "critic", "verifier", "pr_reviewer"}
    assert config.agents["builder"].type == "codex-exec"
    assert config.agents["pr_reviewer"].type == "codex-review"


def test_load_config_uses_copilot_defaults_when_only_copilot_is_ready(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("ai_native.config.shutil.which", lambda name: f"/usr/bin/{name}" if name == "copilot" else None)

    config = AppConfig.load(config_path)

    assert config.agents["builder"].type == "copilot-cli"
    assert config.agents["pr_reviewer"].type == "copilot-cli"
    assert config.agents["pr_reviewer"].allow_all_permissions is False
    assert config.agents["pr_reviewer"].allow_tools == ["read", "shell(git:*)"]


def test_load_config_uses_codex_defaults_when_only_codex_is_ready(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("ai_native.config.shutil.which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

    config = AppConfig.load(config_path)

    assert config.agents["builder"].type == "codex-exec"
    assert config.agents["pr_reviewer"].type == "codex-review"


def test_load_config_prefers_codex_defaults_when_both_providers_are_ready(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "ai_native.config.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "copilot"} else None,
    )

    config = AppConfig.load(config_path)

    assert config.agents["builder"].type == "codex-exec"
    assert config.agents["pr_reviewer"].type == "codex-review"


def test_load_config_parses_copilot_cli_profile(tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
agents:
  builder:
    type: copilot-cli
    model: claude-sonnet-4.5
    autopilot: false
    allow_all_permissions: false
    silent: true
    no_ask_user: true
    max_autopilot_continues: 4
    allow_tools:
      - read
      - shell(git:*)
    allow_urls:
      - github.com
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = AppConfig.load(config_path)

    assert config.agents["builder"].type == "copilot-cli"
    assert config.agents["builder"].autopilot is False
    assert config.agents["builder"].allow_all_permissions is False
    assert config.agents["builder"].allow_tools == ["read", "shell(git:*)"]
    assert config.agents["builder"].allow_urls == ["github.com"]
    assert isinstance(build_adapter(config.agents["builder"]), CopilotCLIAdapter)


def test_copilot_example_config_parses() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    config = AppConfig.load(repo_root / "docs" / "examples" / "ainative.copilot.yaml")

    assert config.agents["builder"].type == "copilot-cli"
    assert config.agents["builder"].allow_all_permissions is True
    assert config.agents["pr_reviewer"].allow_all_permissions is False
    assert config.agents["pr_reviewer"].allow_tools == ["read", "shell(git:*)"]


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


def test_load_config_applies_run_registry_environment_overrides(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("registry:\n  heartbeat_interval_seconds: 15\n", encoding="utf-8")
    monkeypatch.setenv("AINATIVE_RUN_REGISTRY_URL", "https://registry.example.com")
    monkeypatch.setenv("AINATIVE_RUN_REGISTRY_AUTH_TOKEN", "registry-token")
    monkeypatch.setenv("AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS", "8.5")

    config = AppConfig.load(config_path)

    assert config.registry.remote_url == "https://registry.example.com"
    assert config.registry.auth_token == "registry-token"
    assert config.registry.timeout_seconds == 8.5


def test_load_config_rejects_invalid_run_registry_timeout_environment_override(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("registry: {}\n", encoding="utf-8")
    monkeypatch.setenv("AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS", "soon")

    with pytest.raises(ValueError, match="Invalid AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS"):
        AppConfig.load(config_path)

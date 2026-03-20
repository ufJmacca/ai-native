from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from ai_native.cli import _discover_config_path, _resolve_spec_path, _resolve_workspace_root, main
from ai_native.config import AgentProfile
from ai_native.models import RunState
from ai_native.utils import utc_now


def test_cli_stage_command_invokes_orchestrator(monkeypatch, capsys, app_config, tmp_spec: Path, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = workspace_root / "feature.md"
    spec_path.write_text(tmp_spec.read_text(encoding="utf-8"), encoding="utf-8")

    class FakeOrchestrator:
        def __init__(self, config, progress=None, question_responder=None):
            self.config = config
            self.progress = progress
            self.question_responder = question_responder

        def run_until(self, spec_path, target_stage, run_dir=None, dry_run_pr=False, workspace_root=None):  # type: ignore[no-untyped-def]
            assert spec_path == (Path(workspace_root) / "feature.md").resolve()
            assert target_stage == "plan"
            assert Path(workspace_root) == workspace_root.resolve()
            assert callable(self.progress)
            assert callable(self.question_responder)
            self.progress("[ainative] plan: started")
            return RunState(
                run_id="run-1",
                feature_slug="sample",
                spec_path=str(spec_path),
                workspace_root=str(workspace_root),
                spec_hash="hash",
                run_dir=str(tmp_path / "artifacts" / "run-1"),
                created_at=utc_now(),
                updated_at=utc_now(),
            )

    monkeypatch.setattr("ai_native.cli._load_config", lambda _config_path=None: app_config)
    monkeypatch.setattr("ai_native.cli.WorkflowOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ainative", "stage", "--stage", "plan", "--spec", "feature.md", "--workspace-dir", str(workspace_root)],
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "[ainative] plan: started" in output
    assert str(tmp_path / "artifacts" / "run-1") in output


def test_resolve_spec_path_prefers_target_workspace_when_present(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = workspace_root / "specs" / "task-management.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# Spec\n", encoding="utf-8")

    resolved = _resolve_spec_path(app_config, "specs/task-management.md", workspace_root)

    assert resolved == spec_path.resolve()


def test_resolve_spec_path_falls_back_to_template_repo(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    spec_path = app_config.repo_root / "specs" / "test-cli-fallback-spec.md"
    spec_path.write_text("# Spec\n", encoding="utf-8")
    try:
        resolved = _resolve_spec_path(app_config, "specs/test-cli-fallback-spec.md", workspace_root)
    finally:
        spec_path.unlink(missing_ok=True)

    assert resolved == spec_path.resolve()


def test_resolve_spec_path_raises_clear_error_when_missing(app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()

    with pytest.raises(SystemExit, match="Spec file not found. Checked:"):
        _resolve_spec_path(app_config, "specs/missing.md", workspace_root)


def test_discover_config_path_walks_up_to_parent_repo(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    nested = repo_root / "apps" / "web"
    nested.mkdir(parents=True)
    config_path = repo_root / "ainative.yaml"
    config_path.write_text("git:\n  branch_prefix: codex\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    discovered = _discover_config_path()

    assert discovered == config_path.resolve()


def test_discover_config_path_prefers_env_override(monkeypatch, tmp_path: Path) -> None:
    custom_config = tmp_path / "custom.yaml"
    monkeypatch.setenv("AINATIVE_CONFIG", str(custom_config))

    discovered = _discover_config_path()

    assert discovered == custom_config.resolve()


def test_resolve_workspace_root_defaults_to_current_directory(app_config, monkeypatch, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    monkeypatch.chdir(workspace_root)

    resolved = _resolve_workspace_root(app_config, None)

    assert resolved == workspace_root.resolve()


def test_cli_runs_list_and_detail(monkeypatch, capsys, app_config, tmp_path: Path) -> None:
    workspace_root = tmp_path / "target-repo"
    workspace_root.mkdir()
    artifacts_root = workspace_root / ".ai-native" / "runs"
    artifacts_root.mkdir(parents=True)

    spec = workspace_root / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")

    from ai_native.state import StateStore

    store = StateStore(artifacts_root)
    state = store.create_run(spec, workspace_root)

    app_config.workspace.artifacts_dir = Path(".ai-native/runs")
    monkeypatch.setattr("ai_native.cli._load_config", lambda _config_path=None: app_config)

    monkeypatch.setattr(
        sys,
        "argv",
        ["ainative", "runs", "list", "--workspace-dir", str(workspace_root)],
    )
    assert main() == 0
    list_output = capsys.readouterr().out
    assert '"status": "in_progress"' in list_output
    assert '"liveness": "active"' in list_output

    monkeypatch.setattr(
        sys,
        "argv",
        ["ainative", "runs", "detail", "--workspace-dir", str(workspace_root), "--run-dir", str(state.run_dir)],
    )
    assert main() == 0
    detail_output = capsys.readouterr().out
    assert f'"run_id": "{state.run_id}"' in detail_output
    assert '"status": "in_progress"' in detail_output
    assert '"liveness": "active"' in detail_output


def test_cli_doctor_reports_selected_codex_provider_when_copilot_is_missing(
    monkeypatch, capsys, app_config, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("ai_native.cli._load_config", lambda _config_path=None: app_config)
    monkeypatch.setattr(
        "ai_native.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "gh", "git", "uv", "mmdc"} else None,
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "doctor"])

    assert main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"]["codex"] is True
    assert payload["commands"]["copilot"] is False
    assert payload["paths"]["copilot_config"] is False
    assert payload["providers"]["codex"] == {"selected": True, "ready": True}
    assert payload["providers"]["copilot"] == {"selected": False, "ready": False}
    assert payload["paths"]["gh_config_dir"] is True


def test_cli_doctor_reports_selected_copilot_provider_when_codex_is_missing(
    monkeypatch, capsys, app_config, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    app_config.agents = {
        "builder": AgentProfile(type="copilot-cli"),
        "critic": AgentProfile(type="copilot-cli"),
        "verifier": AgentProfile(type="copilot-cli"),
        "pr_reviewer": AgentProfile(type="copilot-cli"),
    }

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("ai_native.cli._load_config", lambda _config_path=None: app_config)
    monkeypatch.setattr(
        "ai_native.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"copilot", "gh", "git", "uv", "mmdc"} else None,
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "doctor"])

    assert main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"]["codex"] is False
    assert payload["commands"]["copilot"] is True
    assert payload["paths"]["codex_auth"] is False
    assert payload["paths"]["copilot_config"] is False
    assert payload["providers"]["codex"] == {"selected": False, "ready": False}
    assert payload["providers"]["copilot"] == {"selected": True, "ready": True}


def test_cli_doctor_auto_selects_copilot_when_config_is_missing(monkeypatch, capsys, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "ai_native.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"copilot", "gh", "git", "uv", "mmdc"} else None,
    )
    monkeypatch.setattr(
        "ai_native.config.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "copilot" else None,
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "doctor"])

    assert main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["config_exists"] is False
    assert payload["providers"]["codex"] == {"selected": False, "ready": False}
    assert payload["providers"]["copilot"] == {"selected": True, "ready": True}


def test_cli_doctor_auto_selects_codex_when_config_is_missing(monkeypatch, capsys, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".gitconfig").write_text("[user]\n  name = Test User\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "ai_native.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "gh", "git", "uv", "mmdc"} else None,
    )
    monkeypatch.setattr(
        "ai_native.config.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "codex" else None,
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "doctor"])

    assert main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["config_exists"] is False
    assert payload["providers"]["codex"] == {"selected": True, "ready": True}
    assert payload["providers"]["copilot"] == {"selected": False, "ready": False}


def test_cli_telemetry_profile_add_use_and_list(monkeypatch, capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("workspace:\n  artifacts_dir: .ai-native/runs\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "--config",
            str(config_path),
            "profile",
            "add",
            "prod",
            "--url",
            "https://example.com/events",
            "--auth-type",
            "bearer",
            "--credentials-ref",
            "env:TELEMETRY_TOKEN",
            "--header",
            "x-team=platform",
        ],
    )
    assert main() == 0

    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "--config", str(config_path), "profile", "use", "prod"])
    assert main() == 0

    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "--config", str(config_path), "profile", "list"])
    assert main() == 0

    output = capsys.readouterr().out
    assert "Added telemetry profile 'prod'" in output
    assert "Using telemetry profile 'prod'" in output
    assert "* prod: https://example.com/events" in output

    payload = config_path.read_text(encoding="utf-8")
    assert "enabled: true" in payload
    assert "profile: prod" in payload
    assert "credentials_ref: env:TELEMETRY_TOKEN" in payload


def test_cli_telemetry_profile_use_fails_for_unknown_profile(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry:\n  enabled: false\n  destinations: {}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "--config", str(config_path), "profile", "use", "missing"])

    with pytest.raises(SystemExit, match="Telemetry profile 'missing' is not configured"):
        main()


def test_cli_telemetry_profile_list_accepts_nested_config_flag(monkeypatch, capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: true
  profile: prod
  destinations:
    prod:
      url: https://example.com/events
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["ainative", "telemetry", "profile", "list", "--config", str(config_path)],
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "* prod: https://example.com/events" in output


def test_cli_telemetry_profile_add_recovers_from_null_mappings(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry: null\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "profile",
            "add",
            "--config",
            str(config_path),
            "prod",
            "--url",
            "https://example.com/events",
        ],
    )

    assert main() == 0
    payload = config_path.read_text(encoding="utf-8")
    assert "telemetry:" in payload
    assert "destinations:" in payload
    assert "prod:" in payload


def test_cli_telemetry_profile_use_recovers_from_null_destinations(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: false
  destinations: null
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "profile", "use", "--config", str(config_path), "prod"])

    with pytest.raises(SystemExit, match="Telemetry profile 'prod' is not configured"):
        main()


def test_cli_telemetry_profile_commands_reject_non_mapping_telemetry(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry: true\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "profile", "list", "--config", str(config_path)])

    with pytest.raises(SystemExit, match="Invalid telemetry config: expected mapping at 'telemetry'"):
        main()


def test_telemetry_configure_writes_config_and_masks_output(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("workspace:\n  specs_dir: specs\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "--config",
            str(config_path),
            "configure",
            "--url",
            "https://telemetry.example.com/ingest",
            "--auth-type",
            "api_key",
            "--api-key",
            "very-secret-key",
            "--tenant",
            "proj-a",
        ],
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "very-secret-key" not in output

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["telemetry"]["url"] == "https://telemetry.example.com/ingest"
    assert persisted["telemetry"]["auth_type"] == "api_key"
    assert persisted["telemetry"]["api_key"] == "very-secret-key"
    assert persisted["telemetry"]["tenant"] == "proj-a"


def test_telemetry_show_masks_secrets(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: true
  url: https://telemetry.example.com
  auth_type: bearer
  token: super-secret-token
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "--config", str(config_path), "show"])

    assert main() == 0
    output = capsys.readouterr().out
    assert "super-secret-token" not in output
    payload = json.loads(output)
    assert payload["token"] != "super-secret-token"


def test_telemetry_configure_rejects_missing_bearer_token(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  url: https://telemetry.example.com
  auth_type: none
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "--config",
            str(config_path),
            "configure",
            "--auth-type",
            "bearer",
        ],
    )

    with pytest.raises(SystemExit, match="Telemetry auth_type=bearer requires --token"):
        main()


def test_telemetry_configure_preserves_existing_enabled_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: false
  url: https://telemetry.example.com/ingest
  auth_type: bearer
  token: existing-token
  tenant: old-tenant
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "--config",
            str(config_path),
            "configure",
            "--tenant",
            "new-tenant",
        ],
    )

    assert main() == 0

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["telemetry"]["enabled"] is False
    assert persisted["telemetry"]["tenant"] == "new-tenant"


def test_telemetry_show_accepts_config_flag_after_subcommand(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry:\n  auth_type: none\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "show", "--config", str(config_path)])

    assert main() == 0


def test_telemetry_configure_preserves_string_false_enabled_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text(
        """
telemetry:
  enabled: "false"
  url: https://telemetry.example.com/ingest
  auth_type: bearer
  token: existing-token
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ainative",
            "telemetry",
            "--config",
            str(config_path),
            "configure",
            "--tenant",
            "new-tenant",
        ],
    )

    assert main() == 0

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["telemetry"]["enabled"] is False


def test_telemetry_test_returns_error_without_url(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ainative.yaml"
    config_path.write_text("telemetry:\n  auth_type: none\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ainative", "telemetry", "--config", str(config_path), "test"])

    with pytest.raises(SystemExit, match="Telemetry URL is not configured"):
        main()

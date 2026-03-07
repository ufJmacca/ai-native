from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_native.adapters.codex import CodexExecAdapter, CodexReviewAdapter
from ai_native.config import AgentProfile


def test_codex_exec_adapter_writes_schema_output(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text('{"title":"ok","summary":"ok","implementation_steps":[],"interfaces":[],"data_flow":[],"edge_cases":[],"test_strategy":[],"rollout_notes":[]}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path, schema_path=tmp_path / "schema.json")

    assert result.json_data["title"] == "ok"
    assert "--output-schema" in captured["command"]
    assert "--full-auto" in captured["command"]


def test_codex_review_adapter_uses_profile_model_and_extra_args(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="review ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CodexReviewAdapter(
        AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            base_branch="develop",
            extra_args=["-c", 'model_reasoning_effort="xhigh"'],
        )
    )

    result = adapter.run("prompt", cwd=tmp_path)

    assert result.text == "review ok"
    assert captured["command"] == [
        "codex",
        "review",
        "-c",
        'model="gpt-5.4"',
        "-c",
        'model_reasoning_effort="xhigh"',
        "--base",
        "develop",
        "prompt",
    ]


def test_codex_exec_adapter_retries_without_workspace_write_when_landlock_panics(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        commands.append(command)
        output_path = Path(command[command.index("-o") + 1])
        if len(commands) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "thread 'main' panicked at linux-sandbox/src/linux_run_main.rs:167:9\n"
                    "error applying legacy Linux sandbox restrictions: Sandbox(LandlockRestrict)"
                ),
            )
        output_path.write_text(
            '{"title":"ok","summary":"ok","implementation_steps":[],"interfaces":[],"data_flow":[],"edge_cases":[],"test_strategy":[],"rollout_notes":[]}',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: True)
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path, schema_path=tmp_path / "schema.json")

    assert result.json_data["title"] == "ok"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-s") + 1] == "workspace-write"
    assert commands[1][commands[1].index("-s") + 1] == "danger-full-access"

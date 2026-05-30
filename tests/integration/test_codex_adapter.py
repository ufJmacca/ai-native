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
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: False)
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path, schema_path=tmp_path / "schema.json")

    assert result.json_data["title"] == "ok"
    assert "--output-schema" in captured["command"]
    assert "--full-auto" in captured["command"]


def test_codex_exec_adapter_includes_image_attachments(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    image_path = tmp_path / "reference.png"
    image_path.write_text("png", encoding="utf-8")

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: False)
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    adapter.run("prompt", cwd=tmp_path, image_paths=[image_path])

    assert "--image" in captured["command"]
    assert str(image_path) in captured["command"]
    assert adapter.supports_image_inputs() is True


def test_codex_exec_adapter_defaults_to_unsandboxed_mode_in_container(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: True)
    monkeypatch.delenv("AINATIVE_CODEX_CONTAINER_SANDBOX", raising=False)
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path)

    assert result.text == "ok"
    assert "-s" not in captured["command"]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["command"]
    assert "--full-auto" not in captured["command"]


def test_codex_review_adapter_uses_profile_model_and_extra_args(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("review ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: False)
    monkeypatch.delenv("AINATIVE_CODEX_CONTAINER_SANDBOX", raising=False)
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
    command = captured["command"]
    assert command[:8] == [
        "codex",
        "exec",
        "-C",
        str(tmp_path),
        "review",
        "-m",
        "gpt-5.4",
        "-o",
    ]
    assert Path(command[8]).name == "codex-review.txt"
    assert command[9:] == [
        "-c",
        'model_reasoning_effort="xhigh"',
        "--base",
        "develop",
    ]


def test_codex_review_adapter_uses_prompt_when_no_base_branch(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("review ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: False)
    monkeypatch.delenv("AINATIVE_CODEX_CONTAINER_SANDBOX", raising=False)
    adapter = CodexReviewAdapter(
        AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            extra_args=["-c", 'model_reasoning_effort="xhigh"'],
        )
    )

    result = adapter.review(cwd=tmp_path, prompt="prompt", base_branch=None)

    assert result.text == "review ok"
    command = captured["command"]
    assert command[:8] == [
        "codex",
        "exec",
        "-C",
        str(tmp_path),
        "review",
        "-m",
        "gpt-5.4",
        "-o",
    ]
    assert Path(command[8]).name == "codex-review.txt"
    assert command[9:] == [
        "-c",
        'model_reasoning_effort="xhigh"',
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
    monkeypatch.setenv("AINATIVE_CODEX_CONTAINER_SANDBOX", "workspace-write")
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path, schema_path=tmp_path / "schema.json")

    assert result.json_data["title"] == "ok"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-s") + 1] == "workspace-write"
    assert "-s" not in commands[1]
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[1]
    assert "--full-auto" not in commands[1]


def test_codex_exec_adapter_retries_when_agent_message_contains_landlock(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        commands.append(command)
        output_path = Path(command[command.index("-o") + 1])
        if len(commands) == 1:
            output_path.write_text(
                (
                    "**Blocked**\n\n"
                    "error applying legacy Linux sandbox restrictions: Sandbox(LandlockRestrict)\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        output_path.write_text("# Builder Summary\nRecovered.\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: True)
    monkeypatch.setenv("AINATIVE_CODEX_CONTAINER_SANDBOX", "workspace-write")
    adapter = CodexExecAdapter(
        AgentProfile(type="codex-exec", model="gpt-5-codex", sandbox="workspace-write", extra_args=["--full-auto"])
    )

    result = adapter.run("prompt", cwd=tmp_path)

    assert result.text == "# Builder Summary\nRecovered."
    assert len(commands) == 2
    assert commands[0][commands[0].index("-s") + 1] == "workspace-write"
    assert "-s" not in commands[1]
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[1]
    assert "--full-auto" not in commands[1]


def test_codex_review_adapter_defaults_to_unsandboxed_mode_in_container(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("review from file", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: True)
    monkeypatch.delenv("AINATIVE_CODEX_CONTAINER_SANDBOX", raising=False)
    adapter = CodexReviewAdapter(
        AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            base_branch="main",
            extra_args=["-c", 'model_reasoning_effort="xhigh"'],
        )
    )

    result = adapter.review(cwd=tmp_path, prompt="prompt", base_branch="main")

    command = captured["command"]
    assert result.text == "review from file"
    assert command[:5] == ["codex", "exec", "-C", str(tmp_path), "review"]
    assert "-s" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--full-auto" not in command


def test_codex_review_adapter_retries_without_workspace_write_when_bubblewrap_fails(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        commands.append(command)
        output_path = Path(command[command.index("-o") + 1])
        if len(commands) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="bwrap: Creating new namespace failed: Operation not permitted",
            )
        output_path.write_text("# Review\nRecovered.\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: True)
    monkeypatch.setenv("AINATIVE_CODEX_CONTAINER_SANDBOX", "workspace-write")
    adapter = CodexReviewAdapter(
        AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            base_branch="main",
            sandbox="workspace-write",
            extra_args=["-c", 'model_reasoning_effort="xhigh"'],
        )
    )

    result = adapter.review(cwd=tmp_path, prompt="prompt", base_branch="main")

    assert result.text == "# Review\nRecovered."
    assert len(commands) == 2
    assert commands[0][commands[0].index("-s") + 1] == "workspace-write"
    assert "-s" not in commands[1]
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[1]


def test_codex_review_adapter_keeps_profile_sandbox_outside_container(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("review ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ai_native.adapters.codex._running_in_container", lambda: False)
    monkeypatch.delenv("AINATIVE_CODEX_CONTAINER_SANDBOX", raising=False)
    adapter = CodexReviewAdapter(
        AgentProfile(
            type="codex-review",
            model="gpt-5.4",
            base_branch="main",
            sandbox="workspace-write",
        )
    )

    result = adapter.review(cwd=tmp_path, prompt="prompt", base_branch="main")

    command = captured["command"]
    assert result.text == "review ok"
    assert command[:7] == ["codex", "exec", "-C", str(tmp_path), "-s", "workspace-write", "review"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command

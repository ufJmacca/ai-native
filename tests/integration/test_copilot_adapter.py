from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_native.adapters.base import AdapterError
from ai_native.adapters.copilot import CopilotCLIAdapter
from ai_native.config import AgentProfile


def test_copilot_cli_adapter_defaults_to_autonomous_mode(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="builder ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli", model="gpt-5"))

    result = adapter.run("prompt", cwd=tmp_path)

    assert result.text == "builder ok"
    assert captured["command"] == [
        "copilot",
        "--model",
        "gpt-5",
        "-s",
        "--no-ask-user",
        "--autopilot",
        "--max-autopilot-continues",
        "10",
        "--yolo",
        "-p",
        "prompt",
    ]


def test_copilot_cli_adapter_ignores_image_attachments(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    image_path = tmp_path / "reference.png"
    image_path.write_text("png", encoding="utf-8")

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="builder ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    result = adapter.run("prompt", cwd=tmp_path, image_paths=[image_path])

    assert result.text == "builder ok"
    assert "--image" not in captured["command"]
    assert str(image_path) not in captured["command"]
    assert adapter.supports_image_inputs() is False


def test_copilot_cli_adapter_parses_schema_output(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object","properties":{"title":{"type":"string"}}}', encoding="utf-8")

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout='{"title":"ok"}\n', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    result = adapter.run("prompt", cwd=tmp_path, schema_path=schema_path)

    prompt = captured["command"][captured["command"].index("-p") + 1]  # type: ignore[index]
    assert result.json_data == {"title": "ok"}
    assert "Return only valid JSON." in prompt
    assert '"title"' in prompt


def test_copilot_cli_adapter_repairs_invalid_json_once(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"object","properties":{"title":{"type":"string"}}}', encoding="utf-8")

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        commands.append(command)
        if len(commands) == 1:
            return SimpleNamespace(returncode=0, stdout="not json\n", stderr="")
        return SimpleNamespace(returncode=0, stdout='{"title":"repaired"}\n', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    result = adapter.run("prompt", cwd=tmp_path, schema_path=schema_path)

    repair_prompt = commands[1][commands[1].index("-p") + 1]
    assert result.json_data == {"title": "repaired"}
    assert len(commands) == 2
    assert "Previous invalid response:" in repair_prompt
    assert "not json" in repair_prompt


def test_copilot_cli_adapter_repairs_schema_violations_once(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"type":"object","additionalProperties":false,"required":["title"],"properties":{"title":{"type":"string"}}}',
        encoding="utf-8",
    )

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        commands.append(command)
        if len(commands) == 1:
            return SimpleNamespace(returncode=0, stdout='{"title":"ok","extra":true}\n', stderr="")
        return SimpleNamespace(returncode=0, stdout='{"title":"repaired"}\n', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    result = adapter.run("prompt", cwd=tmp_path, schema_path=schema_path)

    assert result.json_data == {"title": "repaired"}
    assert len(commands) == 2


def test_copilot_cli_adapter_raises_when_repaired_json_still_violates_schema(monkeypatch, tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"type":"object","additionalProperties":false,"required":["questions"],"properties":{"questions":{"type":"array","items":{"type":"string"},"maxItems":1}}}',
        encoding="utf-8",
    )

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout='{"questions":["one","two"]}\n', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    with pytest.raises(AdapterError, match="did not satisfy schema"):
        adapter.run("prompt", cwd=tmp_path, schema_path=schema_path)


def test_copilot_cli_adapter_respects_permission_overrides(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="review ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(
        AgentProfile(
            type="copilot-cli",
            autopilot=False,
            allow_all_permissions=False,
            allow_tools=["read", "shell(git:*)"],
            deny_urls=["github.com"],
            extra_args=["--debug"],
        )
    )

    result = adapter.run("prompt", cwd=tmp_path)

    assert result.text == "review ok"
    assert captured["command"] == [
        "copilot",
        "-s",
        "--no-ask-user",
        "--allow-tool",
        "read",
        "--allow-tool",
        "shell(git:*)",
        "--deny-url",
        "github.com",
        "--debug",
        "-p",
        "prompt",
    ]


def test_copilot_cli_adapter_review_uses_code_review_agent(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="review ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(
        AgentProfile(
            type="copilot-cli",
            autopilot=False,
            allow_all_permissions=False,
            allow_tools=["read", "shell(git:*)"],
        )
    )

    result = adapter.review(cwd=tmp_path, prompt="prompt", base_branch="main")

    review_prompt = captured["command"][captured["command"].index("-p") + 1]  # type: ignore[index]
    assert result.text == "review ok"
    assert captured["command"] == [
        "copilot",
        "--agent",
        "code-review",
        "-s",
        "--no-ask-user",
        "--allow-tool",
        "read",
        "--allow-tool",
        "shell(git:*)",
        "-p",
        review_prompt,
    ]
    assert "git base branch `main`" in review_prompt
    assert "--autopilot" not in captured["command"]


def test_copilot_cli_adapter_review_defaults_to_autopilot(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="review ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    adapter.review(cwd=tmp_path, prompt="prompt", base_branch="main")

    assert "--autopilot" in captured["command"]
    assert "--max-autopilot-continues" in captured["command"]


def test_copilot_cli_adapter_raises_when_binary_is_missing(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(command[0])

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = CopilotCLIAdapter(AgentProfile(type="copilot-cli"))

    with pytest.raises(AdapterError, match="copilot"):
        adapter.run("prompt", cwd=tmp_path)

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_native.adapters.codex import CodexExecAdapter
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


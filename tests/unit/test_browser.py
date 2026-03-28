from __future__ import annotations

import subprocess
from pathlib import Path

from ai_native.models import PreviewConfig
from ai_native.stages.verify import preview_session


class _FakeProcess:
    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> None:
        return None


def test_preview_session_avoids_unread_pipes(monkeypatch) -> None:
    popen_calls: list[dict[str, object]] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return _FakeProcess()

    monkeypatch.setattr("ai_native.browser.subprocess.Popen", fake_popen)
    monkeypatch.setattr("ai_native.browser._wait_for_url", lambda preview, process: None)

    preview = PreviewConfig(url="http://localhost:4173", command=["npm", "run", "dev"])
    with preview_session(preview, cwd=Path.cwd()):
        pass

    assert len(popen_calls) == 1
    assert popen_calls[0]["stdout"] == subprocess.DEVNULL
    assert popen_calls[0]["stderr"] == subprocess.DEVNULL

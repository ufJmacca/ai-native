from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

from ai_native.models import PreviewConfig, ReferenceInput, ViewportConfig
from ai_native.stages.verify import capture_implementation_screenshots, preview_session


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


def test_capture_implementation_screenshots_uses_load_wait(tmp_path: Path, monkeypatch) -> None:
    goto_calls: list[dict[str, object]] = []

    class _FakePage:
        def goto(self, url: str, wait_until: str) -> None:
            goto_calls.append({"url": url, "wait_until": wait_until})

        def screenshot(self, path: str, full_page: bool) -> None:
            Path(path).write_bytes(b"png")

    class _FakeContext:
        def new_page(self) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            return None

    class _FakeBrowser:
        def new_context(self, viewport: dict[str, int]) -> _FakeContext:
            return _FakeContext()

        def close(self) -> None:
            return None

    class _FakePlaywright:
        class _Chromium:
            @staticmethod
            def launch() -> _FakeBrowser:
                return _FakeBrowser()

        chromium = _Chromium()

    class _FakeSyncPlaywright:
        def __enter__(self) -> _FakePlaywright:
            return _FakePlaywright()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _FakeSyncPlaywright()
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    preview = PreviewConfig(url="http://localhost:4173")
    references = [
        ReferenceInput(
            id="hero",
            label="Hero",
            kind="image",
            path=str(tmp_path / "reference.png"),
            route="/",
            viewport=ViewportConfig(width=1440, height=1200, label="desktop"),
        )
    ]
    Path(references[0].path).write_bytes(b"ref")

    captures = capture_implementation_screenshots(preview, references, tmp_path / "captures")

    assert len(captures) == 1
    assert goto_calls == [{"url": "http://localhost:4173/", "wait_until": "load"}]

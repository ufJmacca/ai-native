from __future__ import annotations

import subprocess
import sys
import types
import urllib.error
from pathlib import Path

from ai_native.models import PreviewConfig, PreviewReadinessConfig, ReferenceInput, ViewportConfig
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


def test_capture_implementation_screenshots_preserves_absolute_routes(tmp_path: Path, monkeypatch) -> None:
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

    preview = PreviewConfig(url="http://localhost:4173/app")
    references = [
        ReferenceInput(
            id="settings",
            label="Settings",
            kind="image",
            path=str(tmp_path / "reference.png"),
            route="/settings",
            viewport=ViewportConfig(width=1440, height=1200, label="desktop"),
        )
    ]
    Path(references[0].path).write_bytes(b"ref")

    captures = capture_implementation_screenshots(preview, references, tmp_path / "captures")

    assert len(captures) == 1
    assert goto_calls == [{"url": "http://localhost:4173/settings", "wait_until": "load"}]


def test_preview_session_accepts_expected_http_error_status(monkeypatch) -> None:
    preview = PreviewConfig(
        url="http://localhost:4173",
        readiness=PreviewReadinessConfig(expect_status=401, timeout_seconds=1, interval_seconds=0.01),
    )
    unauthorized = urllib.error.HTTPError(preview.url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(
        "ai_native.browser.urllib.request.urlopen",
        lambda request, timeout=5: (_ for _ in ()).throw(unauthorized),
    )

    with preview_session(preview, cwd=Path.cwd()):
        pass


def test_capture_implementation_screenshots_disambiguates_colliding_slugs(tmp_path: Path, monkeypatch) -> None:
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
            id="hero/mobile",
            label="Hero mobile",
            kind="image",
            path=str(tmp_path / "reference-a.png"),
            route="/",
            viewport=ViewportConfig(width=1440, height=1200, label="desktop"),
        ),
        ReferenceInput(
            id="hero-mobile",
            label="Hero mobile alt",
            kind="image",
            path=str(tmp_path / "reference-b.png"),
            route="/alternate",
            viewport=ViewportConfig(width=1440, height=1200, label="desktop"),
        ),
    ]
    Path(references[0].path).write_bytes(b"ref-a")
    Path(references[1].path).write_bytes(b"ref-b")

    captures = capture_implementation_screenshots(preview, references, tmp_path / "captures")

    assert len(captures) == 2
    assert captures[0].path != captures[1].path
    assert captures[0].path.exists()
    assert captures[1].path.exists()

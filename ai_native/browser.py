from __future__ import annotations

import contextlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ai_native.models import PreviewConfig, ReferenceInput
from ai_native.stages.common import StageError
from ai_native.utils import ensure_dir, slugify


@dataclass(frozen=True)
class ImplementationCapture:
    route: str
    viewport_label: str
    viewport_width: int
    viewport_height: int
    path: Path


def _wait_for_url(preview: PreviewConfig, process: subprocess.Popen[str] | None) -> None:
    deadline = time.monotonic() + preview.readiness.timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise StageError(f"Preview command exited before {preview.url} became ready.")
        try:
            request = urllib.request.Request(preview.url, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == preview.readiness.expect_status:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(preview.readiness.interval_seconds)
    raise StageError(f"Preview URL {preview.url} did not become ready: {last_error or 'timed out'}")


@contextlib.contextmanager
def preview_session(preview: PreviewConfig, cwd: Path):
    process: subprocess.Popen[str] | None = None
    if preview.command:
        if isinstance(preview.command, str):
            command: list[str] = ["/bin/sh", "-lc", preview.command]
        else:
            command = list(preview.command)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    try:
        _wait_for_url(preview, process)
        yield
    finally:
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def capture_implementation_screenshots(
    preview: PreviewConfig,
    references: list[ReferenceInput],
    output_dir: Path,
) -> list[ImplementationCapture]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise StageError(
            "Playwright is not installed. Run `make bootstrap` or `uv run python -m playwright install chromium`."
        ) from exc

    ensure_dir(output_dir)
    captures: list[ImplementationCapture] = []
    seen: set[tuple[str, str, int, int]] = set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for reference in references:
                viewport = reference.viewport
                key = (reference.route, viewport.resolved_label, viewport.width, viewport.height)
                if key in seen:
                    continue
                seen.add(key)
                context = browser.new_context(viewport={"width": viewport.width, "height": viewport.height})
                try:
                    page = context.new_page()
                    target_url = urllib.parse.urljoin(preview.url.rstrip("/") + "/", reference.route.lstrip("/"))
                    page.goto(target_url, wait_until="load")
                    filename = f"{slugify(reference.id)}-{slugify(viewport.resolved_label)}-implementation.png"
                    output_path = output_dir / filename
                    page.screenshot(path=str(output_path), full_page=True)
                    captures.append(
                        ImplementationCapture(
                            route=reference.route,
                            viewport_label=viewport.resolved_label,
                            viewport_width=viewport.width,
                            viewport_height=viewport.height,
                            path=output_path,
                        )
                    )
                finally:
                    context.close()
        finally:
            browser.close()
    return captures

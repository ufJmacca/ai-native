from __future__ import annotations

from pathlib import Path
import tomllib

import ai_native


def test_package_version_matches_pyproject() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert ai_native.__version__ == pyproject["project"]["version"]


def test_resolve_version_prefers_local_pyproject_over_installed_metadata(monkeypatch) -> None:
    monkeypatch.setattr(ai_native, "package_version", lambda _package_name: "9.9.9")

    assert ai_native._resolve_version() == ai_native._pyproject_version()


def test_resolve_version_falls_back_to_distribution_metadata_when_pyproject_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(ai_native, "_pyproject_version", lambda: None)
    monkeypatch.setattr(ai_native, "package_version", lambda _package_name: "2.3.4")

    assert ai_native._resolve_version() == "2.3.4"

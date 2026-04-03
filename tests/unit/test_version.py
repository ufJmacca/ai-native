from __future__ import annotations

from pathlib import Path
import tomllib

from ai_native import __version__


def test_package_version_matches_pyproject() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]

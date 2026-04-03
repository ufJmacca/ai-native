"""AI Native workflow template package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import tomllib

__all__ = ["__version__"]


def _pyproject_version() -> str | None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8")).get("project", {})
    except OSError:
        return None

    version = project.get("version")
    if version is None:
        return None
    return str(version)


def _resolve_version() -> str:
    if pyproject_version := _pyproject_version():
        return pyproject_version

    try:
        return package_version("ai-native-base")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()

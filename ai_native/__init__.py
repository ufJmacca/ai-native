"""AI Native workflow template package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import tomllib

__all__ = ["__version__"]


def _fallback_version() -> str:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8")).get("project", {})
    except OSError:
        return "0+unknown"
    return str(project.get("version", "0+unknown"))


def _resolve_version() -> str:
    try:
        return package_version("ai-native-base")
    except PackageNotFoundError:
        return _fallback_version()


__version__ = _resolve_version()

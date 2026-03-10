from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

from financial_rag_analyst.app import bootstrap_application


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_financial_rag(*args: str) -> subprocess.CompletedProcess[str]:
    cli_path = shutil.which("financial-rag")
    assert cli_path is not None, "financial-rag executable is not available in the test environment"
    return subprocess.run(
        [cli_path, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pyproject_defines_financial_rag_console_script_and_dependencies() -> None:
    # Arrange
    pyproject_path = REPO_ROOT / "pyproject.toml"

    # Act
    metadata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = metadata["project"]
    dev_dependencies = metadata["dependency-groups"]["dev"]

    # Assert
    assert project["requires-python"] == ">=3.12"
    assert project["scripts"]["financial-rag"] == "financial_rag_analyst.cli:main"
    assert any(dependency.startswith("google-adk") for dependency in project["dependencies"])
    assert any(dependency.startswith("pytest") for dependency in dev_dependencies)


def test_scaffold_directories_and_env_example_exist_without_live_credentials() -> None:
    # Arrange
    expected_paths = [
        REPO_ROOT / "financial_rag_analyst",
        REPO_ROOT / "tests",
        REPO_ROOT / "fixtures",
        REPO_ROOT / "docs",
        REPO_ROOT / ".env.example",
    ]

    # Act
    existing_paths = [path.exists() for path in expected_paths]
    example_lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    configured_values = [
        line.split("=", 1)[1].strip()
        for line in example_lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    ]

    # Assert
    assert all(existing_paths)
    assert configured_values
    assert all(
        not value or "example" in value.lower() or value.startswith("<")
        for value in configured_values
    )


def test_bootstrap_application_returns_local_app_defaults() -> None:
    # Arrange
    repo_root = REPO_ROOT

    # Act
    application = bootstrap_application(repo_root=repo_root)

    # Assert
    assert application.name == "financial-rag-analyst"
    assert application.framework == "google-adk"
    assert application.default_response_format == "a2ui"
    assert application.repo_root == repo_root


def test_financial_rag_help_executes_real_console_script() -> None:
    # Arrange
    command = ["--help"]

    # Act
    result = _run_financial_rag(*command)
    output = result.stdout + result.stderr

    # Assert
    assert result.returncode == 0
    assert "usage: financial-rag" in output
    assert "Financial Agentic RAG analyst scaffold CLI." in output
    assert "bootstrap" in output


def test_financial_rag_bootstrap_executes_real_console_script() -> None:
    # Arrange
    command = ["bootstrap"]

    # Act
    result = _run_financial_rag(*command)
    output = result.stdout + result.stderr

    # Assert
    assert result.returncode == 0
    assert "financial-rag-analyst bootstrap ready at" in output
    assert str(REPO_ROOT) in output

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from ai_native.cli import main


def test_factory_command_group_exposes_runner_subcommands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ainative", "factory", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "factory-runner-protocol/v1" in output
    assert "{run,verify}" in output


def test_factory_command_group_never_prompts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("factory command group must not prompt"),
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "factory"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "required" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "operation",
    [
        "run",
        "verify",
    ],
)
def test_factory_subcommand_help_is_non_interactive(
    monkeypatch,
    capsys,
    operation: str,
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("factory commands must not prompt"),
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "factory", operation, "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--run-spec" in output
    assert "--output-dir" in output


def test_built_wheel_exposes_factory_runner_commands(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    wheelhouse = tmp_path / "wheelhouse"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheelhouse)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr

    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1

    environment_root = tmp_path / "installed"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr

    scripts_dir = environment_root / ("Scripts" if os.name == "nt" else "bin")
    environment_python = scripts_dir / ("python.exe" if os.name == "nt" else "python")

    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(wheels[0]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr

    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"

    imported = subprocess.run(
        [
            str(environment_python),
            "-c",
            "import ai_native; print(ai_native.__file__)",
        ],
        cwd=outside_checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    assert str(environment_root) in imported.stdout
    assert str(repository_root) not in imported.stdout

    completed = subprocess.run(
        [str(scripts_dir / "ainative"), "factory", "--help"],
        cwd=outside_checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "factory-runner-protocol/v1" in completed.stdout
    assert "{run,verify}" in completed.stdout

    for operation in ("run", "verify"):
        operation_help = subprocess.run(
            [str(scripts_dir / "ainative"), "factory", operation, "--help"],
            cwd=outside_checkout,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert operation_help.returncode == 0, operation_help.stderr
        assert "--run-spec" in operation_help.stdout
        assert "--output-dir" in operation_help.stdout

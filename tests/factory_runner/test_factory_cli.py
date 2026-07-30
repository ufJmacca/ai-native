from __future__ import annotations

import subprocess
import sys

import pytest

from ai_native.cli import main


def test_factory_command_group_is_reserved_without_runner_subcommands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ainative", "factory", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "factory-runner-protocol/v1" in output
    assert "run" not in output
    assert "verify" not in output


def test_factory_command_group_never_prompts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("factory command group must not prompt"),
    )
    monkeypatch.setattr(sys, "argv", ["ainative", "factory"])

    assert main() == 0
    assert "reserved" in capsys.readouterr().out.lower()


def test_installed_cli_exposes_reserved_factory_group() -> None:
    completed = subprocess.run(
        ["ainative", "factory", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "factory-runner-protocol/v1" in completed.stdout


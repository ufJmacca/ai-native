from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.generate_factory_runner_goldens import (
    DEFAULT_OUTPUT_DIR,
    render_runtime_golden_artifacts,
    runtime_golden_drift,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _build_and_install_clean_wheel(tmp_path: Path) -> tuple[Path, Path]:
    wheelhouse = tmp_path / "wheelhouse"
    build_environment = os.environ.copy()
    build_environment.pop("AINATIVE_FACTORY_BUILD_SOURCE_COMMIT", None)
    build_environment.pop("AINATIVE_FACTORY_BUILD_SOURCE_TAG", None)
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheelhouse)],
        cwd=REPOSITORY_ROOT,
        env=build_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    wheels = tuple(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1

    environment_root = tmp_path / "installed"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    scripts = environment_root / ("Scripts" if os.name == "nt" else "bin")
    environment_python = scripts / ("python.exe" if os.name == "nt" else "python")

    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    return environment_root, environment_python


def test_clean_wheel_installs_dependencies_and_matches_runtime_goldens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment_root, environment_python = _build_and_install_clean_wheel(tmp_path)
    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    inspection = subprocess.run(
        [
            str(environment_python),
            "-I",
            "-c",
            (
                "import json, sys; "
                "import ai_native, jsonschema, playwright, pydantic, rfc8785, yaml; "
                "print(json.dumps({'prefix': sys.prefix, 'path': sys.path, "
                "'modules': [ai_native.__file__, jsonschema.__file__, "
                "playwright.__file__, pydantic.__file__, rfc8785.__file__, "
                "yaml.__file__]}))"
            ),
        ],
        cwd=outside_checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert inspection.returncode == 0, inspection.stderr
    inspected = json.loads(inspection.stdout)
    assert Path(inspected["prefix"]).resolve() == environment_root.resolve()
    assert all(
        Path(module_path).resolve().is_relative_to(environment_root.resolve())
        for module_path in inspected["modules"]
    )
    assert all(
        str(REPOSITORY_ROOT.resolve()) not in entry for entry in inspected["path"]
    )

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    rendered = render_runtime_golden_artifacts(python_executable=environment_python)

    assert runtime_golden_drift(DEFAULT_OUTPUT_DIR, expected=rendered) == ()

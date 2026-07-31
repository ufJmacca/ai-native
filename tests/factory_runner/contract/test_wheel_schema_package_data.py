from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from tests.factory_runner.contract._schema_support import (
    CONTRACT_CASES,
    EXPECTED_SCHEMA_ARTIFACT_FILENAMES,
    REPOSITORY_ROOT,
)


def test_built_wheel_exposes_exact_schema_package_data_outside_checkout(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheelhouse)],
        cwd=REPOSITORY_ROOT,
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
    isolated_python = scripts / ("python.exe" if os.name == "nt" else "python")

    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(isolated_python),
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
    script = """
from importlib import import_module, resources
import json
import ai_native
protocol = import_module("ai_native.factory_runner.protocol")
schema_root = (
    resources.files("ai_native")
    .joinpath("schemas")
    .joinpath("factory_runner")
    .joinpath("v1")
)
names = sorted(path.name for path in schema_root.iterdir() if path.is_file())
drafts = {
    schema: protocol.load_contract_schema(schema)["$schema"]
    for schema in protocol.iter_contract_schemas()
}
print(json.dumps({
    "package_file": ai_native.__file__,
    "protocol_file": protocol.__file__,
    "schema_filenames": names,
    "schema_drafts": drafts,
    "schema_set_digest": protocol.schema_set_digest(),
}))
"""
    imported = subprocess.run(
        [str(isolated_python), "-I", "-c", script],
        cwd=outside_checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)

    assert str(environment_root) in result["package_file"]
    assert str(environment_root) in result["protocol_file"]
    assert str(REPOSITORY_ROOT) not in result["package_file"]
    assert str(REPOSITORY_ROOT) not in result["protocol_file"]
    assert tuple(result["schema_filenames"]) == EXPECTED_SCHEMA_ARTIFACT_FILENAMES
    assert tuple(sorted(result["schema_drafts"])) == tuple(
        sorted(case.schema_name for case in CONTRACT_CASES)
    )
    assert set(result["schema_drafts"].values()) == {
        "https://json-schema.org/draft/2020-12/schema"
    }
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        result["schema_set_digest"],
    )

from __future__ import annotations

import json
from pathlib import Path


def test_phase_manifest_preserves_serial_an_dependency_order() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    manifest_path = repository_root / "docs" / "factory-runner" / "phase-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    phases = manifest["phases"]
    assert [phase["id"] for phase in phases] == [
        "AN-00",
        "AN-01",
        "AN-02",
        "AN-03",
        "AN-04",
    ]
    assert [phase["depends_on"] for phase in phases] == [
        [],
        ["AN-00"],
        ["AN-01"],
        ["AN-02"],
        ["AN-03"],
    ]
    assert manifest["protocol"] == "factory-runner-protocol/v1"


def test_phase_manifest_completion_gates_require_protected_automation() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    manifest_path = repository_root / "docs" / "factory-runner" / "phase-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for phase in manifest["phases"]:
        gate = phase["completion_gate"].casefold()
        assert "human" not in gate, phase["id"]
        assert "manual" not in gate, phase["id"]
        assert "required automated checks" in gate, phase["id"]
        assert "machine-verifiable phase evidence" in gate, phase["id"]
        assert "trusted publisher" in gate, phase["id"]
        assert "protected github auto-merge" in gate, phase["id"]
        assert "exact default-branch head" in gate, phase["id"]

from __future__ import annotations

import json
from pathlib import Path
import re

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RELEASE_PLEASE_CONFIG = REPOSITORY_ROOT / "release-please-config.json"
RELEASE_PLEASE_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "release-please.yml"
)
FACTORY_RELEASE_WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "factory-runner-release.yml"
)
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_please_creates_a_draft_and_arms_protected_auto_merge() -> None:
    config = json.loads(RELEASE_PLEASE_CONFIG.read_text(encoding="utf-8"))
    package = config["packages"]["."]
    assert package["draft"] is True
    assert package["force-tag-creation"] is True
    assert package.get("draft-pull-request", False) is False

    workflow = _workflow(RELEASE_PLEASE_WORKFLOW)
    job = workflow["jobs"]["release-please"]
    release_step = next(
        step for step in job["steps"] if step.get("id") == "release"
    )
    assert release_step["uses"] == (
        "googleapis/release-please-action@"
        "5c625bfb5d1ff62eadeeb3772007f7f66fdcf071"
    )
    auto_merge = next(
        step
        for step in job["steps"]
        if step.get("name") == "Enable protected release PR auto-merge"
    )
    assert auto_merge["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_PLEASE_TOKEN }}"
    command = auto_merge["run"]
    assert "gh pr merge" in command
    assert "--auto" in command
    assert "--merge" in command
    assert "--admin" not in command

    release_job = workflow["jobs"]["factory-runner-release"]
    assert release_job["needs"] == "release-please"
    assert "release_created" in release_job["if"]
    assert release_job["uses"] == (
        "./.github/workflows/factory-runner-release.yml"
    )
    assert "secrets" not in release_job
    assert release_job["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }


def test_release_workflow_is_automatic_atomic_and_uses_immutable_actions() -> None:
    workflow = _workflow(FACTORY_RELEASE_WORKFLOW)
    assert "workflow_call" in workflow["on"]
    assert "workflow_dispatch" not in workflow["on"]
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert {
        "tag_name",
        "version",
        "source_sha",
        "upload_url",
        "html_url",
    }.issubset(inputs)
    assert all(value["required"] == "true" for value in inputs.values())

    permissions = workflow["permissions"]
    assert permissions["contents"] == "write"
    assert permissions["packages"] == "write"
    assert permissions["id-token"] == "write"
    assert permissions["attestations"] == "write"
    assert permissions["artifact-metadata"] == "write"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"

    rendered = FACTORY_RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "environment:" not in rendered
    assert "required-reviewer" not in rendered.casefold()
    assert "--clobber" not in rendered
    assert ":latest" not in rendered.casefold()

    steps = workflow["jobs"]["release"]["steps"]
    names = [step.get("name") for step in steps]
    required_order = [
        "Verify immutable release identity",
        "Build release wheel once",
        "Build and push immutable OCI image",
        "Run receipt-resolved compatibility suite",
        "Generate image SBOM",
        "Run blocking vulnerability policy",
        "Attest wheel provenance",
        "Attest image provenance",
        "Attest image SBOM",
        "Build and verify release receipt",
        "Attest release receipt",
        "Upload immutable draft release assets",
        "Verify draft assets from a clean directory",
        "Publish verified release atomically",
    ]
    assert [names.index(name) for name in required_order] == sorted(
        names.index(name) for name in required_order
    )

    for step in steps:
        uses = step.get("uses")
        if uses is None or uses.startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), uses

    trivy = next(
        step
        for step in steps
        if step.get("name") == "Run blocking vulnerability policy"
    )
    assert trivy["with"]["version"] == "v0.70.0"
    assert trivy["with"]["exit-code"] == "1"
    assert trivy["with"]["severity"] == "HIGH,CRITICAL"
    assert trivy["with"]["ignore-unfixed"] == "true"

    attestations = [
        step for step in steps if str(step.get("name", "")).startswith("Attest ")
    ]
    assert len(attestations) >= 4
    assert all(step["uses"].startswith("actions/attest@") for step in attestations)


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
    references: list[tuple[Path, str]] = []
    for workflow_path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        for match in re.finditer(
            r"(?m)^\s*uses:\s*([^\s#]+)\s*(?:#.*)?$",
            workflow_path.read_text(encoding="utf-8"),
        ):
            reference = match.group(1)
            if reference.startswith("./"):
                continue
            references.append((workflow_path, reference))

    assert references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference)
        for _path, reference in references
    ), references

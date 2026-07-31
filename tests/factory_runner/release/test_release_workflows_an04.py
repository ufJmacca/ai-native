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
    REPOSITORY_ROOT / ".github" / "workflows" / "factory-runner-release.yml"
)
RELEASE_PR_SYNC_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "release-pr-uv-lock.yml"
)
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_please_delegates_merge_to_the_trusted_cli_controller() -> None:
    config = json.loads(RELEASE_PLEASE_CONFIG.read_text(encoding="utf-8"))
    package = config["packages"]["."]
    assert package["draft"] is True
    assert package["force-tag-creation"] is True
    assert package.get("draft-pull-request", False) is False

    workflow = _workflow(RELEASE_PLEASE_WORKFLOW)
    job = workflow["jobs"]["release-please"]
    release_step = next(step for step in job["steps"] if step.get("id") == "release")
    assert release_step["uses"] == (
        "googleapis/release-please-action@5c625bfb5d1ff62eadeeb3772007f7f66fdcf071"
    )
    rendered_job = json.dumps(job)
    assert "Enable protected release PR auto-merge" not in rendered_job
    assert "gh pr merge" not in rendered_job
    assert "--auto" not in rendered_job
    assert "/pulls/" not in rendered_job

    release_job = workflow["jobs"]["factory-runner-release"]
    assert release_job["needs"] == "release-please"
    assert "release_created" in release_job["if"]
    assert release_job["uses"] == ("./.github/workflows/factory-runner-release.yml")
    assert "secrets" not in release_job
    assert release_job["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }


def test_release_pr_generation_is_automatic_and_write_token_isolated() -> None:
    workflow = _workflow(RELEASE_PR_SYNC_WORKFLOW)
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }

    generator = workflow["jobs"]["generate-release-files"]
    assert generator["permissions"]["contents"] == "read"
    assert "RELEASE_PLEASE_TOKEN" not in json.dumps(generator)
    checkout = next(
        step for step in generator["steps"] if step.get("name") == "Check out release PR"
    )
    assert checkout["with"]["ref"] == (
        "${{ github.event.pull_request.head.sha }}"
    )
    assert checkout["with"]["persist-credentials"] == "false"
    generator_commands = "\n".join(
        step.get("run", "") for step in generator["steps"]
    )
    assert "uv lock" in generator_commands
    assert (
        "scripts/generate_factory_runner_goldens.py --write"
        in generator_commands
    )
    assert "uv.lock" in generator_commands
    assert "tests/fixtures/factory_runner/runtime-golden/" in generator_commands
    upload = next(
        step for step in generator["steps"] if step.get("name") == "Upload patch"
    )
    assert upload["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )

    publisher = workflow["jobs"]["sync-uv-lock"]
    assert publisher["needs"] == "generate-release-files"
    assert publisher["permissions"] == {
        "contents": "write",
        "pull-requests": "read",
    }
    publisher_rendered = json.dumps(publisher)
    assert "RELEASE_PLEASE_TOKEN" in publisher_rendered
    assert "uv run" not in publisher_rendered
    assert "python " not in publisher_rendered
    download = next(
        step for step in publisher["steps"] if step.get("name") == "Download patch"
    )
    assert download["uses"] == (
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    )
    publisher_commands = "\n".join(
        step.get("run", "") for step in publisher["steps"]
    )
    assert "git apply --check" in publisher_commands
    assert "git apply --index" in publisher_commands
    assert "uv.lock" in publisher_commands
    assert "tests/fixtures/factory_runner/runtime-golden/" in publisher_commands
    assert "--force-with-lease" in publisher_commands


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

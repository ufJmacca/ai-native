from __future__ import annotations

from pathlib import Path
import re

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "factory-runner-ci.yml"
SOURCE_COMMIT_EXPRESSION = "${{ github.event.pull_request.head.sha || github.sha }}"
IMMUTABLE_IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def _workflow() -> dict:
    return yaml.load(
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_factory_runner_ci_exposes_one_stable_blocking_check() -> None:
    workflow = _workflow()

    assert workflow["name"] == "Factory Runner CI"
    assert set(workflow["jobs"]) == {"factory-runner"}
    job = workflow["jobs"]["factory-runner"]
    assert job["name"] == "factory-runner-ci"
    assert job["runs-on"] == "ubuntu-latest"
    assert workflow["permissions"] == {"contents": "read"}

    triggers = workflow["on"]
    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"]["branches"] == ["main"]
    required_paths = {
        ".dockerignore",
        ".github/workflows/factory-runner-ci.yml",
        ".github/workflows/factory-runner-release.yml",
        ".github/workflows/release-please.yml",
        ".github/workflows/release-pr-uv-lock.yml",
        ".release-please-manifest.json",
        "CHANGELOG.md",
        "Makefile",
        "ai_native/**",
        "docs/factory-runner/**",
        "hatch_build.py",
        "images/factory-runner/**",
        "pyproject.toml",
        "release-please-config.json",
        "scripts/**",
        "tests/factory_runner/**",
        "uv.lock",
    }
    assert required_paths.issubset(set(triggers["pull_request"]["paths"]))
    assert required_paths.issubset(set(triggers["push"]["paths"]))

    rendered = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "workflow_dispatch" not in rendered
    assert "environment:" not in rendered
    assert "required-reviewer" not in rendered.casefold()
    assert "docker/login-action" not in rendered
    assert "actions/attest" not in rendered
    assert "gh release" not in rendered
    assert "push: true" not in rendered
    assert "packages: write" not in rendered
    assert "id-token: write" not in rendered


def test_factory_runner_ci_builds_one_exact_wheel_and_loaded_image() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["factory-runner"]
    env = workflow["env"]

    assert env["SOURCE_COMMIT"] == SOURCE_COMMIT_EXPRESSION
    assert IMMUTABLE_IMAGE_PATTERN.fullmatch(env["PYTHON_BASE_IMAGE"])
    assert IMMUTABLE_IMAGE_PATTERN.fullmatch(env["UV_BASE_IMAGE"])
    assert env["LOCAL_IMAGE"] == (
        "ai-native-factory-runner:ci-"
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )

    checkout = _step(job, "Check out the exact branch commit")
    assert checkout["with"]["ref"] == SOURCE_COMMIT_EXPRESSION
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["persist-credentials"] == "false"

    wheel = _step(job, "Build the exact branch wheel once")
    assert wheel["env"]["AINATIVE_FACTORY_BUILD_SOURCE_COMMIT"] == (
        "${{ env.SOURCE_COMMIT }}"
    )
    assert "AINATIVE_FACTORY_BUILD_SOURCE_TAG" not in wheel.get("env", {})
    assert wheel["run"].count("uv build --wheel") == 1
    assert "test ! -e dist" in wheel["run"]
    assert 'test "${#wheels[@]}" -eq 1' in wheel["run"]
    assert "sha256sum" in wheel["run"]

    image = _step(job, "Build and load the hardened OCI image")
    assert image["uses"] == (
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
    )
    assert image["with"]["load"] == "true"
    assert image["with"]["push"] == "false"
    assert image["with"]["provenance"] == "false"
    assert image["with"]["sbom"] == "false"
    assert image["with"]["tags"] == "${{ env.LOCAL_IMAGE }}"
    build_args = image["with"]["build-args"]
    for argument in (
        "PYTHON_BASE_IMAGE=${{ env.PYTHON_BASE_IMAGE }}",
        "UV_BASE_IMAGE=${{ env.UV_BASE_IMAGE }}",
        "FACTORY_RUNNER_CREATED=${{ steps.wheel.outputs.created }}",
        "FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256="
        "${{ steps.wheel.outputs.schema_manifest_sha }}",
        "FACTORY_RUNNER_SCHEMA_SET_DIGEST=${{ steps.wheel.outputs.schema_set }}",
        "FACTORY_RUNNER_SOURCE_COMMIT=${{ env.SOURCE_COMMIT }}",
        "FACTORY_RUNNER_VERSION=${{ steps.wheel.outputs.version }}",
        "FACTORY_RUNNER_WHEEL_SHA256=${{ steps.wheel.outputs.sha256 }}",
    ):
        assert argument in build_args

    for step in job["steps"]:
        uses = step.get("uses")
        if uses is not None:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), uses


def test_factory_runner_ci_runs_drift_release_and_cross_artifact_gates() -> None:
    job = _workflow()["jobs"]["factory-runner"]

    drift = _step(job, "Reject certification schema drift")
    assert (
        "scripts/generate_factory_runner_certification_schemas.py --check"
        in drift["run"]
    )

    focused = _step(job, "Run focused release tests")
    assert "pytest tests/factory_runner/release" in focused["run"]
    assert focused["env"]["AINATIVE_FACTORY_RUNNER_TEST_IMAGE"] == (
        "${{ env.LOCAL_IMAGE }}"
    )

    compatibility = _step(
        job,
        "Certify source wheel and OCI mandatory fixtures",
    )
    command = compatibility["run"]
    assert "scripts/run_factory_runner_compatibility.py" in command
    assert "docker image inspect --format '{{.Id}}' \"${LOCAL_IMAGE}\"" in command
    assert "steps.image.outputs.digest" not in command
    assert '--source-commit "${SOURCE_COMMIT}"' in command
    assert '--wheel "${WHEEL_PATH}"' in command
    assert '--oci-image "${IMAGE_REFERENCE}"' in command
    assert '--oci-runtime-image "${LOCAL_IMAGE}"' in command
    assert "--generated-at" in command
    assert "--work-dir" in command
    assert "--output" in command

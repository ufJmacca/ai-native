from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from ai_native.factory_runner.protocol import (
    schema_manifest_digest,
    schema_set_digest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "images" / "factory-runner" / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
IMAGE_ENVIRONMENT_KEY = "AINATIVE_FACTORY_RUNNER_TEST_IMAGE"
EXPECTED_DOCKERIGNORE = (
    "**",
    "!dist/",
    "!dist/*.whl",
    "!images/",
    "!images/factory-runner/",
    "!images/factory-runner/Dockerfile",
    "!pyproject.toml",
    "!uv.lock",
)
EXPECTED_LABEL_KEYS = {
    "io.ai-native.factory-runner.distribution",
    "io.ai-native.factory-runner.protocol",
    "io.ai-native.factory-runner.schema-manifest-sha256",
    "io.ai-native.factory-runner.schema-set-digest",
    "io.ai-native.factory-runner.wheel-sha256",
    "org.opencontainers.image.base.name",
    "org.opencontainers.image.created",
    "org.opencontainers.image.description",
    "org.opencontainers.image.ref.name",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.source",
    "org.opencontainers.image.title",
    "org.opencontainers.image.version",
}
SAFE_ENVIRONMENT = {
    "HOME": "/home/ainative",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONUNBUFFERED": "1",
    "TMPDIR": "/run/ainative",
}


def _logical_dockerfile_lines(content: str) -> tuple[str, ...]:
    logical: list[str] = []
    pending = ""
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    assert not pending
    return tuple(logical)


def _image_inspect(image: str) -> dict[str, object]:
    completed = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    assert isinstance(payload[0], dict)
    return payload[0]


def _config_environment(config: dict[str, object]) -> dict[str, str]:
    values = config.get("Env")
    assert isinstance(values, list)
    parsed: dict[str, str] = {}
    for value in values:
        assert isinstance(value, str) and "=" in value
        key, content = value.split("=", 1)
        parsed[key] = content
    return parsed


def test_docker_build_context_is_a_deny_all_wheel_allowlist() -> None:
    rules = tuple(
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert rules == EXPECTED_DOCKERIGNORE
    assert not any(
        forbidden in rules
        for forbidden in (
            "!.",
            "!ai_native/",
            "!tests/",
            "!services/",
            "!.git/",
            "!.env",
        )
    )


def test_runner_dockerfile_copies_only_release_inputs_and_built_runtime() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    lines = _logical_dockerfile_lines(content)
    copies = tuple(line for line in lines if line.upper().startswith("COPY "))

    assert copies == (
        "COPY --from=uv-source /uv /usr/local/bin/uv",
        "COPY pyproject.toml uv.lock /build/",
        "COPY dist/*.whl /wheelhouse/",
        "COPY --from=builder /opt/ainative /opt/ainative",
    )
    assert "COPY ." not in content
    assert not re.search(
        r"(?im)^COPY\s+(?:--[^\s]+\s+)*(?:ai_native|tests|services|docs|\\.git|\\.env)",
        content,
    )


def test_runner_dockerfile_enforces_wheel_and_identity_inputs() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")

    for argument in (
        "PYTHON_BASE_IMAGE",
        "UV_BASE_IMAGE",
        "FACTORY_RUNNER_CREATED",
        "FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256",
        "FACTORY_RUNNER_SCHEMA_SET_DIGEST",
        "FACTORY_RUNNER_SOURCE_COMMIT",
        "FACTORY_RUNNER_SOURCE_TAG",
        "FACTORY_RUNNER_VERSION",
        "FACTORY_RUNNER_WHEEL_SHA256",
    ):
        assert re.search(rf"(?m)^ARG {argument}$", content)
    assert "FROM ${UV_BASE_IMAGE} AS uv-source" in content
    assert content.count("FROM ${PYTHON_BASE_IMAGE}") == 2
    assert 'find /wheelhouse -maxdepth 1 -type f -name "*.whl"' in content
    assert "wheel_count" in content
    assert "sha256sum" in content
    assert "load_build_identity" in content
    assert "uv export --frozen --no-dev --no-emit-project" in content
    assert "uv pip install" in content
    assert "--no-deps /wheelhouse/*.whl" in content


def test_runner_dockerfile_has_minimal_fixed_runtime_contract() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    lines = _logical_dockerfile_lines(content)

    apt_lines = tuple(line for line in lines if "apt-get install" in line)
    assert len(apt_lines) == 1
    assert "ca-certificates git" in apt_lines[0]
    for prohibited in (
        "curl",
        "docker",
        "gh ",
        "node",
        "npm",
        "openssh",
        "playwright install",
        "sudo",
        "wget",
    ):
        assert prohibited not in apt_lines[0].casefold()

    assert "USER 10001:10001" in lines
    assert "TMPDIR=/run/ainative" in content
    assert "PYTHONDONTWRITEBYTECODE=1" in content
    assert "PYTHONNOUSERSITE=1" in content
    assert "PYTHONUNBUFFERED=1" in content
    assert 'ENTRYPOINT ["/opt/ainative/bin/ainative", "factory"]' in lines
    assert 'CMD ["--help"]' in lines
    assert not re.search(r"(?im)^(VOLUME|EXPOSE|HEALTHCHECK)\b", content)


def test_runner_dockerfile_declares_complete_immutable_image_labels() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")

    for label in EXPECTED_LABEL_KEYS:
        assert f"{label}=" in content
    assert (
        'io.ai-native.factory-runner.protocol="factory-runner-protocol/v1"' in content
    )
    assert 'io.ai-native.factory-runner.distribution="ai-native-base"' in content


@pytest.mark.skipif(
    not os.environ.get(IMAGE_ENVIRONMENT_KEY),
    reason=f"{IMAGE_ENVIRONMENT_KEY} is required for the host-Docker image smoke",
)
def test_built_runner_image_has_the_fixed_config_and_packaged_schema_identity() -> None:
    image = os.environ[IMAGE_ENVIRONMENT_KEY]
    inspected = _image_inspect(image)
    config = inspected.get("Config")
    assert isinstance(config, dict)

    assert config.get("User") == "10001:10001"
    assert config.get("Entrypoint") == [
        "/opt/ainative/bin/ainative",
        "factory",
    ]
    assert config.get("Cmd") == ["--help"]
    assert config.get("WorkingDir") == "/workspace"
    assert not config.get("Volumes")
    assert not config.get("ExposedPorts")

    environment = _config_environment(config)
    assert environment.items() >= SAFE_ENVIRONMENT.items()
    assert not any(
        marker in key.upper()
        for key in environment
        for marker in ("PASSWORD", "SECRET", "TOKEN")
    )

    labels = config.get("Labels")
    assert isinstance(labels, dict)
    assert EXPECTED_LABEL_KEYS <= labels.keys()
    assert (
        labels["io.ai-native.factory-runner.schema-set-digest"] == schema_set_digest()
    )
    assert (
        labels["io.ai-native.factory-runner.schema-manifest-sha256"]
        == schema_manifest_digest()
    )
    assert re.fullmatch(
        r"[0-9a-f]{40}",
        labels["org.opencontainers.image.revision"],
    )
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        labels["io.ai-native.factory-runner.wheel-sha256"],
    )
    assert "@sha256:" in labels["org.opencontainers.image.base.name"]

    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/run/ainative:rw,noexec,nosuid,nodev,size=64m,mode=0700,"
            "uid=10001,gid=10001",
            image,
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "factory-runner-protocol/v1" in completed.stdout
    assert "{run,verify}" in completed.stdout

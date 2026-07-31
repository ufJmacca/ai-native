from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Mapping, Sequence

import pytest

from ai_native import __version__
from ai_native.factory_runner.build_identity import (
    BUILD_IDENTITY_SCHEMA,
    FactoryRunnerBuildIdentity,
)
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.compatibility_report import (
    CERTIFIED_ARTIFACT_ORDER,
    CERTIFIED_FIXTURE_ORDER,
    validate_compatibility_report,
)
from ai_native.factory_runner.protocol import (
    contract_document_digest,
    schema_manifest_digest,
    schema_set_digest,
)
from scripts.run_factory_runner_compatibility import (
    CommandResult,
    CompatibilityInvocation,
    CompatibilityRunner,
    CertificationInputs,
    _OCI_OUTPUT_CLEANUP_PROGRAM,
    _OCI_OUTPUT_HANDOFF_PROGRAM,
    _make_oci_fixture_writable,
    _restore_oci_fixture_cleanup_permissions,
    _resolve_inputs,
)
from tests.factory_runner.contract._support import run_result
from tests.factory_runner.integration._fake_agent import AUTHORED_APP
from tests.factory_runner.integration._support import FAKE_AGENT


SOURCE_COMMIT = "a" * 40
IMAGE_DIGEST = "sha256:" + ("4" * 64)
IMAGE_REFERENCE = "ghcr.io/ufjmacca/ai-native-factory-runner@" + IMAGE_DIGEST
LOCAL_RUNTIME_IMAGE = f"ai-native-factory-runner:ci-{SOURCE_COMMIT}"


def _operation_and_outcome(fixture_id: str) -> tuple[str, str]:
    return {
        "author-success": ("author", "succeeded"),
        "author-no-change": ("author", "no_change"),
        "verify-success": ("verify", "succeeded"),
    }[fixture_id]


class FixtureFactory:
    def __call__(
        self,
        root: Path,
        fixture_id: str,
    ) -> CompatibilityInvocation:
        operation, _outcome = _operation_and_outcome(fixture_id)
        input_dir = root / "input"
        output_dir = root / "output"
        workspace = root / "workspace"
        for directory in (input_dir, output_dir, workspace):
            directory.mkdir(parents=True, exist_ok=True)
        run_spec_path = input_dir / "run-spec.json"
        run_spec_path.write_text(
            json.dumps(
                {
                    "fixture_id": fixture_id,
                    "operation": operation,
                    "outputs": {"output_dir": str(output_dir)},
                }
            ),
            encoding="utf-8",
        )
        fake_agent = root / "compatibility-fake-agent.py"
        fake_agent.write_text("raise SystemExit(97)\n", encoding="utf-8")
        return CompatibilityInvocation(
            fixture_id=fixture_id,
            operation=operation,
            root=root,
            run_spec_path=run_spec_path,
            output_dir=output_dir,
            gateway_command=("python3", str(fake_agent)),
        )


class FakeExecutor:
    def __init__(
        self,
        *,
        wheel_path: Path,
        divergent_artifact: str | None = None,
        local_runtime_image: str | None = None,
        local_image_id: str = IMAGE_DIGEST,
    ) -> None:
        self.wheel_path = wheel_path
        self.divergent_artifact = divergent_artifact
        self.local_runtime_image = local_runtime_image
        self.local_image_id = local_image_id
        self.calls: list[tuple[str, ...]] = []
        self.wheel_identity = FactoryRunnerBuildIdentity(
            schema=BUILD_IDENTITY_SCHEMA,
            distribution="ai-native-base",
            version=__version__,
            source_repository="ufJmacca/ai-native",
            source_commit=SOURCE_COMMIT,
            source_tag=None,
            image=None,
            schema_set_digest=schema_set_digest(),
            schema_manifest_sha256=schema_manifest_digest(),
        )

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, environment, timeout_seconds
        argv = tuple(command)
        self.calls.append(argv)

        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, SOURCE_COMMIT + "\n", "")
        if argv[:2] == ("uv", "venv"):
            environment_root = Path(argv[-1])
            python = environment_root / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            return CommandResult(0, "", "")
        if argv[:3] == ("uv", "pip", "install"):
            assert str(self.wheel_path) in argv
            return CommandResult(0, "", "")
        if len(argv) >= 3 and argv[-2:] == (
            "-c",
            (
                "import json; from ai_native.factory_runner.build_identity "
                "import load_build_identity; print(json.dumps("
                "load_build_identity().model_dump(mode='json', by_alias=True), "
                "sort_keys=True))"
            ),
        ):
            return CommandResult(
                0,
                json.dumps(
                    self.wheel_identity.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                )
                + "\n",
                "",
            )
        if argv[:2] == ("docker", "pull"):
            assert self.local_runtime_image is None
            assert argv[2] == IMAGE_REFERENCE
            return CommandResult(0, "", "")
        if argv[:3] == ("docker", "image", "inspect"):
            assert argv[3] == (self.local_runtime_image or IMAGE_REFERENCE)
            labels = {
                "io.ai-native.factory-runner.distribution": "ai-native-base",
                "io.ai-native.factory-runner.protocol": ("factory-runner-protocol/v1"),
                "io.ai-native.factory-runner.schema-manifest-sha256": (
                    schema_manifest_digest()
                ),
                "io.ai-native.factory-runner.schema-set-digest": (schema_set_digest()),
                "io.ai-native.factory-runner.wheel-sha256": sha256_digest(
                    self.wheel_path.read_bytes()
                ),
                "org.opencontainers.image.revision": SOURCE_COMMIT,
                "org.opencontainers.image.version": __version__,
                "org.opencontainers.image.ref.name": "",
            }
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": self.local_image_id,
                            "RepoDigests": (
                                [] if self.local_runtime_image else [IMAGE_REFERENCE]
                            ),
                            "Config": {
                                "User": "10001:10001",
                                "Entrypoint": [
                                    "/opt/ainative/bin/ainative",
                                    "factory",
                                ],
                                "Labels": labels,
                            },
                        }
                    ]
                ),
                "",
            )
        if (
            _OCI_OUTPUT_HANDOFF_PROGRAM in argv
            or _OCI_OUTPUT_CLEANUP_PROGRAM in argv
        ):
            return CommandResult(0, "", "")
        if "--run-spec" in argv:
            run_spec_path = Path(argv[argv.index("--run-spec") + 1])
            output_dir = Path(argv[argv.index("--output-dir") + 1])
            fixture_id = json.loads(run_spec_path.read_text(encoding="utf-8"))[
                "fixture_id"
            ]
            artifact = (
                "oci"
                if argv[0] == "docker"
                else "wheel"
                if ".compatibility-venv" in argv[0]
                else "source"
            )
            self._write_result(
                output_dir,
                fixture_id,
                divergent=artifact == self.divergent_artifact,
            )
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {argv!r}")

    def _write_result(
        self,
        output_dir: Path,
        fixture_id: str,
        *,
        divergent: bool,
    ) -> None:
        operation, outcome = _operation_and_outcome(fixture_id)
        payload = run_result(operation=operation, outcome=outcome)
        if operation == "verify":
            payload["completed_stages"] = ["verify"]
        payload["runner_build"] = {
            "version": __version__,
            "image": None,
            "source_commit": SOURCE_COMMIT,
        }
        payload["output_manifest_digest"] = "sha256:" + (
            ("f" if divergent else "5") * 64
        )
        payload["result_digest"] = contract_document_digest(payload)
        result_path = output_dir / "result" / "run-result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(canonical_json_bytes(payload))


def _inputs(
    tmp_path: Path,
    wheel_path: Path,
    *,
    local_runtime_image: str | None = None,
) -> CertificationInputs:
    values = dict(
        repository_root=Path(__file__).resolve().parents[3],
        source_python=Path("/usr/local/bin/python"),
        source_commit=SOURCE_COMMIT,
        wheel_path=wheel_path,
        oci_image=IMAGE_REFERENCE,
        generated_at="2026-07-31T06:00:00Z",
        work_dir=tmp_path / "work",
    )
    if local_runtime_image is not None:
        values["oci_runtime_image"] = local_runtime_image
    return CertificationInputs(**values)


def test_oci_fixture_keeps_git_security_metadata_read_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invocation"
    git_dir = root / "workspace" / ".git"
    git_dir.mkdir(parents=True)
    index = git_dir / "index"
    index.write_bytes(b"fixture-index")
    authored_file = root / "workspace" / "app.py"
    authored_file.write_text("greeting = 'before'\n", encoding="utf-8")

    _make_oci_fixture_writable(root)

    assert root.stat().st_mode & 0o002
    assert authored_file.stat().st_mode & 0o002
    assert git_dir.stat().st_mode & 0o222 == 0
    assert index.stat().st_mode & 0o222 == 0

    _restore_oci_fixture_cleanup_permissions(root)

    assert stat.S_IMODE(git_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_compatibility_agent_replaces_foreign_writable_file_with_supported_mode(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("greeting = 'before'\n", encoding="utf-8")
    target.chmod(0o666)
    original_inode = target.stat().st_ino
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Implement the deterministic fixture.\n", encoding="utf-8")
    output = tmp_path / "response.md"
    environment = {
        **os.environ,
        "AINATIVE_OUTPUT_FILE": str(output),
        "AINATIVE_PROMPT_FILE": str(prompt),
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(FAKE_AGENT),
            "--mode",
            "author",
            "--marker",
            str(tmp_path / "agent-calls.log"),
        ],
        cwd=workspace,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert target.read_text(encoding="utf-8") == AUTHORED_APP
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.stat().st_ino != original_inode
    assert not (workspace / ".app.py.factory-agent.tmp").exists()


def test_runner_executes_every_mandatory_fixture_against_exact_artifacts(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    executor = FakeExecutor(wheel_path=wheel_path)
    runner = CompatibilityRunner(
        inputs=_inputs(tmp_path, wheel_path),
        executor=executor,
        fixture_factory=FixtureFactory(),
    )

    report = validate_compatibility_report(
        runner.run().model_dump(mode="json", by_alias=True)
    )

    assert tuple(item.kind for item in report.artifacts) == (CERTIFIED_ARTIFACT_ORDER)
    assert tuple(item.fixture_id for item in report.fixtures) == (
        CERTIFIED_FIXTURE_ORDER
    )
    assert report.artifacts[1].digest == sha256_digest(b"exact-wheel")
    assert report.artifacts[2].reference == IMAGE_REFERENCE
    assert report.artifacts[2].build_identity.image == IMAGE_REFERENCE
    assert all(
        len({result.output_tree_digest for result in fixture.results}) == 1
        for fixture in report.fixtures
    )

    execution_calls = [call for call in executor.calls if "--run-spec" in call]
    assert len(execution_calls) == 9
    assert sum(call[0] == "docker" for call in execution_calls) == 3
    oci_calls = [
        call for call in executor.calls if call[:2] == ("docker", "run")
    ]
    assert len(oci_calls) == 9
    assert sum(_OCI_OUTPUT_HANDOFF_PROGRAM in call for call in oci_calls) == 3
    assert sum(_OCI_OUTPUT_CLEANUP_PROGRAM in call for call in oci_calls) == 3
    for call in oci_calls:
        assert "--read-only" in call
        assert ("--network", "none") == call[
            call.index("--network") : call.index("--network") + 2
        ]
        assert ("--user", "10001:10001") == call[
            call.index("--user") : call.index("--user") + 2
        ]
        assert ("--cap-drop", "ALL") == call[
            call.index("--cap-drop") : call.index("--cap-drop") + 2
        ]
        assert ("--security-opt", "no-new-privileges") == call[
            call.index("--security-opt") : call.index("--security-opt") + 2
        ]
    assert any(call[:3] == ("uv", "pip", "install") for call in executor.calls)
    assert ("docker", "pull", IMAGE_REFERENCE) in executor.calls


def test_runner_can_execute_an_explicit_local_ci_image_without_weakening_report(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    executor = FakeExecutor(
        wheel_path=wheel_path,
        local_runtime_image=LOCAL_RUNTIME_IMAGE,
    )

    report = CompatibilityRunner(
        inputs=_inputs(
            tmp_path,
            wheel_path,
            local_runtime_image=LOCAL_RUNTIME_IMAGE,
        ),
        executor=executor,
        fixture_factory=FixtureFactory(),
    ).run()

    assert report.artifacts[2].reference == IMAGE_REFERENCE
    assert report.artifacts[2].digest == IMAGE_DIGEST
    assert not any(call[:2] == ("docker", "pull") for call in executor.calls)
    assert (
        "docker",
        "image",
        "inspect",
        LOCAL_RUNTIME_IMAGE,
    ) in executor.calls
    oci_calls = [
        call
        for call in executor.calls
        if call[:2] == ("docker", "run") and "--run-spec" in call
    ]
    assert len(oci_calls) == 3
    assert all(LOCAL_RUNTIME_IMAGE in call for call in oci_calls)


def test_runner_rejects_a_local_ci_tag_for_a_different_image_id(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    executor = FakeExecutor(
        wheel_path=wheel_path,
        local_runtime_image=LOCAL_RUNTIME_IMAGE,
        local_image_id="sha256:" + ("5" * 64),
    )

    with pytest.raises(
        RuntimeError,
        match="OCI image config does not match",
    ):
        CompatibilityRunner(
            inputs=_inputs(
                tmp_path,
                wheel_path,
                local_runtime_image=LOCAL_RUNTIME_IMAGE,
            ),
            executor=executor,
            fixture_factory=FixtureFactory(),
        ).run()

    assert not any("--run-spec" in call for call in executor.calls)


def test_runner_fails_closed_on_mutable_image_reference(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    inputs = _inputs(tmp_path, wheel_path)
    inputs = inputs.model_copy(
        update={"oci_image": ("ghcr.io/ufjmacca/ai-native-factory-runner:latest")}
    )
    executor = FakeExecutor(wheel_path=wheel_path)

    with pytest.raises(ValueError, match="digest-pinned"):
        CompatibilityRunner(
            inputs=inputs,
            executor=executor,
            fixture_factory=FixtureFactory(),
        ).run()
    assert executor.calls == []


def test_runner_rejects_cross_artifact_output_drift(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    executor = FakeExecutor(
        wheel_path=wheel_path,
        divergent_artifact="oci",
    )

    with pytest.raises(ValueError, match="equivalent"):
        CompatibilityRunner(
            inputs=_inputs(tmp_path, wheel_path),
            executor=executor,
            fixture_factory=FixtureFactory(),
        ).run()


def test_cli_input_resolution_preserves_the_selected_python_environment(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / f"ai_native_base-{__version__}-py3-none-any.whl"
    wheel_path.write_bytes(b"exact-wheel")
    base_python = tmp_path / "base-python"
    base_python.write_text("", encoding="utf-8")
    environment_python = tmp_path / "venv" / "bin" / "python"
    environment_python.parent.mkdir(parents=True)
    environment_python.symlink_to(base_python)
    arguments = argparse.Namespace(
        receipt=None,
        source_commit=SOURCE_COMMIT,
        oci_image=IMAGE_REFERENCE,
        oci_runtime_image=None,
        repository_root=Path(__file__).resolve().parents[3],
        source_python=environment_python,
        wheel=wheel_path,
        generated_at="2026-07-31T06:00:00Z",
        work_dir=tmp_path / "work",
        uv_command="uv",
        docker_command="docker",
    )

    inputs, receipt = _resolve_inputs(arguments)

    assert receipt is None
    assert inputs.source_python == environment_python
    assert inputs.source_python.is_symlink()

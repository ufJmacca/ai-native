from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Iterator, Literal, Protocol

from pydantic import model_validator

from ai_native.factory_runner.build_identity import (
    FACTORY_RUNNER_DISTRIBUTION,
    FACTORY_RUNNER_SOURCE_REPOSITORY,
    FactoryRunnerBuildIdentity,
    load_build_identity,
)
from ai_native.factory_runner.canonical import canonical_json_bytes, sha256_digest
from ai_native.factory_runner.compatibility_report import (
    CERTIFIED_ARTIFACT_ORDER,
    CERTIFIED_FIXTURE_ORDER,
    COMPATIBILITY_REPORT_SCHEMA,
    COMPATIBILITY_SUITE_VERSION,
    ArtifactFixtureResult,
    CertifiedArtifact,
    FactoryRunnerCompatibilityReport,
    FixtureCertification,
    canonical_compatibility_report_bytes,
    compatibility_report_digest,
)
from ai_native.factory_runner.contracts.common import (
    GitCommitSha,
    StrictContractModel,
    UtcTimestamp,
)
from ai_native.factory_runner.contracts.run_result import RunResult
from ai_native.factory_runner.protocol import (
    validate_contract,
    verify_contract_digest,
)
from ai_native.factory_runner.release_receipt import (
    FactoryRunnerReleaseReceipt,
    validate_release_receipt,
)


_IMAGE_PATTERN = re.compile(
    r"^ghcr\.io/ufjmacca/ai-native-factory-runner@"
    r"sha256:[0-9a-f]{64}$"
)
_BUILD_IDENTITY_QUERY = (
    "import json; from ai_native.factory_runner.build_identity "
    "import load_build_identity; print(json.dumps("
    "load_build_identity().model_dump(mode='json', by_alias=True), "
    "sort_keys=True))"
)
_EXECUTION_TIMEOUT_SECONDS = 180
_FIXTURE_EXPECTATIONS: dict[
    str,
    tuple[Literal["author", "verify"], Literal["succeeded", "no_change"]],
] = {
    "author-success": ("author", "succeeded"),
    "author-no-change": ("author", "no_change"),
    "verify-success": ("verify", "succeeded"),
}
_COMPATIBILITY_ENTRYPOINT = """\
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from types import SimpleNamespace

from ai_native.factory_runner.build_identity import FactoryRunnerBuildIdentity
import ai_native.factory_runner.build_identity as build_identity


class _CompatibilityDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 7, 31, tzinfo=UTC)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


def _timestamp():
    return "2026-07-31T00:00:00.000000Z"


def _legacy_timestamp():
    return "2026-07-31T00:00:00+00:00"


identity = FactoryRunnerBuildIdentity.model_validate(
    json.loads(os.environ["AINATIVE_COMPATIBILITY_BUILD_IDENTITY_JSON"])
)


def _identity(path=None):
    if path is not None:
        raise ValueError("compatibility entrypoint does not accept identity paths")
    return identity


build_identity.load_build_identity = _identity

from ai_native import models, state, utils
from ai_native.factory_runner import author, changes, outputs, runner, verification

changes.load_build_identity = _identity
outputs.load_build_identity = _identity
verification.load_build_identity = _identity
outputs.utc_timestamp = _timestamp
runner.utc_timestamp = _timestamp
verification.utc_timestamp = _timestamp
changes.utc_timestamp = _timestamp
author.utc_now = _legacy_timestamp
state.utc_now = _legacy_timestamp
utils.utc_now = _legacy_timestamp
models.datetime = _CompatibilityDatetime
state.datetime = _CompatibilityDatetime
runner.time = SimpleNamespace(monotonic=lambda: 1_000.0)
verification.time = SimpleNamespace(monotonic=lambda: 1_000.0)

from ai_native.cli import main

raise SystemExit(main())
"""


class CompatibilityExecutionError(RuntimeError):
    """One artifact could not complete deterministic certification."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult: ...


class SubprocessCommandExecutor:
    """Production executor; every compatibility command is unattended."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class CompatibilityInvocation:
    fixture_id: str
    operation: Literal["author", "verify"]
    root: Path
    run_spec_path: Path
    output_dir: Path
    gateway_command: tuple[str, ...]


FixtureFactory = Callable[[Path, str], CompatibilityInvocation]


class CertificationInputs(StrictContractModel):
    repository_root: Path
    source_python: Path
    source_commit: GitCommitSha
    wheel_path: Path
    oci_image: str
    generated_at: UtcTimestamp
    work_dir: Path
    uv_command: str = "uv"
    docker_command: str = "docker"

    @model_validator(mode="after")
    def validate_paths_and_image(self) -> CertificationInputs:
        for field_name, path in (
            ("repository_root", self.repository_root),
            ("source_python", self.source_python),
            ("wheel_path", self.wheel_path),
            ("work_dir", self.work_dir),
        ):
            if not path.is_absolute():
                raise ValueError(f"{field_name} must be absolute")
        if _IMAGE_PATTERN.fullmatch(self.oci_image) is None:
            raise ValueError(
                "oci_image must be an immutable digest-pinned factory-runner reference"
            )
        if "," in os.fspath(self.work_dir) or "\n" in os.fspath(self.work_dir):
            raise ValueError("work_dir is unsafe for an OCI bind mount")
        return self


def _checked(
    executor: CommandExecutor,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int = _EXECUTION_TIMEOUT_SECONDS,
) -> CommandResult:
    result = executor.run(
        command,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        diagnostic = " ".join(result.stderr.split())[:2000]
        raise CompatibilityExecutionError(
            f"compatibility command failed with exit code "
            f"{result.returncode}: {diagnostic}"
        )
    return result


def _safe_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TZ": "UTC",
    }


@contextmanager
def _deterministic_git_environment(repository_root: Path) -> Iterator[None]:
    values = {
        "GIT_AUTHOR_DATE": "2026-07-31T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-07-31T00:00:00Z",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "core.autocrlf",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "core.filemode",
        "GIT_CONFIG_VALUE_2": "true",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _default_fixture_factory(
    root: Path,
    fixture_id: str,
) -> CompatibilityInvocation:
    from tests.factory_runner.integration._support import (
        FAKE_AGENT,
        build_invocation,
    )

    operation, _expected_outcome = _FIXTURE_EXPECTATIONS[fixture_id]
    invocation = build_invocation(root, operation=operation)
    payload = json.loads(invocation.run_spec_path.read_bytes())
    command = payload["policy"]["allowed_commands"][0]
    command[0] = "python3"
    if fixture_id == "author-no-change":
        command[-1] = "raise SystemExit(0)"
    invocation.run_spec_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    copied_agent = root / "compatibility-fake-agent.py"
    shutil.copyfile(FAKE_AGENT, copied_agent)
    agent_mode = {
        "author-success": "author",
        "author-no-change": "author-no-change",
        "verify-success": "fail-if-called",
    }[fixture_id]
    gateway = list(invocation.agent_command(agent_mode))  # type: ignore[arg-type]
    gateway[0] = "python3"
    gateway[1] = str(copied_agent)
    return CompatibilityInvocation(
        fixture_id=fixture_id,
        operation=operation,
        root=root,
        run_spec_path=invocation.run_spec_path,
        output_dir=invocation.output_dir,
        gateway_command=tuple(gateway),
    )


def _tree_digest(output_dir: Path) -> str:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise CompatibilityExecutionError(
            "factory runner did not create a regular output directory"
        )
    manifest: list[dict[str, object]] = []
    for path in sorted(output_dir.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise CompatibilityExecutionError(
                "factory runner output tree contains a non-regular artifact"
            )
        content = path.read_bytes()
        manifest.append(
            {
                "byte_size": len(content),
                "digest": sha256_digest(content),
                "path": path.relative_to(output_dir).as_posix(),
            }
        )
    if not manifest:
        raise CompatibilityExecutionError("factory runner output tree is empty")
    return sha256_digest(canonical_json_bytes(manifest))


def _load_result(
    invocation: CompatibilityInvocation,
    *,
    expected_outcome: Literal["succeeded", "no_change"],
) -> tuple[RunResult, str]:
    result_path = invocation.output_dir / "result" / "run-result.json"
    if not result_path.is_file() or result_path.is_symlink():
        raise CompatibilityExecutionError(
            "factory runner did not emit its canonical RunResult"
        )
    validated = validate_contract(
        result_path.read_bytes(),
        expected_schema="run-result/v1",
    )
    if not isinstance(validated, RunResult):
        raise CompatibilityExecutionError("factory runner emitted a wrong result type")
    verify_contract_digest(validated)
    if (
        validated.operation != invocation.operation
        or validated.outcome != expected_outcome
    ):
        raise CompatibilityExecutionError(
            "factory runner terminal operation or outcome did not match the fixture"
        )
    return validated, _tree_digest(invocation.output_dir)


def _make_oci_fixture_writable(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o777)
        elif stat.S_ISREG(metadata.st_mode):
            path.chmod(0o666)
        else:
            raise CompatibilityExecutionError(
                "compatibility fixture contains a non-regular filesystem entry"
            )
    root.chmod(0o777)


class CompatibilityRunner:
    """Execute the real compatibility suite and construct its certificate."""

    def __init__(
        self,
        *,
        inputs: CertificationInputs,
        executor: CommandExecutor | None = None,
        fixture_factory: FixtureFactory | None = None,
    ) -> None:
        self.inputs = CertificationInputs.model_validate(
            inputs.model_dump(mode="python")
        )
        self.executor = executor or SubprocessCommandExecutor()
        self.fixture_factory = fixture_factory or _default_fixture_factory

    def _prepare_work_dir(self) -> tuple[Path, Path, Path]:
        work_dir = self.inputs.work_dir
        if work_dir == Path(work_dir.anchor) or work_dir.is_symlink():
            raise CompatibilityExecutionError("unsafe compatibility work directory")
        work_dir.mkdir(parents=True, exist_ok=True)
        resolved = work_dir.resolve(strict=True)
        if resolved != work_dir:
            raise CompatibilityExecutionError(
                "compatibility work directory must be canonical"
            )
        venv = work_dir / ".compatibility-venv"
        invocation = work_dir / "invocation"
        outside = work_dir / "outside-checkout"
        for target in (venv, invocation):
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise CompatibilityExecutionError(
                        "unsafe pre-existing compatibility work path"
                    )
                shutil.rmtree(target)
        outside.mkdir(exist_ok=True)
        return venv, invocation, outside

    def _validate_source_checkout(self) -> None:
        result = _checked(
            self.executor,
            ("git", "rev-parse", "HEAD"),
            cwd=self.inputs.repository_root,
            environment=_safe_environment(),
        )
        if result.stdout.strip() != self.inputs.source_commit:
            raise CompatibilityExecutionError(
                "source checkout does not match the certified source commit"
            )

    def _install_wheel(
        self,
        venv: Path,
        outside_checkout: Path,
    ) -> tuple[Path, FactoryRunnerBuildIdentity, str]:
        wheel = self.inputs.wheel_path
        if not wheel.is_file() or wheel.is_symlink():
            raise CompatibilityExecutionError(
                "wheel path must identify one regular artifact"
            )
        wheel_digest = sha256_digest(wheel.read_bytes())
        environment = _safe_environment()
        _checked(
            self.executor,
            (
                self.inputs.uv_command,
                "venv",
                "--python",
                str(self.inputs.source_python),
                str(venv),
            ),
            cwd=self.inputs.work_dir,
            environment=environment,
        )
        wheel_python = venv / "bin" / "python"
        _checked(
            self.executor,
            (
                self.inputs.uv_command,
                "pip",
                "install",
                "--python",
                str(wheel_python),
                str(wheel),
            ),
            cwd=outside_checkout,
            environment=environment,
        )
        identity_result = _checked(
            self.executor,
            (str(wheel_python), "-I", "-c", _BUILD_IDENTITY_QUERY),
            cwd=outside_checkout,
            environment=environment,
        )
        try:
            identity = FactoryRunnerBuildIdentity.model_validate_json(
                identity_result.stdout
            )
        except ValueError as exc:
            raise CompatibilityExecutionError(
                "installed wheel build identity is invalid"
            ) from exc
        if (
            identity.source_commit != self.inputs.source_commit
            or identity.image is not None
        ):
            raise CompatibilityExecutionError(
                "installed wheel does not bind the certified source commit"
            )
        expected_filename = (
            f"{identity.distribution.replace('-', '_')}-"
            f"{identity.version}-py3-none-any.whl"
        )
        if wheel.name != expected_filename:
            raise CompatibilityExecutionError(
                "wheel filename does not match its embedded build identity"
            )
        return wheel_python, identity, wheel_digest

    def _artifact_identities(
        self,
        wheel_identity: FactoryRunnerBuildIdentity,
        wheel_digest: str,
    ) -> tuple[CertifiedArtifact, ...]:
        source_loaded = load_build_identity()
        shared_fields = (
            "distribution",
            "version",
            "source_repository",
            "schema_set_digest",
            "schema_manifest_sha256",
        )
        if any(
            getattr(source_loaded, field) != getattr(wheel_identity, field)
            for field in shared_fields
        ):
            raise CompatibilityExecutionError(
                "source and wheel build identities do not match"
            )
        source_identity = FactoryRunnerBuildIdentity.model_validate(
            {
                **wheel_identity.model_dump(mode="json", by_alias=True),
                "source_commit": self.inputs.source_commit,
                "image": None,
            }
        )
        oci_identity = FactoryRunnerBuildIdentity.model_validate(
            {
                **wheel_identity.model_dump(mode="json", by_alias=True),
                "image": self.inputs.oci_image,
            }
        )
        return (
            CertifiedArtifact(
                kind="source",
                reference=(
                    f"{FACTORY_RUNNER_SOURCE_REPOSITORY}@{self.inputs.source_commit}"
                ),
                digest=None,
                build_identity=source_identity,
            ),
            CertifiedArtifact(
                kind="wheel",
                reference=self.inputs.wheel_path.name,
                digest=wheel_digest,
                build_identity=wheel_identity,
            ),
            CertifiedArtifact(
                kind="oci",
                reference=self.inputs.oci_image,
                digest=self.inputs.oci_image.rsplit("@", 1)[1],
                build_identity=oci_identity,
            ),
        )

    def _inspect_oci(
        self,
        *,
        wheel_identity: FactoryRunnerBuildIdentity,
        wheel_digest: str,
    ) -> None:
        environment = _safe_environment()
        _checked(
            self.executor,
            (self.inputs.docker_command, "pull", self.inputs.oci_image),
            cwd=self.inputs.repository_root,
            environment=environment,
        )
        inspected = _checked(
            self.executor,
            (
                self.inputs.docker_command,
                "image",
                "inspect",
                self.inputs.oci_image,
            ),
            cwd=self.inputs.repository_root,
            environment=environment,
        )
        try:
            payload = json.loads(inspected.stdout)
            image = payload[0]
            config = image["Config"]
            labels = config["Labels"]
            repo_digests = image["RepoDigests"]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise CompatibilityExecutionError(
                "OCI image inspection is incomplete"
            ) from exc
        expected_labels = {
            "io.ai-native.factory-runner.distribution": (FACTORY_RUNNER_DISTRIBUTION),
            "io.ai-native.factory-runner.protocol": ("factory-runner-protocol/v1"),
            "io.ai-native.factory-runner.schema-manifest-sha256": (
                wheel_identity.schema_manifest_sha256
            ),
            "io.ai-native.factory-runner.schema-set-digest": (
                wheel_identity.schema_set_digest
            ),
            "io.ai-native.factory-runner.wheel-sha256": wheel_digest,
            "org.opencontainers.image.revision": self.inputs.source_commit,
            "org.opencontainers.image.version": wheel_identity.version,
            "org.opencontainers.image.ref.name": (wheel_identity.source_tag or ""),
        }
        if (
            not isinstance(labels, Mapping)
            or any(labels.get(key) != value for key, value in expected_labels.items())
            or self.inputs.oci_image not in repo_digests
            or config.get("User") != "10001:10001"
            or config.get("Entrypoint") != ["/opt/ainative/bin/ainative", "factory"]
        ):
            raise CompatibilityExecutionError(
                "OCI image config does not match the exact wheel and source"
            )

    def _execution_environment(
        self,
        invocation: CompatibilityInvocation,
        *,
        python_executable: str,
        execution_identity: FactoryRunnerBuildIdentity,
        source_checkout: bool,
    ) -> dict[str, str]:
        gateway = list(invocation.gateway_command)
        gateway[0] = python_executable
        environment = _safe_environment()
        environment["AINATIVE_FACTORY_AGENT_COMMAND_JSON"] = json.dumps(
            gateway,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        environment["AINATIVE_COMPATIBILITY_BUILD_IDENTITY_JSON"] = json.dumps(
            execution_identity.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if source_checkout:
            environment["PYTHONPATH"] = str(self.inputs.repository_root)
        return environment

    def _execute_fixture(
        self,
        invocation: CompatibilityInvocation,
        *,
        artifact: Literal["source", "wheel", "oci"],
        wheel_python: Path,
        execution_identity: FactoryRunnerBuildIdentity,
        outside_checkout: Path,
    ) -> ArtifactFixtureResult:
        wrapper = invocation.root / "compatibility-entrypoint.py"
        wrapper.write_text(_COMPATIBILITY_ENTRYPOINT, encoding="utf-8")
        operation = "run" if invocation.operation == "author" else "verify"
        runner_arguments = (
            "factory",
            operation,
            "--run-spec",
            str(invocation.run_spec_path),
            "--output-dir",
            str(invocation.output_dir),
        )

        if artifact == "source":
            environment = self._execution_environment(
                invocation,
                python_executable=str(self.inputs.source_python),
                execution_identity=execution_identity,
                source_checkout=True,
            )
            command = (
                str(self.inputs.source_python),
                str(wrapper),
                *runner_arguments,
            )
            cwd = self.inputs.repository_root
        elif artifact == "wheel":
            environment = self._execution_environment(
                invocation,
                python_executable=str(wheel_python),
                execution_identity=execution_identity,
                source_checkout=False,
            )
            command = (str(wheel_python), str(wrapper), *runner_arguments)
            cwd = outside_checkout
        else:
            _make_oci_fixture_writable(invocation.root)
            environment = self._execution_environment(
                invocation,
                python_executable="/opt/ainative/bin/python",
                execution_identity=execution_identity,
                source_checkout=False,
            )
            command_parts = [
                self.inputs.docker_command,
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
                (
                    "/run/ainative:rw,noexec,nosuid,nodev,size=64m,"
                    "mode=0700,uid=10001,gid=10001"
                ),
                "--mount",
                (f"type=bind,src={invocation.root},dst={invocation.root}"),
                "--entrypoint",
                "/opt/ainative/bin/python",
            ]
            for key in (
                "AINATIVE_COMPATIBILITY_BUILD_IDENTITY_JSON",
                "AINATIVE_FACTORY_AGENT_COMMAND_JSON",
                "LC_ALL",
                "PYTHONHASHSEED",
                "TZ",
            ):
                command_parts.extend(("--env", f"{key}={environment[key]}"))
            command_parts.extend(
                (
                    self.inputs.oci_image,
                    str(wrapper),
                    *runner_arguments,
                )
            )
            command = tuple(command_parts)
            cwd = self.inputs.repository_root

        completed = _checked(
            self.executor,
            command,
            cwd=cwd,
            environment=environment,
        )
        if completed.stdout:
            raise CompatibilityExecutionError(
                "factory runner wrote unexpected stdout with event streaming disabled"
            )
        _operation, expected_outcome = _FIXTURE_EXPECTATIONS[invocation.fixture_id]
        result, output_tree_digest = _load_result(
            invocation,
            expected_outcome=expected_outcome,
        )
        return ArtifactFixtureResult(
            artifact=artifact,
            status="passed",
            actual_outcome=expected_outcome,
            run_result_digest=result.result_digest,
            output_manifest_digest=result.output_manifest_digest,
            output_tree_digest=output_tree_digest,
        )

    def run(self) -> FactoryRunnerCompatibilityReport:
        venv, invocation_root, outside_checkout = self._prepare_work_dir()
        self._validate_source_checkout()
        wheel_python, wheel_identity, wheel_digest = self._install_wheel(
            venv,
            outside_checkout,
        )
        self._inspect_oci(
            wheel_identity=wheel_identity,
            wheel_digest=wheel_digest,
        )
        artifacts = self._artifact_identities(wheel_identity, wheel_digest)
        if tuple(artifact.kind for artifact in artifacts) != (CERTIFIED_ARTIFACT_ORDER):
            raise CompatibilityExecutionError(
                "internal compatibility artifact ordering failure"
            )
        execution_identity = artifacts[0].build_identity

        fixture_certifications: list[FixtureCertification] = []
        with _deterministic_git_environment(self.inputs.repository_root):
            for fixture_id in CERTIFIED_FIXTURE_ORDER:
                results: list[ArtifactFixtureResult] = []
                for artifact in CERTIFIED_ARTIFACT_ORDER:
                    if invocation_root.exists():
                        shutil.rmtree(invocation_root)
                    invocation = self.fixture_factory(
                        invocation_root,
                        fixture_id,
                    )
                    if (
                        invocation.fixture_id != fixture_id
                        or invocation.operation != _FIXTURE_EXPECTATIONS[fixture_id][0]
                    ):
                        raise CompatibilityExecutionError(
                            "fixture factory returned the wrong mandatory fixture"
                        )
                    results.append(
                        self._execute_fixture(
                            invocation,
                            artifact=artifact,
                            wheel_python=wheel_python,
                            execution_identity=execution_identity,
                            outside_checkout=outside_checkout,
                        )
                    )
                operation, expected_outcome = _FIXTURE_EXPECTATIONS[fixture_id]
                fixture_certifications.append(
                    FixtureCertification(
                        fixture_id=fixture_id,
                        operation=operation,
                        expected_outcome=expected_outcome,
                        status="passed",
                        canonical_output_tree_digest=results[0].output_tree_digest,
                        results=tuple(results),
                    )
                )

        payload = {
            "schema": COMPATIBILITY_REPORT_SCHEMA,
            "protocol": "factory-runner-protocol/v1",
            "suite_version": COMPATIBILITY_SUITE_VERSION,
            "generated_at": self.inputs.generated_at,
            "source_commit": self.inputs.source_commit,
            "schema_set_digest": wheel_identity.schema_set_digest,
            "schema_manifest_sha256": wheel_identity.schema_manifest_sha256,
            "artifacts": [
                artifact.model_dump(mode="json", by_alias=True)
                for artifact in artifacts
            ],
            "fixtures": [
                fixture.model_dump(mode="json", by_alias=True)
                for fixture in fixture_certifications
            ],
            "status": "passed",
            "report_digest": "sha256:" + ("0" * 64),
        }
        payload["report_digest"] = compatibility_report_digest(payload)
        return FactoryRunnerCompatibilityReport.model_validate(payload)


def _resolve_inputs(
    args: argparse.Namespace,
) -> tuple[CertificationInputs, FactoryRunnerReleaseReceipt | None]:
    receipt = (
        validate_release_receipt(args.receipt.read_bytes())
        if args.receipt is not None
        else None
    )
    source_commit = (
        args.source_commit
        if args.source_commit is not None
        else receipt.source.git_commit_sha
        if receipt is not None
        else None
    )
    oci_image = (
        args.oci_image
        if args.oci_image is not None
        else receipt.oci_image.pinned_reference
        if receipt is not None
        else None
    )
    if source_commit is None or oci_image is None:
        raise ValueError(
            "--source-commit and --oci-image are required without --receipt"
        )
    inputs = CertificationInputs(
        repository_root=args.repository_root.resolve(),
        source_python=Path(os.path.abspath(args.source_python)),
        source_commit=source_commit,
        wheel_path=args.wheel.resolve(),
        oci_image=oci_image,
        generated_at=args.generated_at,
        work_dir=args.work_dir.resolve(),
        uv_command=args.uv_command,
        docker_command=args.docker_command,
    )
    if receipt is not None:
        wheel_digest = sha256_digest(inputs.wheel_path.read_bytes())
        if (
            inputs.source_commit != receipt.source.git_commit_sha
            or inputs.oci_image != receipt.oci_image.pinned_reference
            or inputs.wheel_path.name != receipt.wheel.filename
            or wheel_digest != receipt.wheel.sha256
        ):
            raise ValueError(
                "local compatibility artifacts do not match the release receipt"
            )
    return inputs, receipt


def _validate_receipt_resolved_report(
    report: FactoryRunnerCompatibilityReport,
    receipt: FactoryRunnerReleaseReceipt,
    encoded: bytes,
) -> None:
    wheel = report.artifacts[1]
    oci = report.artifacts[2]
    if (
        report.source_commit != receipt.source.git_commit_sha
        or wheel.digest != receipt.wheel.sha256
        or wheel.build_identity.version != receipt.wheel.version
        or oci.reference != receipt.oci_image.pinned_reference
        or report.schema_set_digest != receipt.contracts.schema_set_digest
        or report.schema_manifest_sha256 != receipt.contracts.schema_manifest_sha256
        or report.suite_version != receipt.compatibility.suite_version
        or report.status != receipt.compatibility.status
        or sha256_digest(encoded) != receipt.compatibility.report_sha256
    ):
        raise CompatibilityExecutionError(
            "rerun report does not reproduce the receipt-resolved "
            "compatibility certificate"
        )


def _write_report(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CompatibilityExecutionError("report output path must not be a symlink")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise CompatibilityExecutionError(
                "unsafe compatibility report temporary path"
            )
        temporary.unlink()
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Certify source, installed-wheel, and digest-pinned OCI "
            "factory-runner compatibility."
        )
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--oci-image")
    parser.add_argument("--source-commit")
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=repository_root,
    )
    parser.add_argument(
        "--source-python",
        type=Path,
        default=Path(sys.executable),
    )
    parser.add_argument("--uv-command", default="uv")
    parser.add_argument("--docker-command", default="docker")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs, receipt = _resolve_inputs(args)
    report = CompatibilityRunner(inputs=inputs).run()
    encoded = canonical_compatibility_report_bytes(report)
    if receipt is not None:
        _validate_receipt_resolved_report(report, receipt, encoded)
    output = args.output.resolve()
    _write_report(output, encoded)
    print(
        f"certified {len(report.artifacts)} artifacts across "
        f"{len(report.fixtures)} mandatory fixtures; "
        f"report {sha256_digest(encoded)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path

from ai_native.factory_runner import outputs
from ai_native.factory_runner.build_identity import FactoryRunnerBuildIdentity
from ai_native.factory_runner.contracts.common import (
    RepositoryIdentity,
    RunIdentity,
)
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root
from ai_native.factory_runner.protocol import (
    schema_manifest_digest,
    schema_set_digest,
)


TIMESTAMP = "2026-07-31T00:00:00Z"
SOURCE_COMMIT = "83e674f8161f38ef9bf4551e92bf655f278262c4"


def _release_identity() -> FactoryRunnerBuildIdentity:
    return FactoryRunnerBuildIdentity(
        schema="factory-runner-build-identity/v1",
        distribution="ai-native-base",
        version="1.4.0",
        source_repository="ufJmacca/ai-native",
        source_commit=SOURCE_COMMIT,
        source_tag=None,
        image=None,
        schema_set_digest=schema_set_digest(),
        schema_manifest_sha256=schema_manifest_digest(),
    )


def test_run_result_uses_the_shared_build_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(outputs, "load_build_identity", _release_identity)
    writer = OutputWriter(validate_output_root(tmp_path / "output"))

    result, _reference = writer.write_run_result(
        operation="author",
        outcome="no_change",
        reason_code="completed",
        message="No changes were required.",
        started_at=TIMESTAMP,
        finished_at=TIMESTAMP,
        identity=RunIdentity(
            work_item_id="work-item-an-04",
            work_item_revision_id="revision-an-04",
            delivery_phase_id="AN-04",
            run_id="run-an-04",
            attempt_id="attempt-an-04",
            correlation_id="correlation-an-04",
        ),
        repository=RepositoryIdentity(
            repository_id="fixture-repository",
            display_name="fixture/target-repository",
            base_commit_sha="a" * 40,
        ),
        completed_stages=(),
    )

    assert result.runner_build.version == "1.4.0"
    assert result.runner_build.source_commit == SOURCE_COMMIT
    assert result.runner_build.image is None

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_native import __version__
from ai_native.factory_runner.build_identity import (
    FactoryRunnerBuildIdentity,
    load_build_identity,
)
from ai_native.factory_runner.protocol import (
    schema_manifest_digest,
    schema_set_digest,
)


SOURCE_COMMIT = "83e674f8161f38ef9bf4551e92bf655f278262c4"


def _identity_payload() -> dict[str, object]:
    return {
        "schema": "factory-runner-build-identity/v1",
        "distribution": "ai-native-base",
        "version": __version__,
        "source_repository": "ufJmacca/ai-native",
        "source_commit": SOURCE_COMMIT,
        # This is a pre-release source identity.  Release tags are recorded
        # only after Release Please creates the real immutable tag.
        "source_tag": None,
        # The image's own manifest digest cannot be embedded in its filesystem
        # without a digest cycle.  The final receipt binds that digest.
        "image": None,
        "schema_set_digest": schema_set_digest(),
        "schema_manifest_sha256": schema_manifest_digest(),
    }


def test_external_build_identity_is_validated_and_loaded_once(
    tmp_path: Path,
) -> None:
    identity_path = tmp_path / "factory-runner-build-identity.json"
    identity_path.write_text(
        json.dumps(_identity_payload(), sort_keys=True),
        encoding="utf-8",
    )

    identity = load_build_identity(identity_path)

    assert identity == FactoryRunnerBuildIdentity.model_validate(_identity_payload())
    assert identity.source_commit == SOURCE_COMMIT
    assert identity.schema_set_digest == schema_set_digest()
    assert identity.schema_manifest_sha256 == schema_manifest_digest()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_commit", "not-a-commit", "source_commit"),
        ("source_tag", "mutable", "source_tag"),
        ("image", "ghcr.io/ufjmacca/ai-native-factory-runner:latest", "mutable"),
        ("schema_set_digest", "sha256:" + ("1" * 64), "schema-set"),
        (
            "schema_manifest_sha256",
            "sha256:" + ("2" * 64),
            "schema-manifest",
        ),
    ],
)
def test_external_build_identity_fails_closed_on_release_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    payload = _identity_payload()
    payload[field] = value
    identity_path = tmp_path / "factory-runner-build-identity.json"
    identity_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_build_identity(identity_path)


def test_source_fallback_has_real_schema_identity_without_inventing_release() -> None:
    identity = load_build_identity()

    assert identity.distribution == "ai-native-base"
    assert identity.version == __version__
    assert identity.source_commit is None
    assert identity.source_tag is None
    assert identity.image is None
    assert identity.schema_set_digest == schema_set_digest()
    assert identity.schema_manifest_sha256 == schema_manifest_digest()

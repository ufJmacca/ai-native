"""Hatch hook that embeds one deterministic factory-runner build identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_SOURCE_COMMIT_ENV = "AINATIVE_FACTORY_BUILD_SOURCE_COMMIT"
_SOURCE_TAG_ENV = "AINATIVE_FACTORY_BUILD_SOURCE_TAG"


class CustomBuildHook(BuildHookInterface):
    """Force-include build identity without modifying the source checkout."""

    _temporary_directory: Path | None = None

    def initialize(self, version: str, build_data: dict) -> None:
        del version
        project_version = str(self.metadata.version)
        if _VERSION_PATTERN.fullmatch(project_version) is None:
            raise RuntimeError(
                "factory-runner release artifacts require a semantic version"
            )

        source_commit = os.environ.get(_SOURCE_COMMIT_ENV) or None
        if (
            source_commit is not None
            and _COMMIT_PATTERN.fullmatch(source_commit) is None
        ):
            raise RuntimeError(
                f"{_SOURCE_COMMIT_ENV} must be a lowercase 40-character SHA"
            )

        source_tag = os.environ.get(_SOURCE_TAG_ENV) or None
        if source_tag is not None:
            expected_tag = f"ai-native-base-v{project_version}"
            if source_tag != expected_tag:
                raise RuntimeError(f"{_SOURCE_TAG_ENV} must equal {expected_tag}")
            if source_commit is None:
                raise RuntimeError(f"{_SOURCE_TAG_ENV} requires {_SOURCE_COMMIT_ENV}")

        root = Path(self.root)
        schema_root = root / "ai_native" / "schemas" / "factory_runner" / "v1"
        schema_set_digest = (
            (schema_root / "schema-set.sha256").read_text(encoding="ascii").strip()
        )
        manifest_bytes = (schema_root / "schema-manifest.json").read_bytes()
        schema_manifest_sha256 = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

        payload = {
            "schema": "factory-runner-build-identity/v1",
            "distribution": "ai-native-base",
            "version": project_version,
            "source_repository": "ufJmacca/ai-native",
            "source_commit": source_commit,
            "source_tag": source_tag,
            # An image cannot contain its own manifest digest without a
            # circular identity.  The release receipt binds the image digest.
            "image": None,
            "schema_set_digest": schema_set_digest,
            "schema_manifest_sha256": schema_manifest_sha256,
        }

        self._temporary_directory = Path(
            tempfile.mkdtemp(prefix="ai-native-factory-build-identity-")
        )
        identity_path = self._temporary_directory / "_build_identity.json"
        identity_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        build_data["force_include"][str(identity_path)] = (
            "ai_native/factory_runner/_build_identity.json"
        )

    def finalize(
        self,
        version: str,
        build_data: dict,
        artifact_path: str,
    ) -> None:
        del version, build_data, artifact_path
        if self._temporary_directory is not None:
            shutil.rmtree(self._temporary_directory)
            self._temporary_directory = None

from __future__ import annotations

from copy import deepcopy
import math
import re

import pytest

from tests.factory_runner.contract._support import (
    PROTOCOL,
    checkpoint,
    protocol_api,
    run_spec,
)


def test_canonical_json_and_digest_are_deterministic_and_non_mutating() -> None:
    api = protocol_api()
    left = {"z": [3, 2, 1], "a": {"unicode": "é"}}
    right = {"a": {"unicode": "é"}, "z": [3, 2, 1]}
    original = deepcopy(left)

    left_bytes = api.canonical_json_bytes(left)
    right_bytes = api.canonical_json_bytes(right)

    assert isinstance(left_bytes, bytes)
    assert left_bytes == right_bytes
    assert b"\n" not in left_bytes
    assert left == original
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", api.sha256_digest(left_bytes))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    api = protocol_api()

    with pytest.raises(ValueError):
        api.canonical_json_bytes({"value": value})


def test_supported_protocol_and_capabilities_negotiate() -> None:
    api = protocol_api()

    api.negotiate_protocol(
        protocol=PROTOCOL,
        required_capabilities=["author"],
        optional_capabilities=["structured-events", "unknown-optional"],
        supported_capabilities=["author", "structured-events"],
    )


@pytest.mark.parametrize(
    ("protocol", "required", "expected_code"),
    [
        ("factory-runner-protocol/v2", ["author"], "unsupported_protocol"),
        (PROTOCOL, ["unknown-required"], "unsupported_capability"),
    ],
)
def test_protocol_negotiation_fails_with_stable_codes(
    protocol: str,
    required: list[str],
    expected_code: str,
) -> None:
    api = protocol_api()

    with pytest.raises(Exception) as exc_info:
        api.negotiate_protocol(
            protocol=protocol,
            required_capabilities=required,
            optional_capabilities=[],
            supported_capabilities=["author"],
        )

    assert getattr(exc_info.value, "code", None) == expected_code


def test_checkpoint_can_resume_with_narrower_authority() -> None:
    api = protocol_api()
    stored = checkpoint()
    resumed = run_spec()
    resumed["identity"]["attempt_id"] = "attempt-02"
    resumed["policy"]["allowed_paths"] = ["src/app.py"]
    resumed["policy"]["allowed_stages"] = ["loop", "verify"]
    resumed["policy"]["max_wall_seconds"] = 300
    resumed["policy"]["max_agent_turns"] = 10
    resumed["policy"]["max_model_tokens"] = 25_000

    api.validate_checkpoint_compatibility(stored, resumed)


@pytest.mark.parametrize(
    "mutation",
    [
        "same_attempt",
        "different_run",
        "different_revision",
        "different_phase",
        "different_repository",
        "different_base",
        "different_context",
        "unsupported_checkpoint_protocol",
        "stage_escalation",
        "path_escalation",
        "command_escalation",
        "environment_escalation",
        "credential_escalation",
        "network_escalation",
        "wall_budget_escalation",
        "turn_budget_escalation",
        "token_budget_escalation",
    ],
)
def test_checkpoint_compatibility_rejects_identity_or_authority_escalation(
    mutation: str,
) -> None:
    api = protocol_api()
    stored = checkpoint()
    resumed = run_spec()
    resumed["identity"]["attempt_id"] = "attempt-02"
    if mutation == "same_attempt":
        resumed["identity"]["attempt_id"] = stored["producer_attempt_id"]
    elif mutation == "different_run":
        resumed["identity"]["run_id"] = "different-run"
    elif mutation == "different_revision":
        resumed["identity"]["work_item_revision_id"] = "different-revision"
    elif mutation == "different_phase":
        resumed["identity"]["delivery_phase_id"] = "different-phase"
    elif mutation == "different_repository":
        resumed["repository"]["repository_id"] = "different-repository"
    elif mutation == "different_base":
        resumed["repository"]["base_commit_sha"] = "b" * 40
    elif mutation == "different_context":
        resumed["context"]["expected_digest"] = "sha256:" + ("f" * 64)
    elif mutation == "unsupported_checkpoint_protocol":
        stored["compatibility"]["protocol"] = "factory-runner-protocol/v2"
    elif mutation == "stage_escalation":
        resumed["policy"]["allowed_stages"].append("commit")
    elif mutation == "path_escalation":
        resumed["policy"]["allowed_paths"].append("secrets.txt")
    elif mutation == "command_escalation":
        resumed["policy"]["allowed_commands"].append(["sh", "-c", "env"])
    elif mutation == "environment_escalation":
        resumed["policy"]["allowed_environment_keys"].append("GITHUB_TOKEN")
    elif mutation == "credential_escalation":
        resumed["policy"]["credential_profile"] = "host-credentials"
    elif mutation == "network_escalation":
        resumed["policy"]["network_profile"] = "open-internet"
    elif mutation == "wall_budget_escalation":
        resumed["policy"]["max_wall_seconds"] = 601
    elif mutation == "turn_budget_escalation":
        resumed["policy"]["max_agent_turns"] = 21
    elif mutation == "token_budget_escalation":
        resumed["policy"]["max_model_tokens"] = 50_001

    with pytest.raises(Exception) as exc_info:
        api.validate_checkpoint_compatibility(stored, resumed)

    assert getattr(exc_info.value, "code", None) == "checkpoint_incompatible"

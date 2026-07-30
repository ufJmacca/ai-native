from __future__ import annotations

from tests.factory_runner.contract._support import require_protocol_api


def test_public_an_01_contract_surface_exists() -> None:
    require_protocol_api()

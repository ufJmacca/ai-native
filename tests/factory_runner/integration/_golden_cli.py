"""Test-only factory CLI entry point with deterministic runtime clocks."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace


GOLDEN_TIMESTAMP = "2026-07-31T00:00:00.000000Z"
LEGACY_GOLDEN_TIMESTAMP = "2026-07-31T00:00:00+00:00"


class _GoldenDatetime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> _GoldenDatetime:
        fixed = cls(2026, 7, 31, tzinfo=UTC)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)  # type: ignore[arg-type]


def _golden_timestamp() -> str:
    return GOLDEN_TIMESTAMP


def _legacy_golden_timestamp() -> str:
    return LEGACY_GOLDEN_TIMESTAMP


def _freeze_runtime_clocks() -> None:
    # The production runner deliberately owns real clocks.  Golden command
    # execution uses this separate entry point so no environment-controlled
    # clock hook can become part of the release surface.
    from ai_native import models, state, utils
    from ai_native.factory_runner import (
        author,
        changes,
        outputs,
        runner,
        verification,
    )

    outputs.utc_timestamp = _golden_timestamp
    runner.utc_timestamp = _golden_timestamp
    verification.utc_timestamp = _golden_timestamp
    changes.utc_timestamp = _golden_timestamp
    author.utc_now = _legacy_golden_timestamp
    state.utc_now = _legacy_golden_timestamp
    utils.utc_now = _legacy_golden_timestamp
    models.datetime = _GoldenDatetime
    state.datetime = _GoldenDatetime
    runner.time = SimpleNamespace(monotonic=lambda: 1_000.0)
    verification.time = SimpleNamespace(monotonic=lambda: 1_000.0)


def main() -> int:
    _freeze_runtime_clocks()

    from ai_native.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

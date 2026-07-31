"""CLI composition for the non-interactive factory runner."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
from typing import Literal

from ai_native.factory_runner.process import CancellationToken


def execute_factory_command(
    *,
    expected_operation: Literal["author", "verify"] | str,
    run_spec_path: Path,
    output_dir: Path,
) -> int:
    """Execute one factory operation without loading legacy configuration."""

    from ai_native.factory_runner.runner import execute_factory

    operation: Literal["author", "verify"] = (
        "author" if expected_operation == "run" else "verify"
    )
    cancellation = CancellationToken()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_cancellation(_signum: int, _frame: object) -> None:
        cancellation.cancel()

    signal.signal(signal.SIGTERM, request_cancellation)
    try:
        return execute_factory(
            expected_operation=operation,
            run_spec_path=run_spec_path,
            output_dir=output_dir,
            environment=os.environ,
            cancellation_token=cancellation,
            log=lambda message: print(message, file=sys.stderr, flush=True),
            event_stdout=sys.stdout.buffer,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


__all__ = ["execute_factory_command"]

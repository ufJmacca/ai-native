from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal

from ai_native.factory_runner import runner
from ai_native.factory_runner.process import CancellationToken


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--boundary", required=True)
    args = parser.parse_args()

    publish_external_bundle = runner.OutputWriter.publish_external_bundle

    def kill_after_checkpoint(
        writer: object,
        references: object,
        contents: object,
    ) -> object:
        published = publish_external_bundle(
            writer,
            references,  # type: ignore[arg-type]
            contents,  # type: ignore[arg-type]
        )
        if not isinstance(contents, dict):
            return published
        for path, content in contents.items():
            if not isinstance(path, str) or not path.endswith("/checkpoint.json"):
                continue
            checkpoint = json.loads(content)
            if checkpoint["workflow_state"]["boundary"] == args.boundary:
                os.kill(os.getpid(), signal.SIGKILL)
        return published

    runner.OutputWriter.publish_external_bundle = kill_after_checkpoint  # type: ignore[method-assign]
    return runner.execute_factory(
        expected_operation="author",
        run_spec_path=args.run_spec,
        output_dir=args.output_dir,
        environment=os.environ.copy(),
        cancellation_token=CancellationToken(),
        log=lambda _message: None,
    )


if __name__ == "__main__":
    raise SystemExit(main())

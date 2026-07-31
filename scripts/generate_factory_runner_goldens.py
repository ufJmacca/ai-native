from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

from ai_native.factory_runner.contracts.common import (
    RepositoryIdentity,
    RunIdentity,
)
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.events import EventSink
from ai_native.factory_runner.outputs import OutputWriter, validate_output_root


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "factory_runner" / "golden"
)
PROTOCOL = "factory-runner-protocol/v1"
TIMESTAMP = "2026-07-31T00:00:00Z"
GOLDEN_FILENAMES = (
    "completion.complete.json",
    "completion.minimal.json",
    "protocol-manifest.complete.json",
    "protocol-manifest.minimal.json",
)


def _identity() -> RunIdentity:
    return RunIdentity(
        work_item_id="golden-work-item",
        work_item_revision_id="golden-revision",
        delivery_phase_id="AN-03",
        run_id="golden-run",
        attempt_id="golden-attempt",
        correlation_id="golden-correlation",
    )


def _repository() -> RepositoryIdentity:
    return RepositoryIdentity(
        repository_id="golden-repository",
        display_name="fixture/golden-repository",
        base_commit_sha="0" * 40,
    )


def _event(sequence: int, event_type: str) -> RunnerEvent:
    return RunnerEvent.model_validate(
        {
            "protocol": PROTOCOL,
            "schema": "runner-event/v1",
            "schema_version": 1,
            "run_id": "golden-run",
            "attempt_id": "golden-attempt",
            "sequence": sequence,
            "timestamp": TIMESTAMP,
            "event_type": event_type,
            "correlation_id": "golden-correlation",
            "causation_id": None,
            "sanitised_payload": {},
            "artifact_refs": [],
        }
    )


def _render_variant(*, complete: bool) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="factory-runner-golden-") as temporary:
        writer = OutputWriter(validate_output_root(Path(temporary) / "output"))
        change_set = None
        latest_checkpoint = None
        if complete:
            sink = EventSink(writer=writer)
            sink.append(_event(1, "RunnerStarted"))
            sink.append(_event(2, "RunnerCompleted"))
            event_stream = sink.finalize()
            latest_checkpoint = writer.write_json(
                "checkpoints/1/checkpoint.json",
                {"fixture": "complete checkpoint"},
            )
            writer.write_bytes(
                "changeset/change.patch",
                b"diff --git a/example b/example\n",
                media_type="application/vnd.git.binary-patch",
            )
            change_set = writer.write_json(
                "changeset/change-set.json",
                {"fixture": "complete change set"},
            )
        else:
            event_stream = writer.write_events_placeholder()

        protocol_manifest = writer.write_protocol_manifest(
            event_stream=event_stream,
        )
        result, result_reference = writer.write_run_result(
            operation="author",
            outcome="succeeded" if complete else "no_change",
            reason_code="completed",
            message=(
                "The complete golden output succeeded."
                if complete
                else "The minimal golden output has no changes."
            ),
            started_at=TIMESTAMP,
            finished_at=TIMESTAMP,
            identity=_identity(),
            repository=_repository(),
            completed_stages=("plan", "loop", "verify") if complete else (),
            latest_checkpoint=latest_checkpoint,
            change_set=change_set,
            event_stream_digest=event_stream.digest,
            protocol_manifest=protocol_manifest,
        )
        writer.write_completion(
            result=result,
            result_reference=result_reference,
            protocol_manifest=protocol_manifest,
        )

        variant = "complete" if complete else "minimal"
        return {
            f"protocol-manifest.{variant}.json": (
                writer.root / protocol_manifest.path
            ).read_bytes(),
            f"completion.{variant}.json": (
                writer.root / "completion.json"
            ).read_bytes(),
        }


def render_terminal_golden_artifacts() -> dict[str, bytes]:
    rendered = {
        **_render_variant(complete=False),
        **_render_variant(complete=True),
    }
    if tuple(sorted(rendered)) != GOLDEN_FILENAMES:
        raise RuntimeError("terminal golden artifact inventory is incomplete")
    return dict(sorted(rendered.items()))


def terminal_golden_drift(output_dir: Path) -> tuple[str, ...]:
    expected = render_terminal_golden_artifacts()
    differences: list[str] = []
    for filename, content in expected.items():
        path = output_dir / filename
        if not path.is_file():
            differences.append(f"missing: {filename}")
        elif path.read_bytes() != content:
            differences.append(f"changed: {filename}")
    return tuple(differences)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic AN-03 terminal output goldens.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in terminal goldens differ",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="write terminal golden artifacts (the default)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="override the golden output directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    if not args.check:
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in render_terminal_golden_artifacts().items():
            (output_dir / filename).write_bytes(content)

    differences = terminal_golden_drift(output_dir)
    if differences:
        print("factory runner terminal golden drift detected:")
        for difference in differences:
            print(f"- {difference}")
        return 1

    action = "verified" if args.check else "generated"
    print(f"{action} {len(GOLDEN_FILENAMES)} terminal golden outputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

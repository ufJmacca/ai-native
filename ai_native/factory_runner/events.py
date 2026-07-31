"""Ordered local event production for factory-runner protocol v1."""

from __future__ import annotations

from threading import RLock
from typing import BinaryIO

from ai_native.factory_runner.canonical import canonical_json_bytes
from ai_native.factory_runner.contracts.common import ArtifactReference
from ai_native.factory_runner.contracts.runner_event import RunnerEvent
from ai_native.factory_runner.outputs import OutputWriter


EVENT_STREAM_PATH = "events.ndjson"
EVENT_STREAM_MEDIA_TYPE = "application/x-ndjson"


class EventSink:
    """Durably stage canonical event lines and publish one immutable stream."""

    def __init__(
        self,
        *,
        writer: OutputWriter,
        stdout: BinaryIO | None = None,
    ) -> None:
        self._writer = writer
        self._stdout = stdout
        self._staged = writer.begin_staged_artifact(
            EVENT_STREAM_PATH,
            media_type=EVENT_STREAM_MEDIA_TYPE,
        )
        self._event_count = 0
        self._identity: tuple[str, str, str] | None = None
        self._final_reference: ArtifactReference | None = None
        self._aborted = False
        self._lock = RLock()

    def _ensure_open(self) -> None:
        if self._final_reference is not None:
            raise RuntimeError("event sink is already finalized")
        if self._aborted:
            raise RuntimeError("event sink is already aborted")
        if self._writer.sealed:
            raise RuntimeError("event sink cannot write after output finalization")

    def append(self, event: RunnerEvent) -> None:
        """Append exactly the next event without accepting gaps or duplicates."""

        validated = RunnerEvent.model_validate(event)
        with self._lock:
            self._ensure_open()
            expected_sequence = self._event_count + 1
            if validated.sequence != expected_sequence:
                raise ValueError(
                    f"event sequence must be contiguous; expected {expected_sequence}"
                )

            identity = (
                str(validated.run_id),
                str(validated.attempt_id),
                str(validated.correlation_id),
            )
            if self._identity is not None and identity != self._identity:
                raise ValueError(
                    "event identity must remain constant within one stream"
                )

            line = canonical_json_bytes(validated.model_dump(mode="json")) + b"\n"
            self._staged.append(line)
            self._event_count = expected_sequence
            if self._identity is None:
                self._identity = identity

            if self._stdout is not None:
                written = self._stdout.write(line)
                if written is not None and written != len(line):
                    raise OSError("event stdout stream accepted a partial write")
                self._stdout.flush()

    def finalize(self) -> ArtifactReference:
        """Atomically publish the final immutable NDJSON artifact."""

        with self._lock:
            self._ensure_open()
            reference = self._staged.finalize()
            self._final_reference = reference
            return reference

    def abort(self) -> None:
        """Discard the unpublished stream after an interrupted attempt."""

        with self._lock:
            self._ensure_open()
            self._staged.abort()
            self._aborted = True


__all__ = [
    "EVENT_STREAM_MEDIA_TYPE",
    "EVENT_STREAM_PATH",
    "EventSink",
]

"""Bounded producer-side secret detection and deterministic human redaction."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import re

from ai_native.factory_runner.process_policy import FactoryPolicyViolation


_MAX_CANARY_BYTES = 64 * 1024
_CANARY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUILTIN_CANARY = re.compile(
    rb"(?:FACTORY_SECRET_CANARY|anf_canary)[A-Za-z0-9_.-]{1,256}"
)
_BUILTIN_OVERLAP_BYTES = 512


class SecretDetectedError(FactoryPolicyViolation):
    """Untrusted bytes contain a secret that may not be persisted."""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"secret detected by policy identifier {identifier}")


@dataclass(frozen=True, slots=True)
class SecretPolicy:
    """Immutable exact canaries known only for the lifetime of one attempt."""

    exact_canaries: tuple[tuple[str, bytes], ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        try:
            canaries = tuple(self.exact_canaries)
        except TypeError as exc:
            raise ValueError("exact_canaries must be an ordered sequence") from exc

        identifiers: set[str] = set()
        values: set[bytes] = set()
        validated: list[tuple[str, bytes]] = []
        for entry in canaries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError(
                    "each exact canary must contain an identifier and bytes"
                )
            identifier, value = entry
            if (
                not isinstance(identifier, str)
                or _CANARY_ID_PATTERN.fullmatch(identifier) is None
            ):
                raise ValueError("secret canary identifier is invalid")
            if (
                not isinstance(value, bytes)
                or not value
                or len(value) > _MAX_CANARY_BYTES
            ):
                raise ValueError("secret canary must be bounded non-empty bytes")
            if identifier in identifiers or value in values:
                raise ValueError(
                    "secret canaries must have unique identifiers and values"
                )
            identifiers.add(identifier)
            values.add(value)
            validated.append((identifier, value))

        object.__setattr__(self, "exact_canaries", tuple(validated))


class SecretScanner:
    """Scan arbitrary byte chunks without retaining the scanned content."""

    def __init__(self, policy: SecretPolicy) -> None:
        if not isinstance(policy, SecretPolicy):
            raise TypeError("policy must be a SecretPolicy")
        self.policy = policy
        self._canaries = tuple(
            sorted(
                policy.exact_canaries,
                key=lambda item: (-len(item[1]), item[0], item[1]),
            )
        )
        self._overlap_bytes = max(
            (
                _BUILTIN_OVERLAP_BYTES,
                *(len(value) - 1 for _identifier, value in self._canaries),
            ),
        )

    def require_clean_chunks(self, chunks: Iterable[bytes]) -> None:
        """Fail on the first exact canary, including one split across chunks."""

        tail = b""
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("secret scanner chunks must be bytes")
            candidate = tail + chunk
            if _BUILTIN_CANARY.search(candidate) is not None:
                raise SecretDetectedError("secret-canary")
            for identifier, canary in self._canaries:
                if canary in candidate:
                    raise SecretDetectedError(identifier)
            if self._overlap_bytes:
                tail = candidate[-self._overlap_bytes :]
            else:
                tail = b""

    def redact_text(self, text: str) -> str:
        """Replace exact textual canaries with stable identifier-only markers."""

        if not isinstance(text, str):
            raise TypeError("redacted human content must be text")
        redacted = text
        for identifier, canary in self._canaries:
            try:
                textual_canary = canary.decode("utf-8", errors="strict")
            except UnicodeError:
                continue
            redacted = redacted.replace(
                textual_canary,
                f"[REDACTED:{identifier}]",
            )
        redacted = _BUILTIN_CANARY.sub(
            b"[REDACTED:secret-canary]",
            redacted.encode("utf-8"),
        ).decode("utf-8")
        self.require_clean_chunks((redacted.encode("utf-8"),))
        return redacted


__all__ = [
    "SecretDetectedError",
    "SecretPolicy",
    "SecretScanner",
]

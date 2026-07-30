from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel
import rfc8785


def canonical_json_bytes(value: Any) -> bytes:
    """Return the RFC 8785 canonical JSON representation of ``value``."""

    try:
        serializable = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        return rfc8785.dumps(serializable)
    except Exception as exc:
        raise ValueError("value cannot be represented as canonical JSON") from exc


def sha256_digest(value: bytes) -> str:
    """Return the protocol's lowercase, prefixed SHA-256 digest."""

    if not isinstance(value, bytes):
        raise TypeError("sha256_digest requires bytes")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "canonical_json_bytes",
    "sha256_digest",
]

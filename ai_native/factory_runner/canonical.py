from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any

from pydantic import BaseModel
import rfc8785


def _json_containers(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_containers(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_containers(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return the RFC 8785 canonical JSON representation of ``value``."""

    try:
        return rfc8785.dumps(_json_containers(value))
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

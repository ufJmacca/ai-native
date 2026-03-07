from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Path):
        return str(value)
    return json.dumps(value, indent=2, sort_keys=True)


class PromptLibrary:
    def __init__(self, prompt_root: Path):
        self.prompt_root = prompt_root

    def load(self, name: str) -> str:
        return (self.prompt_root / name).read_text(encoding="utf-8")

    def render(self, name: str, **context: Any) -> str:
        template = self.load(name)
        payload = {key: _stringify(value) for key, value in context.items()}
        return template.format_map(_SafeDict(payload))


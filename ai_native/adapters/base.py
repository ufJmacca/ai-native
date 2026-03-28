from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class AgentResult(BaseModel):
    text: str = ""
    json_data: Any | None = None
    stdout: str = ""
    stderr: str = ""
    command: list[str] = Field(default_factory=list)
    returncode: int = 0


class AgentAdapter(Protocol):
    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        ...

    def supports_image_inputs(self) -> bool:
        ...


class ReviewAdapter(Protocol):
    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        ...


class AdapterError(RuntimeError):
    pass

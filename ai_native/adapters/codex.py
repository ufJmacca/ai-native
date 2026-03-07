from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.config import AgentProfile


class CodexExecAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "codex-output.txt"
            command = ["codex", "exec", "-C", str(cwd)]
            if self.profile.model:
                command.extend(["-m", self.profile.model])
            if self.profile.sandbox:
                command.extend(["-s", self.profile.sandbox])
            if self.profile.search:
                command.append("--search")
            if schema_path:
                command.extend(["--output-schema", str(schema_path)])
            command.extend(["-o", str(output_path)])
            command.extend(self.profile.extra_args)
            command.append(prompt)
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            if completed.returncode != 0:
                raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "codex exec failed")
            payload = None
            if schema_path and text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AdapterError(f"Codex output was not valid JSON for schema {schema_path}: {exc}") from exc
            return AgentResult(
                text=text,
                json_data=payload,
                stdout=completed.stdout,
                stderr=completed.stderr,
                command=command,
                returncode=completed.returncode,
            )


class CodexReviewAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        command = ["codex", "review", "--base", base_branch or self.profile.base_branch or "main"]
        if prompt:
            command.append(prompt)
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "codex review failed")
        return AgentResult(
            text=completed.stdout.strip(),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
            returncode=completed.returncode,
        )

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        return self.review(cwd=cwd, prompt=prompt, base_branch=self.profile.base_branch)


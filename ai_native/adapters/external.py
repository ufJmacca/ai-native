from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.config import AgentProfile


class ExternalCommandAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        if not self.profile.command:
            raise AdapterError("external-command adapter requires a command")
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_path = Path(tmp_dir) / "prompt.txt"
            output_path = Path(tmp_dir) / "output.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            env = os.environ.copy()
            env["AINATIVE_PROMPT_FILE"] = str(prompt_path)
            env["AINATIVE_OUTPUT_FILE"] = str(output_path)
            if schema_path:
                env["AINATIVE_SCHEMA_FILE"] = str(schema_path)
            completed = subprocess.run(
                self.profile.command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if completed.returncode != 0:
                raise AdapterError(completed.stderr.strip() or "external command failed")
            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            payload = None
            if schema_path and text:
                payload = json.loads(text)
            return AgentResult(
                text=text,
                json_data=payload,
                stdout=completed.stdout,
                stderr=completed.stderr,
                command=list(self.profile.command),
                returncode=completed.returncode,
            )


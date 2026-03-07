from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.config import AgentProfile

LANDLOCK_RESTRICT_ERROR = "Sandbox(LandlockRestrict)"


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("DEVCONTAINER")) or bool(os.environ.get("container"))


class CodexExecAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def _normalized_extra_args(self, sandbox: str | None) -> list[str]:
        extra_args = list(self.profile.extra_args)
        if sandbox == "danger-full-access" and "--full-auto" in extra_args:
            extra_args = [arg for arg in extra_args if arg != "--full-auto"]
            if "--dangerously-bypass-approvals-and-sandbox" not in extra_args:
                extra_args.insert(0, "--dangerously-bypass-approvals-and-sandbox")
        return extra_args

    def _build_command(self, cwd: Path, output_path: Path, prompt: str, schema_path: Path | None, sandbox: str | None) -> list[str]:
        command = ["codex", "exec", "-C", str(cwd)]
        if self.profile.model:
            command.extend(["-m", self.profile.model])
        if sandbox and sandbox != "danger-full-access":
            command.extend(["-s", sandbox])
        if self.profile.search:
            command.append("--search")
        if schema_path:
            command.extend(["--output-schema", str(schema_path)])
        command.extend(["-o", str(output_path)])
        command.extend(self._normalized_extra_args(sandbox))
        command.append(prompt)
        return command

    def _preferred_sandbox(self) -> str | None:
        raw = os.environ.get("AINATIVE_CODEX_CONTAINER_SANDBOX")
        if raw is not None:
            normalized = raw.strip()
            return normalized or None
        if _running_in_container():
            return "danger-full-access"
        return self.profile.sandbox

    def _fallback_sandbox(self, current_sandbox: str | None) -> str | None:
        if _running_in_container() and current_sandbox != "danger-full-access":
            return "danger-full-access"
        return None

    def _should_retry_with_fallback(self, completed: subprocess.CompletedProcess[str], sandbox: str | None) -> bool:
        if completed.returncode == 0 or not sandbox:
            return False
        message = (completed.stderr or completed.stdout or "").strip()
        return LANDLOCK_RESTRICT_ERROR in message

    def _contains_landlock_error(self, text: str, completed: subprocess.CompletedProcess[str]) -> bool:
        combined = "\n".join(part for part in (text, completed.stderr, completed.stdout) if part)
        return LANDLOCK_RESTRICT_ERROR in combined

    def _run_command(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "codex-output.txt"
            sandbox = self._preferred_sandbox()
            command = self._build_command(cwd=cwd, output_path=output_path, prompt=prompt, schema_path=schema_path, sandbox=sandbox)
            completed = self._run_command(command, cwd=cwd)

            if self._should_retry_with_fallback(completed, sandbox):
                fallback_sandbox = self._fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        schema_path=schema_path,
                        sandbox=fallback_sandbox,
                    )
                    completed = self._run_command(command, cwd=cwd)

            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            if self._contains_landlock_error(text, completed):
                fallback_sandbox = self._fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    if output_path.exists():
                        output_path.unlink()
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        schema_path=schema_path,
                        sandbox=fallback_sandbox,
                    )
                    completed = self._run_command(command, cwd=cwd)
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

    def _build_command(self, prompt: str, base_branch: str | None = None) -> list[str]:
        command = ["codex", "review"]
        if self.profile.model:
            command.extend(["-c", f"model={json.dumps(self.profile.model)}"])
        command.extend(self.profile.extra_args)
        command.extend(["--base", base_branch or self.profile.base_branch or "main"])
        if prompt:
            command.append(prompt)
        return command

    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        command = self._build_command(prompt=prompt, base_branch=base_branch)
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

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.config import AgentProfile

LANDLOCK_RESTRICT_ERROR = "Sandbox(LandlockRestrict)"
DANGEROUS_SANDBOX_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("DEVCONTAINER")) or bool(os.environ.get("container"))


def _preferred_sandbox(profile: AgentProfile) -> str | None:
    raw = os.environ.get("AINATIVE_CODEX_CONTAINER_SANDBOX")
    if raw is not None:
        normalized = raw.strip()
        return normalized or None
    if _running_in_container():
        return "danger-full-access"
    return profile.sandbox


def _fallback_sandbox(current_sandbox: str | None) -> str | None:
    if _running_in_container() and current_sandbox != "danger-full-access":
        return "danger-full-access"
    return None


def _normalized_extra_args(extra_args: list[str], sandbox: str | None) -> list[str]:
    normalized = list(extra_args)
    if sandbox == "danger-full-access":
        normalized = [arg for arg in normalized if arg != "--full-auto"]
        if DANGEROUS_SANDBOX_FLAG not in normalized:
            normalized.insert(0, DANGEROUS_SANDBOX_FLAG)
    return normalized


def _contains_retryable_sandbox_error(text: str) -> bool:
    if LANDLOCK_RESTRICT_ERROR in text:
        return True
    normalized = text.lower()
    if "bubblewrap" not in normalized and "bwrap" not in normalized:
        return False
    return "namespace" in normalized or "permission" in normalized or "operation not permitted" in normalized


def _combined_output(text: str, completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (text, completed.stderr, completed.stdout) if part)


def _should_retry_with_fallback(completed: subprocess.CompletedProcess[str], sandbox: str | None) -> bool:
    if completed.returncode == 0 or not sandbox:
        return False
    return _contains_retryable_sandbox_error(_combined_output("", completed))


def _run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class CodexExecAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def _build_command(
        self,
        cwd: Path,
        output_path: Path,
        prompt: str,
        schema_path: Path | None,
        sandbox: str | None,
        image_paths: list[Path] | None,
    ) -> list[str]:
        command = ["codex", "exec", "-C", str(cwd)]
        if self.profile.model:
            command.extend(["-m", self.profile.model])
        for image_path in image_paths or []:
            command.extend(["--image", str(image_path)])
        if sandbox and sandbox != "danger-full-access":
            command.extend(["-s", sandbox])
        if self.profile.search:
            command.append("--search")
        if schema_path:
            command.extend(["--output-schema", str(schema_path)])
        command.extend(["-o", str(output_path)])
        command.extend(_normalized_extra_args(self.profile.extra_args, sandbox))
        command.append(prompt)
        return command

    def supports_image_inputs(self) -> bool:
        return True

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "codex-output.txt"
            sandbox = _preferred_sandbox(self.profile)
            command = self._build_command(
                cwd=cwd,
                output_path=output_path,
                prompt=prompt,
                schema_path=schema_path,
                sandbox=sandbox,
                image_paths=image_paths,
            )
            completed = _run_command(command, cwd=cwd)

            if _should_retry_with_fallback(completed, sandbox):
                fallback_sandbox = _fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        schema_path=schema_path,
                        sandbox=fallback_sandbox,
                        image_paths=image_paths,
                    )
                    completed = _run_command(command, cwd=cwd)

            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            if _contains_retryable_sandbox_error(_combined_output(text, completed)):
                fallback_sandbox = _fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    if output_path.exists():
                        output_path.unlink()
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        schema_path=schema_path,
                        sandbox=fallback_sandbox,
                        image_paths=image_paths,
                    )
                    completed = _run_command(command, cwd=cwd)
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

    def _build_command(self, cwd: Path, output_path: Path, prompt: str, base_branch: str | None = None, sandbox: str | None = None) -> list[str]:
        command = ["codex", "exec", "-C", str(cwd)]
        if sandbox and sandbox != "danger-full-access":
            command.extend(["-s", sandbox])
        command.append("review")
        if self.profile.model:
            command.extend(["-m", self.profile.model])
        command.extend(["-o", str(output_path)])
        command.extend(_normalized_extra_args(self.profile.extra_args, sandbox))
        resolved_base = base_branch or self.profile.base_branch
        if resolved_base:
            command.extend(["--base", resolved_base])
        elif prompt:
            command.append(prompt)
        return command

    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "codex-review.txt"
            sandbox = _preferred_sandbox(self.profile)
            command = self._build_command(
                cwd=cwd,
                output_path=output_path,
                prompt=prompt,
                base_branch=base_branch,
                sandbox=sandbox,
            )
            completed = _run_command(command, cwd=cwd)

            if _should_retry_with_fallback(completed, sandbox):
                fallback_sandbox = _fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        base_branch=base_branch,
                        sandbox=fallback_sandbox,
                    )
                    completed = _run_command(command, cwd=cwd)

            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            if _contains_retryable_sandbox_error(_combined_output(text, completed)):
                fallback_sandbox = _fallback_sandbox(sandbox)
                if fallback_sandbox != sandbox:
                    if output_path.exists():
                        output_path.unlink()
                    command = self._build_command(
                        cwd=cwd,
                        output_path=output_path,
                        prompt=prompt,
                        base_branch=base_branch,
                        sandbox=fallback_sandbox,
                    )
                    completed = _run_command(command, cwd=cwd)
                    text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()

            if completed.returncode != 0:
                raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "codex review failed")
            return AgentResult(
                text=text,
                stdout=completed.stdout,
                stderr=completed.stderr,
                command=command,
                returncode=completed.returncode,
            )

    def run(
        self,
        prompt: str,
        cwd: Path,
        schema_path: Path | None = None,
        image_paths: list[Path] | None = None,
    ) -> AgentResult:
        return self.review(cwd=cwd, prompt=prompt, base_branch=self.profile.base_branch)

    def supports_image_inputs(self) -> bool:
        return False

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from ai_native.adapters.base import AdapterError, AgentResult
from ai_native.config import AgentProfile

_DEFAULT_AUTOPILOT = True
_DEFAULT_ALLOW_ALL_PERMISSIONS = True
_DEFAULT_NO_ASK_USER = True
_DEFAULT_SILENT = True
_DEFAULT_MAX_AUTOPILOT_CONTINUES = 10


def _read_schema_text(schema_path: Path) -> str:
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return schema_path.read_text(encoding="utf-8")
    return json.dumps(payload, indent=2, sort_keys=True)


def _load_schema(schema_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Schema file was not valid JSON: {schema_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterError(f"Schema file must contain a JSON object: {schema_path}")
    return payload


def _schema_prompt(prompt: str, schema_path: Path) -> str:
    return "\n\n".join(
        [
            prompt.rstrip(),
            "Return only valid JSON.",
            "Do not wrap the response in markdown fences or add commentary.",
            "The JSON must satisfy this schema exactly:",
            _read_schema_text(schema_path),
        ]
    )


def _repair_prompt(original_prompt: str, invalid_response: str, schema_path: Path) -> str:
    return "\n\n".join(
        [
            "Rewrite the previous response so it is valid JSON and matches the required schema exactly.",
            "Return only JSON with no markdown fences or prose.",
            "Original task:",
            original_prompt.rstrip(),
            "Previous invalid response:",
            invalid_response.strip() or "(empty response)",
            "Required schema:",
            _read_schema_text(schema_path),
        ]
    )


def _review_prompt(prompt: str, base_branch: str | None) -> str:
    if not base_branch:
        return prompt
    return "\n\n".join(
        [
            f"Review the current branch against the git base branch `{base_branch}`.",
            "Use Copilot's review workflow to inspect the actual code changes before writing findings.",
            prompt.rstrip(),
        ]
    )


class CopilotCLIAdapter:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def _resolved_autopilot(self) -> bool:
        return _DEFAULT_AUTOPILOT if self.profile.autopilot is None else self.profile.autopilot

    def _resolved_allow_all_permissions(self) -> bool:
        return (
            _DEFAULT_ALLOW_ALL_PERMISSIONS
            if self.profile.allow_all_permissions is None
            else self.profile.allow_all_permissions
        )

    def _resolved_no_ask_user(self) -> bool:
        return _DEFAULT_NO_ASK_USER if self.profile.no_ask_user is None else self.profile.no_ask_user

    def _resolved_silent(self) -> bool:
        return _DEFAULT_SILENT if self.profile.silent is None else self.profile.silent

    def _resolved_max_autopilot_continues(self) -> int:
        return (
            _DEFAULT_MAX_AUTOPILOT_CONTINUES
            if self.profile.max_autopilot_continues is None
            else self.profile.max_autopilot_continues
        )

    def _build_command(self, prompt: str, *, agent: str | None = None, use_autopilot: bool) -> list[str]:
        command = ["copilot"]
        if self.profile.model:
            command.extend(["--model", self.profile.model])
        if agent:
            command.extend(["--agent", agent])
        if self._resolved_silent():
            command.append("-s")
        if self._resolved_no_ask_user():
            command.append("--no-ask-user")
        if use_autopilot:
            command.append("--autopilot")
            command.extend(["--max-autopilot-continues", str(self._resolved_max_autopilot_continues())])
        if self._resolved_allow_all_permissions():
            command.append("--yolo")
        else:
            for item in self.profile.allow_tools:
                command.extend(["--allow-tool", item])
            for item in self.profile.deny_tools:
                command.extend(["--deny-tool", item])
            for item in self.profile.allow_urls:
                command.extend(["--allow-url", item])
            for item in self.profile.deny_urls:
                command.extend(["--deny-url", item])
        command.extend(self.profile.extra_args)
        command.extend(["-p", prompt])
        return command

    def _run_command(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdapterError("GitHub Copilot CLI executable 'copilot' was not found in PATH") from exc

    def _parse_json_output(self, text: str, schema_path: Path) -> tuple[str, object]:
        stripped = text.strip()
        if not stripped:
            raise AdapterError(f"Copilot output was empty for schema {schema_path}")
        payload = json.loads(stripped)
        Draft202012Validator(_load_schema(schema_path)).validate(payload)
        return stripped, payload

    def run(self, prompt: str, cwd: Path, schema_path: Path | None = None) -> AgentResult:
        original_prompt = _schema_prompt(prompt, schema_path) if schema_path else prompt
        command = self._build_command(original_prompt, use_autopilot=self._resolved_autopilot())
        completed = self._run_command(command, cwd=cwd)
        if completed.returncode != 0:
            raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "copilot failed")

        text = completed.stdout.strip()
        payload = None
        if schema_path:
            try:
                text, payload = self._parse_json_output(text, schema_path)
            except (json.JSONDecodeError, ValidationError):
                repair_prompt = _repair_prompt(prompt, text, schema_path)
                repair_command = self._build_command(repair_prompt, use_autopilot=self._resolved_autopilot())
                repair_completed = self._run_command(repair_command, cwd=cwd)
                if repair_completed.returncode != 0:
                    raise AdapterError(
                        repair_completed.stderr.strip() or repair_completed.stdout.strip() or "copilot repair failed"
                    )
                try:
                    text, payload = self._parse_json_output(repair_completed.stdout.strip(), schema_path)
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise AdapterError(f"Copilot output did not satisfy schema {schema_path}: {exc}") from exc
                command = repair_command
                completed = repair_completed

        return AgentResult(
            text=text,
            json_data=payload,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
            returncode=completed.returncode,
        )

    def review(self, cwd: Path, prompt: str, base_branch: str | None = None) -> AgentResult:
        command = self._build_command(
            _review_prompt(prompt, base_branch),
            agent="code-review",
            use_autopilot=self._resolved_autopilot(),
        )
        completed = self._run_command(command, cwd=cwd)
        if completed.returncode != 0:
            raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "copilot review failed")
        return AgentResult(
            text=completed.stdout.strip(),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
            returncode=completed.returncode,
        )

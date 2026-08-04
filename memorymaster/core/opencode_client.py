"""Bounded headless OpenCode OAuth client shared by production and evaluation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from memorymaster.dreaming.providers import (
    CommandRunner,
    GLMConsolidator,
    ProviderCallError,
    _default_command_runner,
    _opencode_environment,
)


class OpenCodeClientError(RuntimeError):
    """A stable OpenCode failure that never exposes provider response content."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class OpenCodeClientResult:
    text: str
    provider: str
    model: str
    effort: str
    opencode_version: str
    prompt_hash: str
    latency_ms: int
    input_tokens: int
    output_tokens: int

    def provenance(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("text")
        return payload


class OpenCodeClient:
    """Invoke an authenticated OpenCode session with tools denied and no fallback."""

    def __init__(
        self,
        *,
        model: str = "openai/gpt-5.4-mini",
        effort: str = "medium",
        command: str | None = None,
        runner: CommandRunner = _default_command_runner,
        work_dir: str | Path | None = None,
        timeout: int = 180,
    ) -> None:
        self.model = model if "/" in model else f"openai/{model}"
        self.provider = self.model.split("/", 1)[0]
        self.effort = effort.strip()
        self.command = command or os.environ.get("MEMORYMASTER_OPENCODE_COMMAND")
        self.runner = runner
        self.work_dir = Path(work_dir) if work_dir else Path.home() / ".memorymaster" / "evals" / "opencode"
        self.timeout = max(1, int(timeout))
        self._cached_version: str | None = None

    def complete(self, prompt: str) -> OpenCodeClientResult:
        if not prompt.strip():
            raise OpenCodeClientError("empty_prompt", "OpenCode prompt is empty.")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        executable = self._executable()
        env = _opencode_environment(self.provider)
        api_key = {
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(self.provider)
        if api_key:
            env.pop(api_key, None)
        started = time.monotonic()
        version = self._version(executable, env)
        completed = self._run(executable, prompt, env)
        return self._result(completed, executable, env, prompt, version, started)

    def _result(
        self,
        completed: subprocess.CompletedProcess[str],
        executable: str,
        env: dict[str, str],
        prompt: str,
        version: str,
        started: float,
    ) -> OpenCodeClientResult:
        session_id: str | None = None
        try:
            text, input_tokens, output_tokens, session_id = GLMConsolidator._response_text(
                completed.stdout
            )
        except ProviderCallError as exc:
            raise OpenCodeClientError("malformed_output", str(exc)) from exc
        finally:
            if session_id:
                self._delete_session(executable, session_id, env)
        return OpenCodeClientResult(
            text=text.strip(),
            provider=self.provider,
            model=self.model,
            effort=self.effort,
            opencode_version=version,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _executable(self) -> str:
        executable = self.command or shutil.which("opencode.cmd") or shutil.which("opencode")
        if not executable:
            raise OpenCodeClientError(
                "not_installed", "OpenCode CLI is not installed or not on PATH."
            )
        return executable

    def _version(self, executable: str, env: dict[str, str]) -> str:
        if self._cached_version is not None:
            return self._cached_version
        try:
            completed = self.runner(
                [executable, "--version"], "", 30, self.work_dir, env
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OpenCodeClientError(
                "version_unavailable", "OpenCode version probe failed."
            ) from exc
        version = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        if completed.returncode != 0 or not version:
            raise OpenCodeClientError(
                "version_unavailable", "OpenCode version probe failed."
            )
        self._cached_version = version[:120]
        return self._cached_version

    def _run(
        self, executable: str, prompt: str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        command = [
            executable,
            "run",
            "--pure",
            "--dir",
            str(self.work_dir),
            "--model",
            self.model,
        ]
        if self.effort:
            command.extend(["--variant", self.effort])
        command.extend(["--format", "json"])
        try:
            completed = self.runner(command, prompt, self.timeout, self.work_dir, env)
        except subprocess.TimeoutExpired as exc:
            raise OpenCodeClientError("timeout", "OpenCode call timed out.") from exc
        except OSError as exc:
            raise OpenCodeClientError(
                "call_failed", "OpenCode call failed to start."
            ) from exc
        if completed.returncode != 0:
            raise OpenCodeClientError(
                "call_failed", f"OpenCode call exited with {completed.returncode}."
            )
        return completed

    def _delete_session(
        self, executable: str, session_id: str, env: dict[str, str]
    ) -> None:
        try:
            self.runner(
                [executable, "session", "delete", session_id],
                "",
                30,
                self.work_dir,
                env,
            )
        except (OSError, subprocess.SubprocessError):
            return

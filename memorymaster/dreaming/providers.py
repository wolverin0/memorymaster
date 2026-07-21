"""Bounded JSON-only Gemini and authenticated OpenCode/GLM adapters."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memorymaster.dreaming.models import (
    ConsolidationResult,
    DreamCandidate,
    ExtractionResult,
    ProviderUsage,
    candidate_from_payload,
    decision_from_payload,
)


Transport = Callable[[str, dict[str, Any], dict[str, str], int], tuple[int, dict[str, Any], dict[str, str]]]
CommandRunner = Callable[
    [list[str], str, int, Path, dict[str, str]],
    subprocess.CompletedProcess[str],
]
LOGGER = logging.getLogger(__name__)

_GEMINI_CANDIDATE_SCHEMA = {
    "type": "OBJECT",
    "required": [
        "text",
        "claim_type",
        "subject",
        "predicate",
        "scope_class",
        "evidence_message_id",
        "evidence_quote",
    ],
    "properties": {
        "text": {"type": "STRING", "description": "A complete stable claim, never empty."},
        "claim_type": {"type": "STRING", "description": "Fact, decision, preference, profile, or constraint type."},
        "subject": {"type": "STRING", "description": "The claim subject, never empty."},
        "predicate": {"type": "STRING", "description": "The claim relationship or attribute, never empty."},
        "object_value": {"type": "STRING", "nullable": True},
        "scope_class": {"type": "STRING", "enum": ["project", "personal"]},
        "evidence_message_id": {"type": "STRING", "description": "ID of the quoted supplied message."},
        "evidence_quote": {"type": "STRING", "description": "An exact non-empty substring of that message."},
        "confidence": {"type": "NUMBER", "minimum": 0, "maximum": 1},
        "valid_from": {"type": "STRING", "nullable": True},
        "valid_until": {"type": "STRING", "nullable": True},
    },
}


class ProviderCallError(RuntimeError):
    pass


def _default_transport(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return int(response.status), body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            body = {"error": {"message": "provider HTTP error"}}
        return int(exc.code), body, dict(exc.headers)


def _retry_after(headers: dict[str, str], attempt: int) -> float:
    try:
        return min(30.0, max(0.0, float(headers.get("Retry-After", ""))))
    except ValueError:
        return min(10.0, float(2**attempt))


def _post_with_retry(transport: Transport, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int, sleep: Callable[[float], None]) -> tuple[int, dict[str, Any]]:
    last_status = 0
    attempts = 4
    for attempt in range(attempts):
        try:
            status, body, response_headers = transport(url, payload, headers, timeout)
        except Exception as exc:
            if attempt == attempts - 1:
                raise ProviderCallError("provider request failed") from exc
            sleep(min(10.0, float(2**attempt)))
            continue
        last_status = status
        if status == 200:
            return status, body
        if status not in {408, 429, 500, 502, 503, 504} or attempt == attempts - 1:
            break
        sleep(_retry_after(response_headers, attempt))
    raise ProviderCallError(f"provider request failed with HTTP {last_status}")


def _json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderCallError("provider returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderCallError("provider JSON response must be an object")
    return parsed


def _default_command_runner(
    command: list[str], prompt: str, timeout: int, cwd: Path, env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        env=env,
        check=False,
    )


def _without_markdown_fence(raw: str) -> str:
    text = raw.strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


class GeminiExtractor:
    provider = "google"

    def __init__(self, *, api_key: str | None = None, model: str | None = None, transport: Transport = _default_transport, sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("MEMORYMASTER_DREAM_EXTRACT_MODEL", "gemini-3.5-flash")
        self.transport = transport
        self.sleep = sleep

    def extract(self, messages: list[dict[str, Any]], *, scope: str, capture_hash: str) -> ExtractionResult:
        if not self.api_key:
            raise ProviderCallError("GEMINI_API_KEY is not configured")
        prompt = (
            "Extract at most five stable facts, decisions, preferences, profiles, or constraints. "
            "Ignore ephemeral work chatter. For evidence_quote, copy characters verbatim from the "
            "text of evidence_message_id: never paraphrase, normalize, shorten, or add ellipses. "
            "If no stable candidate has a verbatim quote, return an empty candidates array. "
            "Use scope_class personal only for stable user preference/profile/constraint knowledge."
        )
        payload = self._payload(prompt, messages, scope)
        started = time.monotonic()
        status, body = _post_with_retry(self.transport, self._url(), payload, {"Content-Type": "application/json"}, 90, self.sleep)
        raw = self._response_text(body)
        parsed = _json_object(raw)
        rows = parsed.get("candidates", [])
        if not isinstance(rows, list):
            raise ProviderCallError("Gemini candidates must be an array")
        candidates = tuple(candidate_from_payload(row, capture_hash, index, messages) for index, row in enumerate(rows[:5]) if isinstance(row, dict))
        usage = body.get("usageMetadata", {}) if isinstance(body.get("usageMetadata"), dict) else {}
        return ExtractionResult(candidates, ProviderUsage(self.provider, self.model, status, int((time.monotonic() - started) * 1000), int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0)), True))

    def _url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

    @staticmethod
    def _payload(prompt: str, messages: list[dict[str, Any]], scope: str) -> dict[str, Any]:
        schema = {
            "type": "OBJECT",
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "ARRAY",
                    "maxItems": 5,
                    "items": _GEMINI_CANDIDATE_SCHEMA,
                },
            },
        }
        text = json.dumps({"scope": scope, "messages": messages}, ensure_ascii=False)
        return {"contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000, "thinkingConfig": {"thinkingLevel": "minimal"}, "responseMimeType": "application/json", "responseSchema": schema}}

    @staticmethod
    def _response_text(body: dict[str, Any]) -> str:
        try:
            return "".join(str(part.get("text", "")) for part in body["candidates"][0]["content"]["parts"] if isinstance(part, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError("Gemini response has no text candidate") from exc


class GLMConsolidator:
    provider = "zai-coding-plan"

    def __init__(
        self,
        *,
        model: str | None = None,
        command: str | None = None,
        runner: CommandRunner = _default_command_runner,
        work_dir: str | Path | None = None,
    ) -> None:
        configured_model = model or os.environ.get(
            "MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "zai-coding-plan/glm-5.2",
        )
        self.model = configured_model if "/" in configured_model else f"zai-coding-plan/{configured_model}"
        self.command = command or os.environ.get("MEMORYMASTER_OPENCODE_COMMAND")
        self.runner = runner
        self.work_dir = Path(work_dir) if work_dir is not None else Path.home() / ".memorymaster" / "dreaming-opencode"

    def consolidate(self, candidates: list[DreamCandidate], current_claims: list[dict[str, Any]], *, scope: str) -> ConsolidationResult:
        prompt = self._prompt(candidates, current_claims, scope)
        command = self._command()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("GLM_API_KEY", None)
        env.update({
            "NO_COLOR": "1",
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {"instructions": [], "permission": "deny"}, separators=(",", ":"),
            ),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        })
        started = time.monotonic()
        try:
            completed = self.runner(command, prompt, 180, self.work_dir, env)
        except FileNotFoundError as exc:
            raise ProviderCallError("OpenCode CLI is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderCallError("OpenCode GLM invocation timed out") from exc
        if completed.returncode != 0:
            raise ProviderCallError(
                f"OpenCode GLM invocation failed with exit {completed.returncode}"
            )
        raw, input_tokens, output_tokens, session_id = self._response_text(completed.stdout)
        try:
            return self._validated_result(
                raw, candidates, started, input_tokens, output_tokens,
            )
        finally:
            if session_id:
                self._delete_session(command[0], session_id, env)

    def _validated_result(
        self,
        raw: str,
        candidates: list[DreamCandidate],
        started: float,
        input_tokens: int,
        output_tokens: int,
    ) -> ConsolidationResult:
        parsed = _json_object(_without_markdown_fence(raw))
        rows = parsed.get("decisions", [])
        if not isinstance(rows, list):
            raise ProviderCallError("GLM decisions must be an array")
        valid_ids = {candidate.candidate_id for candidate in candidates}
        decisions = tuple(decision_from_payload(row, valid_ids) for row in rows if isinstance(row, dict))
        decision_ids = [decision.candidate_id for decision in decisions]
        if len(decision_ids) != len(valid_ids) or set(decision_ids) != valid_ids:
            raise ProviderCallError("GLM must return exactly one decision per candidate")
        usage = ProviderUsage(
            self.provider,
            self.model,
            200,
            int((time.monotonic() - started) * 1000),
            input_tokens,
            output_tokens,
            True,
        )
        return ConsolidationResult(decisions, usage)

    def _delete_session(
        self, executable: str, session_id: str, env: dict[str, str],
    ) -> None:
        command = [executable, "session", "delete", session_id]
        try:
            completed = self.runner(command, "", 30, self.work_dir, env)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("Could not delete Dreaming OpenCode session: %s", type(exc).__name__)
            return
        if completed.returncode != 0:
            LOGGER.warning(
                "Could not delete Dreaming OpenCode session: exit %s", completed.returncode,
            )

    def _command(self) -> list[str]:
        executable = self.command or shutil.which("opencode.cmd") or shutil.which("opencode")
        if not executable:
            raise ProviderCallError("OpenCode CLI is not installed or not on PATH")
        return [
            executable,
            "run",
            "--pure",
            "--dir",
            str(self.work_dir),
            "--model",
            self.model,
            "--format",
            "json",
        ]

    @staticmethod
    def _prompt(
        candidates: list[DreamCandidate], current_claims: list[dict[str, Any]], scope: str,
    ) -> str:
        system = (
            "Compare every candidate with the governed current claims. Return one JSON object "
            "with a decisions array and exactly one decision per candidate. Allowed actions are "
            "add, reinforce, propose_supersede, propose_stale, propose_conflict, and ignore. "
            "Every decision requires candidate_id, action, rationale, and confidence. Proposal "
            "actions require target_claim_id. Never merge scopes. Do not use tools. Output JSON only."
        )
        user = json.dumps({"scope": scope, "candidates": [c.to_dict() for c in candidates], "current_claims": current_claims}, ensure_ascii=False)
        return f"{system}\n\nINPUT:\n{user}"

    @staticmethod
    def _response_text(output: str) -> tuple[str, int, int, str | None]:
        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        session_id: str | None = None
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderCallError("OpenCode returned malformed event JSON") from exc
            event_session = event.get("sessionID")
            if session_id is None and isinstance(event_session, str):
                session_id = event_session
            if event.get("type") == "text":
                part = event.get("part", {})
                if isinstance(part, dict):
                    text_parts.append(str(part.get("text", "")))
            elif event.get("type") == "step_finish":
                part = event.get("part", {})
                tokens = part.get("tokens", {}) if isinstance(part, dict) else {}
                if isinstance(tokens, dict):
                    input_tokens += int(tokens.get("input", 0) or 0)
                    output_tokens += int(tokens.get("output", 0) or 0)
        if not text_parts:
            raise ProviderCallError("OpenCode GLM response has no text event")
        return "".join(text_parts), input_tokens, output_tokens, session_id

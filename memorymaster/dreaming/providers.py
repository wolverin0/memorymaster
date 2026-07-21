"""Bounded JSON-only Gemini and GLM adapters for Dreaming."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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
        return min(5.0, max(0.0, float(headers.get("Retry-After", ""))))
    except ValueError:
        return 0.5 * (attempt + 1)


def _post_with_retry(transport: Transport, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int, sleep: Callable[[float], None]) -> tuple[int, dict[str, Any]]:
    last_status = 0
    for attempt in range(2):
        try:
            status, body, response_headers = transport(url, payload, headers, timeout)
        except Exception as exc:
            if attempt == 1:
                raise ProviderCallError("provider request failed") from exc
            sleep(0.5)
            continue
        last_status = status
        if status == 200:
            return status, body
        if status not in {408, 429, 500, 502, 503, 504} or attempt == 1:
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
            "Ignore ephemeral work chatter. Cite one exact substring from one supplied message. "
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
        schema = {"type": "OBJECT", "required": ["candidates"], "properties": {"candidates": {"type": "ARRAY", "maxItems": 5, "items": {"type": "OBJECT"}}}}
        text = json.dumps({"scope": scope, "messages": messages}, ensure_ascii=False)
        return {"contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000, "thinkingConfig": {"thinkingLevel": "minimal"}, "responseMimeType": "application/json", "responseSchema": schema}}

    @staticmethod
    def _response_text(body: dict[str, Any]) -> str:
        try:
            return "".join(str(part.get("text", "")) for part in body["candidates"][0]["content"]["parts"] if isinstance(part, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError("Gemini response has no text candidate") from exc


class GLMConsolidator:
    provider = "zai"

    def __init__(self, *, api_key: str | None = None, model: str | None = None, transport: Transport = _default_transport, sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GLM_API_KEY", "")
        self.model = model or os.environ.get("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "glm-5.2")
        self.base_url = os.environ.get("MEMORYMASTER_ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
        self.transport = transport
        self.sleep = sleep

    def consolidate(self, candidates: list[DreamCandidate], current_claims: list[dict[str, Any]], *, scope: str) -> ConsolidationResult:
        if not self.api_key:
            raise ProviderCallError("GLM_API_KEY is not configured")
        payload = self._payload(candidates, current_claims, scope)
        started = time.monotonic()
        status, body = _post_with_retry(self.transport, f"{self.base_url.rstrip('/')}/chat/completions", payload, {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, 120, self.sleep)
        raw = self._response_text(body)
        parsed = _json_object(raw)
        rows = parsed.get("decisions", [])
        if not isinstance(rows, list):
            raise ProviderCallError("GLM decisions must be an array")
        valid_ids = {candidate.candidate_id for candidate in candidates}
        decisions = tuple(decision_from_payload(row, valid_ids) for row in rows if isinstance(row, dict))
        decision_ids = [decision.candidate_id for decision in decisions]
        if len(decision_ids) != len(valid_ids) or set(decision_ids) != valid_ids:
            raise ProviderCallError("GLM must return exactly one decision per candidate")
        usage = body.get("usage", {}) if isinstance(body.get("usage"), dict) else {}
        return ConsolidationResult(decisions, ProviderUsage(self.provider, self.model, status, int((time.monotonic() - started) * 1000), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), True))

    def _payload(self, candidates: list[DreamCandidate], current_claims: list[dict[str, Any]], scope: str) -> dict[str, Any]:
        system = "Compare each candidate with governed current claims. Return add, reinforce, propose_supersede, propose_stale, propose_conflict, or ignore. Proposal actions require target_claim_id. Never merge scopes."
        user = json.dumps({"scope": scope, "candidates": [c.to_dict() for c in candidates], "current_claims": current_claims}, ensure_ascii=False)
        return {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.1, "max_tokens": 4000, "thinking": {"type": "enabled"}, "reasoning_effort": "high", "response_format": {"type": "json_object"}}

    @staticmethod
    def _response_text(body: dict[str, Any]) -> str:
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError("GLM response has no message content") from exc

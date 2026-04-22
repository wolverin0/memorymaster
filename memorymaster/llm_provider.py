"""Multi-provider LLM client for MemoryMaster hooks and curators.

Supports: google (Gemini), openai (GPT/o-series), anthropic (Claude), ollama (local).
Provider is selected via MEMORYMASTER_LLM_PROVIDER env var (default: google).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import urllib.error
from typing import Any


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Provider implementations — all return raw text response
# ---------------------------------------------------------------------------


def _call_google(prompt: str, text: str) -> str:
    """Google Gemini API.

    Default model is ``gemini-3.1-flash-lite-preview``: observed free-tier
    RPM is ~500 per key versus ~20 for gemini-2.5-flash-lite, and output
    quality is materially better on the tasks this repo feeds it
    (extraction, wiki-absorb, classification). Override via
    ``MEMORYMASTER_LLM_MODEL``.

    Key source priority:
        1. Rotator file (``~/.memorymaster/gemini-keys.env``) — rotates
           round-robin, auto-cooldown on 429, permanent skip on revoked keys.
        2. ``GEMINI_API_KEY`` env var — singular key, no rotation.
    """
    from memorymaster.key_rotator import get_rotator

    model = _env("MEMORYMASTER_LLM_MODEL", "gemini-3.1-flash-lite-preview")

    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 500,
        },
    }

    # Gemini 3.x models support thinkingLevel
    if "gemini-3" in model:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "minimal"}
    elif "gemini-2.5" in model:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

    rotator = get_rotator("gemini")
    if rotator and len(rotator) > 0:
        # Try each key at most once per call, rotating on 429.
        attempts = len(rotator)
        for _ in range(attempts):
            pair = rotator.next_key()
            if pair is None:
                break
            label, key = pair
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            result = _http_post(url, payload, _extract_google, rotator_label=label, rotator=rotator)
            if result:
                return result
        return ""

    api_key = _env("GEMINI_API_KEY")
    if not api_key:
        return ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    return _http_post(url, payload, _extract_google)


def _call_openai(prompt: str, text: str) -> str:
    """OpenAI-compatible API (GPT, o-series, or any compatible endpoint)."""
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        return ""

    model = _env("MEMORYMASTER_LLM_MODEL", "gpt-4o-mini")
    base = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base}/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 500,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    return _http_post(url, payload, _extract_openai, extra_headers=headers)


def _call_anthropic(prompt: str, text: str) -> str:
    """Anthropic Claude API."""
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    model = _env("MEMORYMASTER_LLM_MODEL", "claude-haiku-4-5-20251001")
    url = "https://api.anthropic.com/v1/messages"

    payload = {
        "model": model,
        "max_tokens": 500,
        "system": prompt,
        "messages": [{"role": "user", "content": text}],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    return _http_post(url, payload, _extract_anthropic, extra_headers=headers)


def _call_ollama(prompt: str, text: str) -> str:
    """Local Ollama instance."""
    base = _env("OLLAMA_URL", "http://localhost:11434")
    model = _env("MEMORYMASTER_LLM_MODEL", "llama3.2:3b")
    url = f"{base}/api/generate"

    payload = {
        "model": model,
        "prompt": f"{prompt}\n\n{text}",
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 500},
    }

    return _http_post(url, payload, _extract_ollama)


# ---------------------------------------------------------------------------
# Response extractors
# ---------------------------------------------------------------------------


def _extract_google(data: dict) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _extract_openai(data: dict) -> str:
    choices = data.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def _extract_anthropic(data: dict) -> str:
    content = data.get("content", [])
    if not content:
        return ""
    return "".join(c.get("text", "") for c in content if c.get("type") == "text")


def _extract_ollama(data: dict) -> str:
    return data.get("response", "")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _http_post(
    url: str,
    payload: dict,
    extractor: Any,
    extra_headers: dict | None = None,
    timeout: int = 10,
    rotator_label: str | None = None,
    rotator: Any = None,
) -> str:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    log = logging.getLogger(__name__)

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return extractor(result)
    except urllib.error.HTTPError as exc:
        # Read the response body so 429/4xx messages surface the provider's
        # actual error (quota metric, model, Retry-After, status).
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        log.warning(
            "LLM call failed (%s) label=%s: HTTP %s %s",
            url[:60], rotator_label or "-", exc.code, body[:500],
        )
        if rotator is not None and rotator_label and exc.code in (429, 403):
            retry_after = _parse_retry_after(body, exc.headers)
            status = _extract_google_status(body)
            if status in ("PERMISSION_DENIED", "UNAUTHENTICATED") or exc.code == 403:
                rotator.mark_banned(rotator_label, reason=f"HTTP {exc.code} {status}")
            else:
                rotator.mark_rate_limited(rotator_label, retry_after=retry_after)
        return ""
    except Exception as exc:
        log.warning("LLM call failed (%s) label=%s: %s", url[:60], rotator_label or "-", exc)
        return ""


def _parse_retry_after(body: str, headers: Any) -> float | None:
    """Extract seconds-to-retry from Google's 429 body or Retry-After header."""
    if headers is not None:
        h_val = dict(headers).get("Retry-After") if hasattr(headers, "get") else None
        if h_val:
            try:
                return float(h_val)
            except (TypeError, ValueError):
                pass
    m = re.search(r"[Pp]lease retry in ([\d.]+)s", body)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _extract_google_status(body: str) -> str:
    """Pull `error.status` field from a Google API JSON error body."""
    try:
        data = json.loads(body)
        return str(data.get("error", {}).get("status", ""))
    except (json.JSONDecodeError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "google": _call_google,
    "gemini": _call_google,
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "claude": _call_anthropic,
    "ollama": _call_ollama,
}


def call_llm(prompt: str, text: str) -> str:
    """Call configured LLM provider. Returns raw text response.

    Configure via env vars:
        MEMORYMASTER_LLM_PROVIDER  — google|openai|anthropic|ollama (default: google)
        MEMORYMASTER_LLM_MODEL     — model override (default per provider)
        GEMINI_API_KEY             — for google provider
        OPENAI_API_KEY             — for openai provider
        ANTHROPIC_API_KEY          — for anthropic provider
        OLLAMA_URL                 — for ollama provider (default: http://localhost:11434)
    """
    provider = _env("MEMORYMASTER_LLM_PROVIDER", "google").lower()
    fn = _PROVIDERS.get(provider)
    if not fn:
        return ""
    return fn(prompt, text)


def parse_json_response(text: str) -> list[dict]:
    """Parse LLM response as JSON array, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []
    except (json.JSONDecodeError, ValueError):
        return []

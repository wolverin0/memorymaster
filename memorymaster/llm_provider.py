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
        "options": {"temperature": 0.1, "num_predict": 500, "num_ctx": 8192},
    }

    return _http_post(url, payload, _extract_ollama, timeout=60)


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


# Quota-exhausted detection for the fallback chain.
#
# Defensive on purpose (claim 11907): err on the side of false-negatives
# (fallback fires less) rather than false-positives (fallback spuriously
# fires on legitimate content that merely mentions "quota"). Each pattern
# requires error-response shape, not a bare English word:
#   - "RESOURCE_EXHAUSTED"  — Google API error.status field
#   - quoted-code patterns like `"code": 429` in a JSON error body
#   - "quota exceeded" as a contiguous phrase (rare outside error bodies)
# Case-insensitive because some providers capitalize inconsistently.
_QUOTA_EXHAUSTED_RE = re.compile(
    r"RESOURCE_EXHAUSTED"
    r"|\"code\"\s*:\s*429"
    r"|\bHTTP\s*429\b"
    r"|\bquota\s+exceeded\b"
    r"|\brate[\s_-]?limit(?:ed)?\b.*\bexceeded\b",
    re.IGNORECASE,
)


_FALLBACK_STATS: dict[str, int] = {"attempts": 0, "fired": 0, "primary_ok": 0}


def get_fallback_stats() -> dict[str, int]:
    """Return a copy of fallback telemetry counters.

    Counters:
        attempts    — total `call_llm` invocations
        fired       — times fallback provider was actually called
        primary_ok  — times primary returned a usable (non-empty, non-quota) response
    """
    return dict(_FALLBACK_STATS)


def reset_fallback_stats() -> None:
    """Reset fallback telemetry counters to zero (test helper / operator reset)."""
    for key in _FALLBACK_STATS:
        _FALLBACK_STATS[key] = 0


def _looks_like_quota_error(response: str) -> bool:
    """True if the response body appears to be a quota-exhausted error."""
    if not response:
        return False
    return bool(_QUOTA_EXHAUSTED_RE.search(response))


def call_llm(prompt: str, text: str) -> str:
    """Call configured LLM provider with optional fallback chain. Returns raw text.

    Configure via env vars:
        MEMORYMASTER_LLM_PROVIDER           — google|openai|anthropic|ollama (default: google)
        MEMORYMASTER_LLM_MODEL              — model override (default per provider)
        MEMORYMASTER_LLM_FALLBACK_PROVIDER  — (optional) provider to use if primary returns
                                              empty or a quota-exhausted error
        MEMORYMASTER_LLM_FALLBACK_MODEL     — (optional) model override used while calling
                                              fallback provider (MEMORYMASTER_LLM_MODEL is
                                              swapped for the duration of the fallback call
                                              and restored afterwards)
        GEMINI_API_KEY                      — for google provider
        OPENAI_API_KEY                      — for openai provider
        ANTHROPIC_API_KEY                   — for anthropic provider
        OLLAMA_URL                          — for ollama provider (default: http://localhost:11434)

    Fallback semantics:
        1. Call primary. If non-empty AND not a quota-exhausted error shape → return it.
        2. Otherwise, if MEMORYMASTER_LLM_FALLBACK_PROVIDER is set, call the fallback
           (temporarily swapping MEMORYMASTER_LLM_MODEL to the fallback model if provided),
           log at INFO, and return the fallback response.
        3. If fallback also fails or returns empty, return the primary's (possibly empty)
           response — preserves legacy "empty string on failure" contract for callers that
           already treat empty as "no result".
    """
    _FALLBACK_STATS["attempts"] += 1
    log = logging.getLogger(__name__)

    primary_name = _env("MEMORYMASTER_LLM_PROVIDER", "google").lower()
    primary_fn = _PROVIDERS.get(primary_name)
    if not primary_fn:
        return ""

    primary_response = primary_fn(prompt, text)

    # Happy path: primary returned something that doesn't look like a quota error.
    if primary_response and not _looks_like_quota_error(primary_response):
        _FALLBACK_STATS["primary_ok"] += 1
        return primary_response

    fallback_name = _env("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "").lower()
    if not fallback_name:
        return primary_response

    fallback_fn = _PROVIDERS.get(fallback_name)
    if not fallback_fn:
        log.warning(
            "llm_fallback configured but unknown provider=%s; returning primary response",
            fallback_name,
        )
        return primary_response

    reason = "quota_exhausted" if _looks_like_quota_error(primary_response) else "empty_response"
    log.info("llm_fallback_fired primary=%s reason=%s", primary_name, reason)
    _FALLBACK_STATS["fired"] += 1

    # Swap MEMORYMASTER_LLM_MODEL to fallback model for the duration of the call.
    fallback_model = _env("MEMORYMASTER_LLM_FALLBACK_MODEL", "")
    saved_model = os.environ.get("MEMORYMASTER_LLM_MODEL")
    try:
        if fallback_model:
            os.environ["MEMORYMASTER_LLM_MODEL"] = fallback_model
        elif "MEMORYMASTER_LLM_MODEL" in os.environ:
            # No fallback model configured — let the fallback provider use its
            # own default, not the primary's model (which may be Gemini-specific).
            del os.environ["MEMORYMASTER_LLM_MODEL"]
        fallback_response = fallback_fn(prompt, text)
    finally:
        if saved_model is None:
            os.environ.pop("MEMORYMASTER_LLM_MODEL", None)
        else:
            os.environ["MEMORYMASTER_LLM_MODEL"] = saved_model

    if fallback_response and not _looks_like_quota_error(fallback_response):
        return fallback_response

    # Both failed — match legacy contract, return primary's (possibly empty) response.
    return primary_response


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

"""Multi-provider LLM client for MemoryMaster hooks and curators.

Supports: google (Gemini), openai (GPT/o-series), anthropic (Claude API),
claude_cli (Claude Code OAuth via local `claude --print` binary), ollama (local).
Provider is selected via MEMORYMASTER_LLM_PROVIDER env var (default: google).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from contextvars import ContextVar
from typing import Any

from memorymaster import llm_budget


# Per-call env overrides. Threaded via contextvars so concurrent callers
# (steward ThreadPoolExecutor + MCP rerank) never read each other's
# in-flight overrides — unlike os.environ, which is process-global.
# Currently used for the fallback model swap; a None value means "no
# override, read os.environ as usual". audit: fallback-model-env-mutation
_ENV_OVERRIDES: ContextVar[dict[str, str | None] | None] = ContextVar(
    "memorymaster_llm_env_overrides", default=None
)


# When True for the current call (set via contextvars), _call_google skips the
# shared module-level file key-rotator and uses the single GEMINI_API_KEY path.
# Lets callers like llm_rerank scope one request to a no-rotator client WITHOUT
# clearing the process-global rotator cache (which would poison concurrent
# call_llm invocations). audit: rerank-temporary-env-poisons-shared-state
_SKIP_FILE_ROTATOR: ContextVar[bool] = ContextVar(
    "memorymaster_llm_skip_file_rotator", default=False
)


def use_call_scoped_env(
    overrides: dict[str, str | None],
    *,
    skip_file_rotator: bool = False,
):
    """Context manager: apply per-call env overrides + optional rotator skip.

    Overrides are read by ``_env``/``_call_google`` via contextvars, so they
    are private to the calling thread/task and never mutate ``os.environ`` or
    the shared key-rotator cache. A ``None`` override value means "behave as if
    the var is unset".
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        prev = _ENV_OVERRIDES.get()
        merged = dict(prev) if prev else {}
        merged.update(overrides)
        tok_env = _ENV_OVERRIDES.set(merged)
        tok_skip = _SKIP_FILE_ROTATOR.set(skip_file_rotator)
        try:
            yield
        finally:
            _SKIP_FILE_ROTATOR.reset(tok_skip)
            _ENV_OVERRIDES.reset(tok_env)

    return _ctx()


def _env(key: str, default: str = "") -> str:
    overrides = _ENV_OVERRIDES.get()
    if overrides is not None and key in overrides:
        value = overrides[key]
        # An explicit None override means "behave as if unset" — let the
        # provider fall back to its own default rather than the primary's.
        return default if value is None else value
    return os.environ.get(key, default)


_GOOGLE_ENV_ROTATOR = None
_GOOGLE_ENV_ROTATOR_KEYSET: tuple[str, ...] = ()


def _truthy_env(key: str) -> bool:
    return _env(key).strip().lower() in {"1", "true", "yes", "on"}


def _split_keys(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _google_rotation_keys() -> list[str]:
    for env_key in ("MEMORYMASTER_LLM_API_KEYS", "GEMINI_API_KEYS", "MEMORYMASTER_API_KEYS"):
        keys = _split_keys(_env(env_key))
        if keys:
            return keys

    single_key = _env("GEMINI_API_KEY") or _env("MEMORYMASTER_API_KEY")
    return [single_key.strip()] if single_key.strip() else []


def _get_google_env_rotator():
    if not _truthy_env("MEMORYMASTER_LLM_KEY_ROTATION"):
        return None

    keys = tuple(_google_rotation_keys())
    if not keys:
        return None

    global _GOOGLE_ENV_ROTATOR, _GOOGLE_ENV_ROTATOR_KEYSET
    if _GOOGLE_ENV_ROTATOR is None or _GOOGLE_ENV_ROTATOR_KEYSET != keys:
        from memorymaster.llm_steward import DEFAULT_COOLDOWN_SECONDS, KeyRotator

        try:
            cooldown = float(
                _env(
                    "MEMORYMASTER_LLM_KEY_COOLDOWN_SECONDS",
                    str(DEFAULT_COOLDOWN_SECONDS),
                )
            )
        except ValueError:
            cooldown = DEFAULT_COOLDOWN_SECONDS
        _GOOGLE_ENV_ROTATOR = KeyRotator(keys=list(keys), cooldown_seconds=cooldown)
        _GOOGLE_ENV_ROTATOR_KEYSET = keys
    return _GOOGLE_ENV_ROTATOR


def _call_google_with_env_rotation(model: str, payload: dict[str, Any]) -> str | None:
    rotator = _get_google_env_rotator()
    if rotator is None:
        return None

    for _ in range(rotator.key_count):
        api_key = rotator.get_key()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        # status_sink records the HTTP status of an HTTPError (if any) so we can
        # distinguish a genuine 429 (cool the key) from an empty-200 success
        # (leave the key untouched — cooling a HEALTHY key here would, across a
        # batch, cool ALL keys and make get_key falsely sleep on "all keys
        # rate-limited"). audit: env-rotation-empty200-cools-healthy-key
        status_sink: dict[str, int | None] = {"http_status": None}
        result = _http_post(url, payload, _extract_google, status_sink=status_sink)
        if result:
            rotator.clear_cooldown(api_key)
            return result
        if status_sink["http_status"] == 429:
            rotator.mark_rate_limited(api_key)
        # else: empty-200 (or non-rate-limit error). Do NOT cool a healthy key.
    return ""


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
        1. Env rotation when ``MEMORYMASTER_LLM_KEY_ROTATION=1`` and multiple
           keys are configured via ``MEMORYMASTER_LLM_API_KEYS``,
           ``GEMINI_API_KEYS``, or ``MEMORYMASTER_API_KEYS``.
        2. Rotator file (``~/.memorymaster/gemini-keys.env``) — rotates
           round-robin, auto-cooldown on 429, permanent skip on revoked keys.
        3. ``GEMINI_API_KEY`` env var — singular key, no rotation.
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

    env_rotated_result = _call_google_with_env_rotation(model, payload)
    if env_rotated_result is not None:
        return env_rotated_result

    rotator = None if _SKIP_FILE_ROTATOR.get() else get_rotator("gemini")
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
        "options": {"temperature": 0.1, "num_predict": 1500, "num_ctx": 8192},
    }

    return _http_post(url, payload, _extract_ollama, timeout=60)


def _call_claude_cli(prompt: str, text: str) -> str:
    """Claude Code OAuth via local `claude --print` binary.

    Uses the user's existing Claude Code subscription (no API key needed). The
    `claude` CLI must be on PATH. OAuth credentials are managed by the CLI and
    persist across sessions on desktop installs (VM tokens expire ~24h).

    Cold start adds ~3-15s per call — acceptable for batched/cron use, not for
    latency-sensitive recall paths. Set MEMORYMASTER_CLAUDE_CLI_BIN to override
    binary location (default: 'claude' from PATH).
    """
    log = logging.getLogger(__name__)
    bin_path = _env("MEMORYMASTER_CLAUDE_CLI_BIN", "") or shutil.which("claude")
    if not bin_path:
        log.warning("claude_cli: binary not found on PATH (set MEMORYMASTER_CLAUDE_CLI_BIN)")
        return ""

    model = _env("MEMORYMASTER_LLM_MODEL", "claude-haiku-4-5-20251001")
    timeout_s = int(_env("MEMORYMASTER_CLAUDE_CLI_TIMEOUT", "120"))
    full_prompt = f"{prompt}\n\n{text}"

    # On Windows, prevent a console window from popping up for every
    # subprocess.run call. Without this flag, when the parent process is
    # pythonw.exe (no console) — e.g. the MemoryMasterSteward scheduled
    # task running silently — Windows creates a NEW console for each
    # `claude --print` child, producing one popup window per claim
    # processed. CREATE_NO_WINDOW (0x08000000) suppresses that.
    # No-op on non-Windows.
    extra_kwargs: dict = {}
    if sys.platform == "win32":
        extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            [bin_path, "--print", "--model", model],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            **extra_kwargs,
        )
    except subprocess.TimeoutExpired:
        log.warning("claude_cli: timed out after %ds", timeout_s)
        return ""
    except OSError as exc:
        log.warning("claude_cli: subprocess failed: %s", exc)
        return ""

    if result.returncode != 0:
        log.warning(
            "claude_cli: exit=%d stderr=%s",
            result.returncode,
            (result.stderr or "")[:200],
        )
        return ""
    return (result.stdout or "").strip()


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
    status_sink: dict[str, int | None] | None = None,
) -> str:
    """POST JSON, return extracted text or "" on failure.

    ``status_sink``: optional mutable dict. On an HTTPError the response's
    HTTP status code is written to ``status_sink["http_status"]`` so callers
    (e.g. the env-key rotator) can distinguish a real 429 from an empty-200
    success and avoid cooling a healthy key.
    """
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
        if status_sink is not None:
            status_sink["http_status"] = exc.code
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
    "claude_cli": _call_claude_cli,
    "claude-cli": _call_claude_cli,
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
        MEMORYMASTER_LLM_PROVIDER           — google|openai|anthropic|claude_cli|ollama (default: google)
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

    # Per-cycle budget gate. If a cycle_scope() is active and the calls cap
    # is already hit (or this provider's failure breaker is open), raise
    # LLMBudgetExceeded so the caller can record the abort visibly instead
    # of silently overspending. When no scope is active, this is a no-op
    # — preserves backwards-compat for callers outside run_cycle/wiki/daydream.
    llm_budget.check_before_call(primary_name)

    primary_response = primary_fn(prompt, text)
    llm_budget.record_call(
        primary_name,
        tokens=llm_budget.estimate_tokens(prompt, text, primary_response),
    )

    # Happy path: primary returned something that doesn't look like a quota error.
    if primary_response and not _looks_like_quota_error(primary_response):
        _FALLBACK_STATS["primary_ok"] += 1
        return primary_response

    # Treat empty or quota-shaped response as a provider failure for breaker purposes.
    # record_failure may raise if the per-provider failure cap is hit.
    llm_budget.record_failure(primary_name)

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

    # Budget gate for the fallback provider too.
    llm_budget.check_before_call(fallback_name)

    # Override MEMORYMASTER_LLM_MODEL to the fallback model for the duration of
    # the call WITHOUT mutating os.environ — a ContextVar override keeps the swap
    # private to this thread/task so concurrent callers (steward pool + MCP
    # rerank) never read the wrong model. A None override means "behave as if
    # MEMORYMASTER_LLM_MODEL is unset" so the fallback provider uses its own
    # default (the primary's model may be Gemini-specific).
    fallback_model = _env("MEMORYMASTER_LLM_FALLBACK_MODEL", "")
    prev_overrides = _ENV_OVERRIDES.get()
    new_overrides = dict(prev_overrides) if prev_overrides else {}
    new_overrides["MEMORYMASTER_LLM_MODEL"] = fallback_model if fallback_model else None
    token = _ENV_OVERRIDES.set(new_overrides)
    try:
        fallback_response = fallback_fn(prompt, text)
        llm_budget.record_call(
            fallback_name,
            tokens=llm_budget.estimate_tokens(prompt, text, fallback_response),
        )
    finally:
        _ENV_OVERRIDES.reset(token)

    if fallback_response and not _looks_like_quota_error(fallback_response):
        return fallback_response

    # Both failed — match legacy contract, return primary's (possibly empty) response.
    # Record fallback failure for breaker purposes; may raise if cap is hit.
    llm_budget.record_failure(fallback_name)
    return primary_response


def parse_json_response(text: str) -> list[dict]:
    """Parse LLM response as JSON array, handling markdown code fences and prose preambles.

    Resilient to four common LLM output shapes:
      1. raw JSON array: ``[{...}, {...}]``
      2. fenced JSON: ``\u0060\u0060\u0060json\\n[...]\\n\u0060\u0060\u0060``
      3. prose preamble + fenced: ``Here is the answer:\\n\u0060\u0060\u0060json\\n[...]\u0060\u0060\u0060``
      4. prose preamble + raw: ``The entities are: [...]``

    Strategy: try direct parse, then try fenced-strip from start, then fall back
    to greedy-extracting the largest ``[...]`` block in the text.
    """
    text = text.strip()
    # Shape 2 — strict fenced from the very start.
    if text.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\n?", "", text)
        stripped = re.sub(r"\n?```$", "", stripped)
        try:
            result = json.loads(stripped)
            return _coerce_to_list(result)
        except (json.JSONDecodeError, ValueError):
            pass

    # Shape 1 — direct parse.
    try:
        result = json.loads(text)
        return _coerce_to_list(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Shapes 3 + 4 — find the first ``\u0060\u0060\u0060json``/``\u0060\u0060\u0060`` block; if absent, the largest ``[...]``.
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced_match:
        try:
            result = json.loads(fenced_match.group(1).strip())
            return _coerce_to_list(result)
        except (json.JSONDecodeError, ValueError):
            pass

    # Greedy: first ``[`` to last matching ``]``. Defensive against prose with stray brackets.
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last > first:
        try:
            result = json.loads(text[first : last + 1])
            return _coerce_to_list(result)
        except (json.JSONDecodeError, ValueError):
            pass

    return []


def _coerce_to_list(result) -> list[dict]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []

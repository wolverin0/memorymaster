"""LLM Steward — automated claim extraction and curation.

Supports multiple LLM providers:
  - gemini (default, free tier available)
  - openai (GPT-4o, GPT-4o-mini, etc.)
  - anthropic (Claude Sonnet, Haiku, etc.)
  - Any OpenAI-compatible API (Ollama, Together, Groq, etc.)

Supports multi-key rotation for rate-limit resilience:
  - Round-robin key selection across multiple API keys
  - Automatic failover on 429/rate-limit errors
  - Per-key cooldown tracking with configurable duration
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

from memorymaster.stores._storage_shared import open_conn
from memorymaster.core import observability
from memorymaster.core.security import sanitize_claim_input

# P2 phase0 cycle cut: KeyRotator/DEFAULT_COOLDOWN_SECONDS now live in
# memorymaster.core.key_rotator (class RoundRobinKeyRotator). Re-exported here under
# the historical names for backward compatibility — external callers import
# KeyRotator from llm_steward.
from memorymaster.core.key_rotator import (  # noqa: F401 — re-export for compat
    DEFAULT_COOLDOWN_SECONDS,
    RoundRobinKeyRotator as KeyRotator,
)

log = logging.getLogger(__name__)


def _parse_api_keys(
    api_key: str = "",
    api_keys: str = "",
    env_var: str = "MEMORYMASTER_API_KEYS",
) -> list[str]:
    """Build a list of API keys from multiple sources.

    Priority (first non-empty wins):
      1. ``api_keys`` (comma-separated string, from --api-keys flag)
      2. env var ``MEMORYMASTER_API_KEYS`` (comma-separated)
      3. ``api_key`` (single key, from --api-key flag)
      4. env var ``MEMORYMASTER_API_KEY`` (single key)

    Returns a list of stripped, non-empty keys.
    """
    if api_keys:
        keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        if keys:
            return keys

    env_multi = os.environ.get(env_var, "")
    if env_multi:
        keys = [k.strip() for k in env_multi.split(",") if k.strip()]
        if keys:
            return keys

    if api_key:
        return [api_key.strip()]

    env_single = os.environ.get("MEMORYMASTER_API_KEY", "")
    if env_single:
        return [env_single.strip()]

    return [""]


# Provider configurations
PROVIDERS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        "default_model": "gemini-2.5-flash",
        "format": "gemini",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "format": "openai",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-haiku-4-5-20251001",
        "format": "anthropic",
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "default_model": "llama3.2",
        "format": "openai",
    },
    "custom": {
        "url": "",  # User must provide --base-url
        "default_model": "",
        "format": "openai",  # Assume OpenAI-compatible
    },
}

EXTRACT_PROMPT = """You are a memory curator for an AI coding agent system.
Given a raw memory text, extract structured knowledge claims.

For each distinct fact/decision/learning in the text, output a JSON object with:
- "subject": the entity (e.g. "ClawTrol API", "memorymaster", "SSH access")
- "predicate": the relationship (e.g. "runs_on", "requires", "is_located_at", "learned")
- "object_value": the specific value (e.g. "port 4001", "Python 3.10+", "10.0.0.1")
- "confidence": 0.0-1.0 how certain this fact is (1.0 = explicit statement, 0.5 = inferred, 0.2 = speculative)
- "action": "confirm" if this is a solid fact, "archive" if it's noise/chatter/duplicate/not useful

Rules:
- Extract ANY technical fact: config values, IPs, ports, commands, file paths, architecture decisions, bug fixes, lessons learned, workflows, API endpoints, tool behavior
- Also extract decisions ("we decided to use X"), status facts ("X is working/broken"), and process knowledge ("to do X you need Y")
- The text may be in any language — extract facts in English
- Skip ONLY pure greetings, filler ("ok", "let me check"), or content with zero factual information
- One text may contain 1-5 claims. Prefer extracting MORE claims over fewer.
- ONLY return empty array [] if the text truly has no factual content at all
- Output ONLY a JSON array, no markdown, no explanation

Text to analyze:
---
{text}
---

Output JSON array:"""


@dataclass
class ExtractionResult:
    claim_id: int
    original_text: str
    extractions: list[dict]
    action: str  # "confirm", "archive", "skip"
    raw_response: str


def _build_gemini_payload(prompt: str) -> bytes:
    return json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
    }).encode("utf-8")


def _build_openai_payload(prompt: str, model: str) -> bytes:
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }).encode("utf-8")


def _build_anthropic_payload(prompt: str, model: str) -> bytes:
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }).encode("utf-8")


def _extract_gemini_text(data: dict) -> str:
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            return parts[0].get("text", "")
    return ""


def _extract_openai_text(data: dict) -> str:
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _extract_anthropic_text(data: dict) -> str:
    content = data.get("content", [])
    if content:
        return content[0].get("text", "")
    return ""


def _build_request_for_key(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    base_url: str,
) -> tuple[str, bytes, dict[str, str]]:
    """Build URL, payload, and headers for a single API key."""
    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    fmt = cfg["format"]

    if base_url:
        url = base_url.rstrip("/")
        if fmt == "openai" and "/chat/completions" not in url:
            url += "/chat/completions"
    else:
        url = cfg["url"].format(model=model, api_key=api_key)

    headers: dict[str, str] = {"Content-Type": "application/json"}

    if fmt == "gemini":
        payload = _build_gemini_payload(prompt)
    elif fmt == "anthropic":
        payload = _build_anthropic_payload(prompt, model)
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        payload = _build_openai_payload(prompt, model)
        if api_key and provider != "ollama":
            headers["Authorization"] = f"Bearer {api_key}"

    return url, payload, headers


def _call_llm(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    base_url: str = "",
    max_retries: int = 2,
    key_rotator: KeyRotator | None = None,
) -> str:
    """Call any supported LLM provider and return text response.

    When ``key_rotator`` is provided, 429 errors trigger automatic key
    rotation instead of simple backoff. The ``api_key`` parameter is
    ignored in that case (keys come from the rotator).
    """
    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    fmt = cfg["format"]

    extractors = {
        "gemini": _extract_gemini_text,
        "openai": _extract_openai_text,
        "anthropic": _extract_anthropic_text,
    }
    extract_fn = extractors.get(fmt, _extract_openai_text)

    if key_rotator is not None:
        return _call_llm_with_rotation(
            provider, model, prompt, base_url,
            key_rotator, extract_fn, max_retries,
        )

    # Single-key path (backward compatible)
    url, payload, headers = _build_request_for_key(
        provider, api_key, model, prompt, base_url,
    )

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url, data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return extract_fn(data)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                wait = 4 * (2 ** attempt)
                log.info("Rate limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise
    return ""


def _call_llm_with_rotation(
    provider: str,
    model: str,
    prompt: str,
    base_url: str,
    rotator: KeyRotator,
    extract_fn: object,
    max_retries: int,
) -> str:
    """Call LLM with automatic key rotation on 429 errors.

    Tries up to ``max_retries + rotator.key_count`` total attempts,
    rotating to a fresh key on each 429.
    """
    max_attempts = max_retries + rotator.key_count
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        current_key = rotator.get_key()
        url, payload, headers = _build_request_for_key(
            provider, current_key, model, prompt, base_url,
        )

        try:
            req = urllib.request.Request(
                url, data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rotator.clear_cooldown(current_key)
                return extract_fn(data)  # type: ignore[operator]
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                rotator.mark_rate_limited(current_key)
                log.info(
                    "Key rate-limited (attempt %d/%d), rotating...",
                    attempt + 1, max_attempts,
                )
                continue
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            raise
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            raise

    if last_error is not None:
        raise last_error
    return ""


def _parse_extractions(raw: str) -> list[dict]:
    """Parse LLM response into list of extraction dicts."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return []


def extract_claim(
    provider: str, api_key: str, model: str,
    claim_id: int, text: str,
    base_url: str = "",
    key_rotator: KeyRotator | None = None,
    use_llm_provider: bool = False,
) -> ExtractionResult:
    """Extract structured claims from raw text using any LLM.

    When ``use_llm_provider`` is True, route through
    ``memorymaster.core.llm_provider.call_llm`` (honours MEMORYMASTER_LLM_PROVIDER,
    including the keyless ``claude_cli`` path) instead of the direct-HTTP
    ``_call_llm``. The prompt + parsing are identical either way.
    """
    snippet = text[:2000]
    if use_llm_provider:
        from memorymaster.core.llm_provider import call_llm as _provider_call_llm
        # call_llm joins as f"{prompt}\n\n{text}"; pass instructions (placeholder
        # stripped) as the prompt and the claim text as the second arg.
        instructions = EXTRACT_PROMPT.replace("{text}", "").rstrip()
        raw = _provider_call_llm(instructions, snippet)
    else:
        prompt = EXTRACT_PROMPT.replace("{text}", snippet)
        raw = _call_llm(provider, api_key, model, prompt, base_url, key_rotator=key_rotator)
    extractions = _parse_extractions(raw)

    if not extractions:
        action = "archive"
    else:
        actions = [e.get("action", "confirm") for e in extractions]
        action = "archive" if all(a == "archive" for a in actions) else "confirm"

    return ExtractionResult(
        claim_id=claim_id,
        original_text=text,
        extractions=extractions,
        action=action,
        raw_response=raw,
    )


def _auto_validate_claims(
    db_path: str,
    claim_ids: list[int],
    workspace_root: str = "",
    store: Any | None = None,
) -> dict:
    """Run deterministic validators on recently confirmed/extracted claims.

    Fetches the claims by ID and runs the deterministic validation job on
    them. Returns the validation stats dict from ``jobs.deterministic.run()``.

    P2 phase0 cycle cut: llm_steward must never import store_factory. Callers
    inject ``store``; when None, the default store for ``db_path`` is resolved
    via ``jobs.deterministic.open_store`` (which owns that dependency).
    """
    if not claim_ids:
        return {"checked": 0, "boosted": 0, "dropped": 0, "hard_conflicted": 0}

    from memorymaster.govern.jobs.deterministic import open_store, run as run_deterministic

    if store is None:
        store = open_store(db_path)
    ws = Path(workspace_root) if workspace_root else Path.cwd()

    # Fetch full Claim objects for the IDs that were just confirmed/created
    claims = []
    for cid in claim_ids:
        claim = store.get_claim(cid, include_citations=False)
        if claim is not None:
            claims.append(claim)

    if not claims:
        return {"checked": 0, "boosted": 0, "dropped": 0, "hard_conflicted": 0}

    result = run_deterministic(
        store,
        workspace_root=ws,
        limit=len(claims),
        revalidation_claims=claims,
        policy_mode="revalidation",
    )
    log.info(
        "Auto-validation: checked=%d boosted=%d dropped=%d hard_conflicted=%d",
        result.get("checked", 0),
        result.get("boosted", 0),
        result.get("dropped", 0),
        result.get("hard_conflicted", 0),
    )
    return result


def _archive_candidate_cas(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    version: int,
    replaced_by_claim_id: int | None = None,
) -> bool:
    """Archive a claim ONLY if it is still the 'candidate' at the read version.

    MED audit fix: the steward reads a batch of candidates, then issues raw
    status UPDATEs much later (after an LLM round-trip). A concurrent
    mark_superseded/transition_claim (which CAS on version) may have moved the
    claim out of 'candidate' in the meantime. A blind ``UPDATE ... WHERE id=?``
    would clobber that newer status (lost-update race). Guarding on the read
    version + the 'candidate' status means we only apply when nothing else
    touched the row; rowcount==0 signals the race was lost and the caller must
    skip its follow-up writes for this claim.
    """
    if replaced_by_claim_id is not None:
        cur = conn.execute(
            "UPDATE claims SET status = 'archived', replaced_by_claim_id = ?, "
            "updated_at = datetime('now'), version = version + 1 "
            "WHERE id = ? AND version = ? AND status = 'candidate'",
            (replaced_by_claim_id, claim_id, version),
        )
    else:
        cur = conn.execute(
            "UPDATE claims SET status = 'archived', version = version + 1 "
            "WHERE id = ? AND version = ? AND status = 'candidate'",
            (claim_id, version),
        )
    return cur.rowcount > 0


def _confirm_candidate_cas(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    version: int,
    subject: Any,
    predicate: Any,
    object_value: Any,
    confidence: float,
) -> bool:
    """Confirm a candidate claim with version CAS (see _archive_candidate_cas)."""
    cur = conn.execute(
        "UPDATE claims SET status = 'confirmed', subject = ?, predicate = ?, "
        "object_value = ?, confidence = ?, version = version + 1 "
        "WHERE id = ? AND version = ? AND status = 'candidate'",
        (subject, predicate, object_value, confidence, claim_id, version),
    )
    return cur.rowcount > 0


def run_steward(
    db_path: str,
    api_key: str,
    provider: str = "gemini",
    model: str = "",
    base_url: str = "",
    limit: int = 50,
    dry_run: bool = False,
    delay: float = 5.0,
    api_keys: list[str] | None = None,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    auto_validate: bool = True,
    workspace_root: str = "",
    scope: str | None = None,
    use_llm_provider: bool = False,
    store: Any | None = None,
) -> dict[str, Any]:
    """Process candidate claims through LLM extraction and curation.

    Args:
        api_keys: Optional list of API keys for round-robin rotation.
                  When provided, ``api_key`` is used as fallback only
                  if ``api_keys`` is empty.
        cooldown_seconds: How long a key stays on cooldown after a 429.
        auto_validate: When True (default), run deterministic validators
                       on newly confirmed claims after LLM extraction.
        workspace_root: Workspace root for deterministic file-path probes.
                        Defaults to cwd if empty.
        scope: When set, only candidates with this exact ``scope`` are
               processed (e.g. ``"project:memorymaster"``). When ``None``
               (default), candidates from all scopes are processed in id
               order, preserving the legacy behaviour.
        use_llm_provider: When True, extraction routes through
               ``llm_provider.call_llm`` (honours MEMORYMASTER_LLM_PROVIDER,
               including the keyless ``claude_cli`` OAuth path) instead of the
               direct-HTTP provider call. Lets the steward run without a raw
               API key when Claude Code CLI is available.
        store: Optional injected claims store used by auto-validation
               (P2 cycle cut: llm_steward must not import store_factory).
               When None, the default store for ``db_path`` is resolved via
               ``jobs.deterministic.open_store``.

    Returns summary stats dict.
    """
    # Build key rotator if multiple keys available
    effective_keys = api_keys if api_keys else [api_key] if api_key else [""]
    key_rotator: KeyRotator | None = None
    if len(effective_keys) > 1 or (api_keys is not None and len(effective_keys) == 1):
        key_rotator = KeyRotator(keys=effective_keys, cooldown_seconds=cooldown_seconds)
        log.info("Key rotation enabled with %d keys", key_rotator.key_count)

    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    if not model:
        model = cfg["default_model"]

    # MED audit fix: the steward holds write transactions across time.sleep(delay)
    # between LLM calls. Without WAL + a busy_timeout this writer blocks (and is
    # blocked by) every other writer on the shared DB, and busy_timeout=0 turns a
    # momentary lock into an immediate "database is locked" error → lost writes.
    # open_conn supplies WAL + busy_timeout=15000 (uniform fleet envelope).
    conn = open_conn(db_path)

    # Optional scope filter: when set, only that exact scope's candidates are
    # processed. When None, behaviour is unchanged (all scopes, id order).
    if scope is not None:
        candidates = conn.execute(
            "SELECT id, text, scope, version FROM claims WHERE status = 'candidate' "
            "AND text IS NOT NULL AND scope = ? ORDER BY id LIMIT ?",
            (scope, limit),
        ).fetchall()
    else:
        candidates = conn.execute(
            "SELECT id, text, scope, version FROM claims WHERE status = 'candidate' "
            "AND text IS NOT NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    confirmed_claim_ids: list[int] = []

    from memorymaster.govern.candidate_dedupe import (
        find_near_duplicate,
        is_enabled as _dedupe_enabled,
        is_shadow_mode as _dedupe_shadow,
    )

    dedupe_on = _dedupe_enabled()
    dedupe_shadow = _dedupe_shadow()

    stats: dict[str, Any] = {
        "total": len(candidates),
        "confirmed": 0,
        "archived": 0,
        "errors": 0,
        "claims_extracted": 0,
        "claims_filtered_sensitive": 0,
        "provider": provider,
        "model": model,
        "key_count": key_rotator.key_count if key_rotator else 1,
        "dedupe_enabled": dedupe_on,
        "dedupe_shadow": dedupe_shadow,
        "dedupe_archived": 0,
        "dedupe_would_archive": 0,
        "dedupe_passthrough": 0,
        "dedupe_score_sum": 0.0,
        "dedupe_score_count": 0,
        "results": [],
    }

    for row in candidates:
        claim_id = row["id"]
        text = row["text"] or ""
        scope = row["scope"] or ""
        claim_version = row["version"]

        if len(text.strip()) < 10:
            if not dry_run:
                if not _archive_candidate_cas(
                    conn, claim_id=claim_id, version=claim_version,
                ):
                    # Another writer moved this claim out of 'candidate' first.
                    log.info("Skipping #%d: no longer candidate (race)", claim_id)
                    continue
                conn.execute(
                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                    "VALUES (?, 'transition', 'auto-archived: too short', datetime('now'))",
                    (claim_id,),
                )
            stats["archived"] += 1
            continue

        if dedupe_on and scope:
            dedupe = find_near_duplicate(
                conn,
                candidate_id=claim_id,
                candidate_text=text,
                candidate_scope=scope,
            )
            if dedupe.jaccard_score is not None:
                stats["dedupe_score_sum"] += dedupe.jaccard_score
                stats["dedupe_score_count"] += 1
            if dedupe.action == "archive":
                stats["results"].append({
                    "claim_id": claim_id,
                    "dedupe": {
                        "canonical_id": dedupe.canonical_claim_id,
                        "score": dedupe.jaccard_score,
                        "reason": dedupe.reason,
                        "would_archive": dedupe_shadow,
                    },
                })
                if dedupe_shadow:
                    # F-8 fix (overnight audit, mm-d24c context): treat
                    # would_archive as terminal in shadow mode. Previously this
                    # also incremented passthrough AND fell through to LLM
                    # extract — operator reading the stats saw inflated
                    # passthrough numbers AND wasted Haiku tokens re-processing
                    # claims that were going to be archived anyway. Continue
                    # gives shadow mode the same flow as active mode minus
                    # the actual archive write — exactly the "what would
                    # have happened" semantics shadow is supposed to model.
                    stats["dedupe_would_archive"] += 1
                    continue
                else:
                    if not dry_run:
                        if not _archive_candidate_cas(
                            conn, claim_id=claim_id, version=claim_version,
                            replaced_by_claim_id=dedupe.canonical_claim_id,
                        ):
                            log.info(
                                "Skipping dedupe-archive #%d: no longer candidate (race)",
                                claim_id,
                            )
                            continue
                        conn.execute(
                            "UPDATE claims SET access_count = COALESCE(access_count, 0) + 1, "
                            "updated_at = datetime('now') WHERE id = ?",
                            (dedupe.canonical_claim_id,),
                        )
                        conn.execute(
                            "INSERT INTO events (claim_id, event_type, details, created_at) "
                            "VALUES (?, 'transition', ?, datetime('now'))",
                            (claim_id, f"dedupe-archived: {dedupe.reason}"),
                        )
                    stats["dedupe_archived"] += 1
                    stats["archived"] += 1
                    continue
            else:
                stats["dedupe_passthrough"] += 1

        try:
            result = extract_claim(
                provider, api_key, model, claim_id, text, base_url,
                key_rotator=key_rotator,
                use_llm_provider=use_llm_provider,
            )
        except Exception as e:
            log.warning("LLM error for claim #%d: %s", claim_id, e)
            stats["errors"] += 1
            stats["results"].append({"claim_id": claim_id, "error": str(e)})
            continue

        if result.action == "archive":
            if not dry_run:
                if not _archive_candidate_cas(
                    conn, claim_id=claim_id, version=claim_version,
                ):
                    log.info("Skipping llm-archive #%d: no longer candidate (race)", claim_id)
                    continue
                conn.execute(
                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                    "VALUES (?, 'transition', 'llm-archived: no useful claims', datetime('now'))",
                    (claim_id,),
                )
            stats["archived"] += 1
        else:
            confirm_extractions = [
                e for e in result.extractions if e.get("action") != "archive"
            ]
            if confirm_extractions:
                first = confirm_extractions[0]
                f_subj = first.get("subject")
                f_pred = first.get("predicate")
                f_obj = first.get("object_value")
                f_conf = min(1.0, max(0.0, float(first.get("confidence", 0.7))))
                if not dry_run:
                    # F-1 fix (mm-d53d, 2026-05-04): scope must match the source
                    # claim's scope, not the literal 'project' which never appears
                    # in real data (every claim uses 'project:<slug>'). Same bug
                    # class as mm-5e6d's broken FTS5-hash query — query that
                    # filters on a hardcoded value the data never contains.
                    dup = conn.execute(
                        "SELECT id FROM claims WHERE subject = ? AND predicate = ? "
                        "AND status = 'confirmed' AND scope = ? AND id != ? LIMIT 1",
                        (f_subj, f_pred, scope, claim_id),
                    ).fetchone()
                    if dup:
                        if not _archive_candidate_cas(
                            conn, claim_id=claim_id, version=claim_version,
                        ):
                            log.info(
                                "Skipping llm-confirm-dup #%d: no longer candidate (race)",
                                claim_id,
                            )
                            continue
                        conn.execute(
                            "UPDATE claims SET object_value = ?, confidence = MAX(confidence, ?), "
                            "updated_at = datetime('now') WHERE id = ?",
                            (f_obj, f_conf, dup["id"]),
                        )
                        confirmed_claim_ids.append(dup["id"])
                    else:
                        if not _confirm_candidate_cas(
                            conn, claim_id=claim_id, version=claim_version,
                            subject=f_subj, predicate=f_pred,
                            object_value=f_obj, confidence=f_conf,
                        ):
                            log.info(
                                "Skipping llm-confirm #%d: no longer candidate (race)",
                                claim_id,
                            )
                            continue
                        confirmed_claim_ids.append(claim_id)
                    conn.execute(
                        "INSERT INTO events (claim_id, event_type, details, created_at) "
                        "VALUES (?, 'transition', ?, datetime('now'))",
                        (claim_id, f"llm-confirmed: {f_subj}/{f_pred}"),
                    )

                    for extra in confirm_extractions[1:]:
                        subj = extra.get("subject")
                        pred = extra.get("predicate")
                        obj_val = extra.get("object_value")
                        conf = min(1.0, max(0.0, float(extra.get("confidence", 0.7))))

                        # F-1 fix (mm-d53d, 2026-05-04): same as above — scope
                        # is the source claim's scope, not the literal 'project'.
                        existing = conn.execute(
                            "SELECT id FROM claims WHERE subject = ? AND predicate = ? "
                            "AND status = 'confirmed' AND scope = ? LIMIT 1",
                            (subj, pred, scope),
                        ).fetchone()

                        if existing:
                            conn.execute(
                                "UPDATE claims SET object_value = ?, confidence = MAX(confidence, ?), "
                                "updated_at = datetime('now') WHERE id = ?",
                                (obj_val, conf, existing["id"]),
                            )
                            confirmed_claim_ids.append(existing["id"])
                        else:
                            # Sensitivity firewall — this cycle insert bypasses
                            # svc.ingest, so run the SAME canonical filter over the
                            # exact fields about to be persisted (default-deny). A
                            # credential in an LLM extraction must never be stored.
                            sanitized = sanitize_claim_input(
                                text=text[:200],
                                object_value=obj_val,
                                citations=[],
                                subject=subj,
                                predicate=pred,
                            )
                            if sanitized.is_sensitive:
                                observability.bump_claim_filtered(
                                    "steward_extraction_sensitive"
                                )
                                log.warning(
                                    "steward: skipped sensitive extraction from "
                                    "claim #%d [REDACTED findings=%s]",
                                    claim_id,
                                    ",".join(sanitized.findings),
                                )
                                stats["claims_filtered_sensitive"] += 1
                                continue
                            try:
                                # F-1 fix: use source claim's scope here too, so
                                # newly-inserted extra extractions are reachable
                                # from the same recall path as their source.
                                cursor = conn.execute(
                                    "INSERT INTO claims (text, subject, predicate, object_value, "
                                    "confidence, status, scope, created_at, updated_at) "
                                    "VALUES (?, ?, ?, ?, ?, 'confirmed', ?, datetime('now'), datetime('now'))",
                                    (text[:200], subj, pred, obj_val, conf, scope),
                                )
                                new_id = cursor.lastrowid
                                if new_id is None:
                                    raise RuntimeError("SQLite did not return a claim id.")
                                conn.execute(
                                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                                    "VALUES (?, 'transition', ?, datetime('now'))",
                                    (new_id, f"llm-extracted from claim #{claim_id}"),
                                )
                                stats["claims_extracted"] += 1
                                confirmed_claim_ids.append(new_id)
                            except sqlite3.IntegrityError:
                                pass

                stats["confirmed"] += 1
            else:
                if not dry_run:
                    if not _archive_candidate_cas(
                        conn, claim_id=claim_id, version=claim_version,
                    ):
                        log.info(
                            "Skipping llm-empty-archive #%d: no longer candidate (race)",
                            claim_id,
                        )
                        continue
                stats["archived"] += 1

        stats["results"].append({
            "claim_id": claim_id,
            "action": result.action,
            "extractions": len(result.extractions),
            "preview": text[:80],
        })

        # MED audit fix: commit each claim's writes BEFORE sleeping. Previously
        # the open write transaction (every claim's UPDATE/INSERT) was held across
        # time.sleep(delay), blocking all other writers for the whole delay window.
        # Releasing the lock before the sleep keeps the steward cooperative.
        if not dry_run:
            conn.commit()

        time.sleep(delay)

    if not dry_run:
        conn.commit()
    conn.close()

    # Auto-validate newly confirmed claims with deterministic probes
    if auto_validate and not dry_run and confirmed_claim_ids:
        try:
            unique_ids = list(dict.fromkeys(confirmed_claim_ids))
            validation_stats = _auto_validate_claims(
                db_path, unique_ids, workspace_root, store=store,
            )
            stats["auto_validation"] = validation_stats
        except Exception as e:
            log.warning("Auto-validation failed (non-fatal): %s", e)
            stats["auto_validation"] = {"error": str(e)}

    if stats["dedupe_score_count"] > 0:
        stats["dedupe_avg_jaccard"] = (
            stats["dedupe_score_sum"] / stats["dedupe_score_count"]
        )
    else:
        stats["dedupe_avg_jaccard"] = None
    del stats["dedupe_score_sum"]
    del stats["dedupe_score_count"]

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="LLM Steward for MemoryMaster — automated claim extraction and curation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Gemini (free)
  memorymaster-steward --db memory.db --provider gemini --api-key YOUR_KEY

  # OpenAI
  memorymaster-steward --db memory.db --provider openai --api-key sk-... --model gpt-4o-mini

  # Anthropic
  memorymaster-steward --db memory.db --provider anthropic --api-key sk-ant-... --model claude-haiku-4-5-20251001

  # Ollama (local, no key needed)
  memorymaster-steward --db memory.db --provider ollama --model llama3.2

  # Any OpenAI-compatible API
  memorymaster-steward --db memory.db --provider custom --base-url https://api.together.xyz/v1 --api-key KEY --model meta-llama/Llama-3-8b

  # Multi-key rotation (round-robin with failover on rate limits)
  memorymaster-steward --db memory.db --provider gemini --api-keys KEY1,KEY2,KEY3

  # Via environment variable
  export MEMORYMASTER_API_KEYS=KEY1,KEY2,KEY3
  memorymaster-steward --db memory.db --provider gemini
""",
    )
    parser.add_argument("--db", required=True, help="Path to memorymaster SQLite DB")
    parser.add_argument("--api-key", default="", help="API key for the LLM provider")
    parser.add_argument(
        "--api-keys", default="",
        help="Comma-separated API keys for round-robin rotation (also: MEMORYMASTER_API_KEYS env var)",
    )
    parser.add_argument(
        "--cooldown", type=float, default=DEFAULT_COOLDOWN_SECONDS,
        help=f"Cooldown seconds for rate-limited keys (default: {DEFAULT_COOLDOWN_SECONDS})",
    )
    parser.add_argument(
        "--provider", default="gemini",
        choices=["gemini", "openai", "anthropic", "ollama", "custom"],
        help="LLM provider (default: gemini)",
    )
    parser.add_argument("--model", default="", help="Model name (uses provider default if omitted)")
    parser.add_argument("--base-url", default="", help="Custom API base URL (for custom/ollama providers)")
    parser.add_argument("--limit", type=int, default=50, help="Max claims to process (default: 50)")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds between API calls (default: 5.0)")
    parser.add_argument("--dry-run", action="store_true", help="Don't modify DB, just show what would happen")
    parser.add_argument(
        "--no-auto-validate", action="store_true",
        help="Disable automatic deterministic validation of newly confirmed claims",
    )
    parser.add_argument(
        "--workspace-root", default="",
        help="Workspace root for file-path validation probes (default: cwd)",
    )
    parser.add_argument(
        "--scope", default=None,
        help="Only process candidates with this exact scope (e.g. project:memorymaster). "
             "When omitted, candidates from all scopes are processed in id order.",
    )
    parser.add_argument(
        "--use-llm-provider", action="store_true",
        help="Route extraction through llm_provider.call_llm (honours "
             "MEMORYMASTER_LLM_PROVIDER incl. the keyless claude_cli OAuth path) "
             "instead of the direct-HTTP provider call.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    resolved_keys = _parse_api_keys(
        api_key=args.api_key,
        api_keys=args.api_keys,
    )

    stats = run_steward(
        args.db,
        api_key=resolved_keys[0] if len(resolved_keys) == 1 else "",
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        limit=args.limit,
        dry_run=args.dry_run,
        delay=args.delay,
        api_keys=resolved_keys if len(resolved_keys) > 1 else None,
        cooldown_seconds=args.cooldown,
        auto_validate=not args.no_auto_validate,
        workspace_root=args.workspace_root,
        scope=args.scope,
        use_llm_provider=args.use_llm_provider,
    )

    print(f"\n{'='*50}")
    print(f"LLM Steward Results ({stats['provider']}/{stats['model']})")
    print(f"{'='*50}")
    key_count = stats.get('key_count', 1)
    if key_count > 1:
        print(f"API keys:   {key_count} (round-robin rotation)")
    print(f"Processed:  {stats['total']}")
    print(f"Confirmed:  {stats['confirmed']}")
    print(f"Archived:   {stats['archived']}")
    print(f"New claims: {stats['claims_extracted']}")
    print(f"Errors:     {stats['errors']}")
    av = stats.get("auto_validation")
    if av and not av.get("error"):
        print("\nAuto-validation:")
        print(f"  Checked:        {av.get('checked', 0)}")
        print(f"  Boosted:        {av.get('boosted', 0)}")
        print(f"  Dropped:        {av.get('dropped', 0)}")
        print(f"  Hard conflicts: {av.get('hard_conflicted', 0)}")
    elif av and av.get("error"):
        print(f"\nAuto-validation: FAILED — {av['error']}")
    print()
    for r in stats["results"]:
        if "error" in r:
            print(f"  #{r['claim_id']}: ERROR - {r['error']}")
        else:
            print(f"  #{r['claim_id']}: {r['action']} ({r['extractions']} extractions) — {r.get('preview','')}")


if __name__ == "__main__":
    main()

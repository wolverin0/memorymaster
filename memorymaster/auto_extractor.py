"""Extract structured claims from unstructured text using LLM.

Takes raw text (conversation logs, docs, notes) and extracts
individual facts as separate claims with proper typing.

Uses Ollama at http://192.168.100.155:11434 with deepseek-coder-v2:16b
(same endpoint as auto_resolver.py).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://192.168.100.155:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"

_EXTRACTION_PROMPT = """\
Extract individual facts from the following text. Return a JSON array where each \
element is one discrete fact with these fields:
  - "text": the full plain-English statement of the fact
  - "claim_type": one of "fact", "decision", "constraint", "preference", "status", "relationship"
  - "subject": the main entity the fact is about (short noun phrase)
  - "predicate": the relationship or property being described (verb phrase)
  - "object_value": the value, outcome, or object of the predicate

Rules:
- One fact per object — do not bundle multiple facts into one entry
- Omit entries that are opinions, greetings, or questions with no factual content
- subject/predicate/object_value may be null if they cannot be inferred
- Return only the JSON array, no explanation, no markdown fences

Text to analyse:
{text}"""


def _call_ollama(prompt: str, base_url: str, model: str) -> list[dict[str, Any]]:
    """POST to Ollama /api/chat and parse the JSON array response."""
    url = base_url.rstrip("/") + "/api/chat"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 2048},
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            raw = result.get("message", {}).get("content", "").strip()
            # Strip optional markdown fences the model might add despite instructions
            if raw.startswith("```"):
                lines = raw.splitlines()
                lines = [ln for ln in lines if not ln.strip().startswith("```")]
                raw = "\n".join(lines).strip()
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            logger.warning("auto_extractor: LLM returned non-list JSON, ignoring")
            return []
    except urllib.error.URLError as exc:
        logger.error("auto_extractor: Ollama unreachable at %s: %s", base_url, exc)
        return []
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("auto_extractor: failed to parse LLM response: %s", exc)
        return []


def _normalise_claim(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure required fields exist and values are strings or None."""
    text = str(raw.get("text") or "").strip()
    if not text:
        return {}
    return {
        "text": text,
        "claim_type": str(raw.get("claim_type") or "fact").strip() or "fact",
        "subject": (str(raw.get("subject")).strip() or None) if raw.get("subject") else None,
        "predicate": (str(raw.get("predicate")).strip() or None) if raw.get("predicate") else None,
        "object_value": (str(raw.get("object_value")).strip() or None) if raw.get("object_value") else None,
    }


def extract_claims_from_text(
    text: str,
    source: str,
    scope: str = "project",
    base_url: str = "",
    model: str = "",
) -> list[dict[str, Any]]:
    """Use LLM to extract structured claims from unstructured text.

    Args:
        text: Raw input text (conversation log, document, notes, …).
        source: Citation source label for provenance (e.g. "conversation", "docs/readme.md").
        scope: Claim scope tag applied to every extracted claim.
        base_url: Ollama base URL (defaults to DEFAULT_OLLAMA_URL or $OLLAMA_URL).
        model: Model name (defaults to DEFAULT_MODEL or $EXTRACTOR_LLM_MODEL).

    Returns:
        List of dicts with keys: text, claim_type, subject, predicate, object_value,
        source, scope.  Empty list on failure.
    """
    if not text.strip():
        return []

    resolved_url = (base_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    resolved_model = model or os.environ.get("EXTRACTOR_LLM_MODEL") or DEFAULT_MODEL

    prompt = _EXTRACTION_PROMPT.format(text=text.strip())
    raw_claims = _call_ollama(prompt, resolved_url, resolved_model)

    results: list[dict[str, Any]] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        normalised = _normalise_claim(raw)
        if not normalised:
            continue
        results.append({**normalised, "source": source, "scope": scope})

    logger.info(
        "auto_extractor: extracted %d claims from %d chars of text (source=%s)",
        len(results),
        len(text),
        source,
    )
    return results

"""LLM-powered automatic conflict resolution.

When two claims contradict (same subject/predicate, different object_value),
asks an LLM to evaluate which one has stronger evidence and should be kept.

Uses Ollama or any OpenAI-compatible endpoint. The loser gets superseded,
not deleted — preserving full audit trail.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from memorymaster.lifecycle import transition_claim
from memorymaster.models import Claim

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://192.168.100.155:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"

RESOLUTION_PROMPT = """You are a memory quality evaluator. Two claims contradict each other.
Decide which one should be KEPT based on:
1. Recency (newer information is usually more accurate)
2. Specificity (more detailed/specific claims are better)
3. Confidence score (higher = more validated)
4. Citation quality (real file paths > vague sources)

Claim A (id={id_a}):
  Text: {text_a}
  Confidence: {conf_a}
  Updated: {updated_a}
  Citations: {cites_a}

Claim B (id={id_b}):
  Text: {text_b}
  Confidence: {conf_b}
  Updated: {updated_b}
  Citations: {cites_b}

Return JSON only: {{"winner": "A" or "B", "reason": "brief explanation"}}"""


def _llm_evaluate(prompt: str, model: str = "", base_url: str = "") -> dict:
    """Call LLM and parse JSON response.

    Returns empty dict {} if LLM returns invalid JSON or connection fails.
    """
    url = (base_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    mdl = model or os.environ.get("RESOLVER_LLM_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 200},
    }).encode()

    req = urllib.request.Request(
        f"{url}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            raw = result.get("message", {}).get("content", "")
            # Parse JSON from response
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                text = "\n".join(lines)
            try:
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    logger.warning("LLM returned non-dict JSON: %s", type(parsed))
                    return {}
                return parsed
            except json.JSONDecodeError as exc:
                logger.warning("LLM returned invalid JSON: %s (text: %s)", exc, text[:100])
                return {}
    except Exception as exc:
        logger.warning("LLM conflict evaluation failed: %s", exc)
        return {}


def _cite_summary(claim: Claim) -> str:
    """Summarize citations for LLM prompt."""
    if not claim.citations:
        return "(none)"
    return "; ".join(f"{c.source}{f':{c.locator}' if c.locator else ''}" for c in claim.citations[:3])


def resolve_conflict_pair(
    store,
    claim_a: Claim,
    claim_b: Claim,
) -> dict[str, Any]:
    """Use LLM to evaluate and resolve a conflict between two claims.

    The loser is transitioned to 'superseded' with the winner as replaced_by.
    """
    prompt = RESOLUTION_PROMPT.format(
        id_a=claim_a.id,
        text_a=claim_a.text[:500],
        conf_a=f"{claim_a.confidence:.2f}",
        updated_a=claim_a.updated_at,
        cites_a=_cite_summary(claim_a),
        id_b=claim_b.id,
        text_b=claim_b.text[:500],
        conf_b=f"{claim_b.confidence:.2f}",
        updated_b=claim_b.updated_at,
        cites_b=_cite_summary(claim_b),
    )

    decision = _llm_evaluate(prompt)
    winner_letter = decision.get("winner", "").upper()
    reason = decision.get("reason", "llm_evaluation")

    if winner_letter not in ("A", "B"):
        # LLM couldn't decide — keep both, mark as unresolved
        return {"resolved": False, "reason": "llm_undecided"}

    winner = claim_a if winner_letter == "A" else claim_b
    loser = claim_b if winner_letter == "A" else claim_a

    try:
        transition_claim(
            store,
            claim_id=loser.id,
            to_status="superseded",
            reason=f"llm_conflict_resolution: {reason}",
            event_type="validator",
            replaced_by_claim_id=winner.id,
        )
        return {
            "resolved": True,
            "winner_id": winner.id,
            "loser_id": loser.id,
            "reason": reason,
        }
    except Exception as exc:
        logger.warning("Failed to resolve conflict %d vs %d: %s", claim_a.id, claim_b.id, exc)
        return {"resolved": False, "reason": str(exc)}


def _resolve_group_pairs(store, claims: list[Claim], limit: int) -> tuple[int, int, int]:
    """Resolve all pairs within a conflict group. Returns (evaluated, resolved, failed)."""
    evaluated = 0
    resolved = 0
    failed = 0

    for i in range(len(claims) - 1):
        if evaluated >= limit:
            break
        # Re-fetch to check if still conflicted (might have been resolved in earlier pair)
        a = store.get_claim(claims[i].id, include_citations=True)
        b = store.get_claim(claims[i + 1].id, include_citations=True)
        if a is None or b is None or a.status != "conflicted" or b.status != "conflicted":
            continue

        result = resolve_conflict_pair(store, a, b)
        evaluated += 1
        if result.get("resolved"):
            resolved += 1
            logger.info(
                "Resolved conflict: winner=%d, loser=%d (%s)",
                result["winner_id"], result["loser_id"], result["reason"],
            )
        else:
            failed += 1

    return evaluated, resolved, failed


def auto_resolve_conflicts(store, *, limit: int = 50) -> dict[str, int]:
    """Find and resolve conflicted claims using LLM evaluation.

    Groups conflicted claims by (subject, predicate, scope) tuple,
    then asks the LLM to pick a winner in each group.

    Returns immediately with empty counts if no conflicted claims found.
    """
    try:
        conflicted = store.find_by_status("conflicted", limit=limit * 2, include_citations=True)
    except Exception as exc:
        logger.error("Failed to find conflicted claims: %s", exc)
        return {"pairs_evaluated": 0, "resolved": 0, "failed": 0}

    if not conflicted:
        logger.debug("auto_resolve_conflicts: no conflicted claims found")
        return {"pairs_evaluated": 0, "resolved": 0, "failed": 0}

    # Group by tuple
    groups: dict[tuple, list[Claim]] = {}
    for c in conflicted:
        if c.subject and c.predicate:
            key = (c.subject, c.predicate, c.scope)
            groups.setdefault(key, []).append(c)

    evaluated = 0
    resolved = 0
    failed = 0

    for _key, claims in groups.items():
        if len(claims) < 2 or evaluated >= limit:
            continue
        group_eval, group_res, group_fail = _resolve_group_pairs(store, claims, limit - evaluated)
        evaluated += group_eval
        resolved += group_res
        failed += group_fail

    return {"pairs_evaluated": evaluated, "resolved": resolved, "failed": failed}

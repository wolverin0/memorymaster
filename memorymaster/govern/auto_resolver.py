"""LLM-powered automatic conflict resolution.

When two claims contradict (same subject/predicate, different object_value),
asks an LLM to evaluate which one has stronger evidence and should be kept.

Provider routing goes through `memorymaster.core.llm_provider.call_llm`, which
honors `MEMORYMASTER_LLM_PROVIDER` (claude_cli / google / openai / anthropic
/ ollama) instead of the previous hardcoded Ollama-only path. The loser
gets superseded, not deleted — preserving full audit trail.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from memorymaster.stores._storage_shared import ConcurrentModificationError
from memorymaster.core.llm_provider import call_llm
from memorymaster.core.models import Claim

logger = logging.getLogger(__name__)

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
    """Ask the configured LLM to pick the winner and parse its JSON response.

    Returns empty dict {} if the LLM returns invalid JSON or the call fails.
    The `model` and `base_url` kwargs are kept for backwards compatibility but
    are no longer consulted — provider routing is centralized in llm_provider.
    """
    try:
        raw = call_llm(prompt, "")
    except Exception as exc:
        logger.warning("LLM conflict evaluation failed: %s", exc)
        return {}

    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.split("\n") if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("LLM returned invalid JSON: %s (text: %s)", exc, text[:100])
        return {}

    if not isinstance(parsed, dict):
        logger.warning("LLM returned non-dict JSON: %s", type(parsed))
        return {}
    return parsed


def _cite_summary(claim: Claim) -> str:
    """Summarize citations for LLM prompt."""
    if not claim.citations:
        return "(none)"
    return "; ".join(f"{c.source}{f':{c.locator}' if c.locator else ''}" for c in claim.citations[:3])


def _lost_race_result(
    winner: Claim,
    loser: Claim,
    replacement_id,
) -> dict[str, Any]:
    return {
        "resolved": False,
        "reason": "lost_race",
        "winner_id": winner.id,
        "loser_id": loser.id,
        "current_replacement_id": replacement_id,
    }


def _apply_conflict_resolution(
    store,
    winner: Claim,
    loser: Claim,
    reason: str,
) -> dict[str, Any]:
    try:
        store.mark_superseded(
            loser.id,
            winner.id,
            f"llm_conflict_resolution: {reason}",
        )
        updated = store.get_claim(loser.id, include_citations=False)
        replacement_id = getattr(updated, "replaced_by_claim_id", None)
        if replacement_id != winner.id:
            return _lost_race_result(winner, loser, replacement_id)
        return {
            "resolved": True,
            "winner_id": winner.id,
            "loser_id": loser.id,
            "reason": reason,
        }
    except ConcurrentModificationError as exc:
        current = store.get_claim(loser.id, include_citations=False)
        if current is not None and current.status == "superseded":
            return _lost_race_result(winner, loser, current.replaced_by_claim_id)
        logger.warning("Failed to resolve conflict %d vs %d: %s", winner.id, loser.id, exc)
        return {"resolved": False, "reason": str(exc)}
    except Exception as exc:
        logger.warning("Failed to resolve conflict %d vs %d: %s", winner.id, loser.id, exc)
        return {"resolved": False, "reason": str(exc)}


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
    return _apply_conflict_resolution(store, winner, loser, reason)


def _resolve_group_pairs(store, claims: list[Claim], limit: int) -> tuple[int, int, int]:
    """Resolve a conflict group down to a single survivor. Returns (evaluated, resolved, failed).

    MED audit fix: the previous loop only compared ADJACENT claims
    (i vs i+1). In a 3+ group that left non-adjacent claims still
    'conflicted' even though the whole group was reported resolved — a
    claim that lost to its neighbour was never compared to the eventual
    survivor. We instead carry a running winner and judge it against each
    remaining conflicted claim, so every loser is superseded by the actual
    group survivor and at most one 'conflicted' claim remains.
    """
    evaluated = 0
    resolved = 0
    failed = 0

    # Running winner: the first still-conflicted claim, refreshed from store.
    winner: Claim | None = None

    for claim in claims:
        if evaluated >= limit:
            break

        contender = store.get_claim(claim.id, include_citations=True)
        if contender is None or contender.status != "conflicted":
            continue

        if winner is None:
            winner = contender
            continue

        # Re-fetch the running winner: an earlier pair (or another writer)
        # may have changed its status/version since we last saw it.
        winner = store.get_claim(winner.id, include_citations=True)
        if winner is None or winner.status != "conflicted":
            # Running winner is gone; the current contender becomes the new winner.
            winner = contender
            continue

        result = resolve_conflict_pair(store, winner, contender)
        evaluated += 1
        if result.get("resolved"):
            resolved += 1
            logger.info(
                "Resolved conflict: winner=%d, loser=%d (%s)",
                result["winner_id"], result["loser_id"], result["reason"],
            )
            # The survivor (whichever side the LLM kept) carries forward.
            survivor_id = result["winner_id"]
            survivor = store.get_claim(survivor_id, include_citations=True)
            winner = survivor if survivor is not None else contender
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

"""Near-duplicate live claims proposed to the steward with rapidfuzz (T-0773).

The steward's Jaccard dedupe only catches near-identical wording (>= 0.85 token
overlap) and only for candidates. Paraphrases of the same fact among confirmed
claims survive, for example "Committing secrets is a critical security failure..."
next to "Committing secrets directly into the repository constitutes a critical
security failure...".

Measured read-only on the live DB (2026-10-03, 4,781 confirmed+candidate claims):
token_set_ratio >= 85 within a scope found 118 pairs beyond Jaccard, with a
precision of 5/30. Almost every false pair was one template with different
identifiers (``pm_lead_arrival`` vs ``pm_lead_tracking``, ``/api/alerts`` vs
``/api/errors``, ``PR #36`` vs ``PR #37``). Rejecting pairs whose differing words
include an identifier left 9 pairs, all 9 true duplicates.

Proposals only: a ``policy_decision`` event ``steward_proposal:fuzzy_near_duplicate``
on the newer claim, never a status change, never auto-approved.
"""
from __future__ import annotations

import collections
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

ENABLED_ENV = "MEMORYMASTER_FUZZY_DEDUP"
PER_CYCLE_ENV = "MEMORYMASTER_FUZZY_DEDUP_PER_CYCLE"
THRESHOLD = 85.0
JACCARD_ALREADY_HANDLED = 0.85
MIN_TEXT_CHARS = 20
LIVE_STATUSES = ("confirmed", "candidate")
SOURCE = "fuzzy"
DECISION = "near_duplicate"

_WORD = re.compile(r"[\w/.#:-]+", re.UNICODE)
_PLAIN_TOKEN = re.compile(r"\w+", re.UNICODE)
_IDENTIFIER = re.compile(r"[_/.#:]|\d")


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    keep_id: int
    newer_id: int
    scope: str
    score: float

    @property
    def ref(self) -> str:
        return f"{self.keep_id}~{self.newer_id}"


def enabled(environ=os.environ) -> bool:
    return (environ.get(ENABLED_ENV) or "1").strip().lower() not in {"0", "false", "no", "off"}


def per_cycle(environ=os.environ) -> int:
    try:
        return max(0, int(environ.get(PER_CYCLE_ENV) or 50))
    except ValueError:
        return 50


def _words(text: str) -> set[str]:
    return {w.strip(".:,") for w in _WORD.findall(text.lower()) if w.strip(".:,")}


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(_PLAIN_TOKEN.findall(a.lower())), set(_PLAIN_TOKEN.findall(b.lower()))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def differs_by_identifier(a: str, b: str) -> bool:
    """True when the words that differ include an identifier: same template, different entity."""
    return any(_IDENTIFIER.search(word) for word in _words(a) ^ _words(b))


def find_pairs(rows: Iterable[Sequence[Any]], *, threshold: float = THRESHOLD) -> list[NearDuplicate]:
    """``rows`` are ``(id, scope, text)``; pairs are compared only within one scope."""
    from rapidfuzz import fuzz, process

    by_scope: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for claim_id, scope, text in rows:
        if text and len(text) >= MIN_TEXT_CHARS:
            by_scope[scope or ""].append((int(claim_id), str(text)))
    pairs: list[NearDuplicate] = []
    for scope, items in by_scope.items():
        if len(items) < 2:
            continue
        lowered = [text.lower() for _, text in items]
        scores = process.cdist(lowered, lowered, scorer=fuzz.token_set_ratio, score_cutoff=threshold, workers=1)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                score = float(scores[i][j])
                if score < threshold:
                    continue
                (id_a, text_a), (id_b, text_b) = items[i], items[j]
                if _jaccard(text_a, text_b) >= JACCARD_ALREADY_HANDLED or differs_by_identifier(text_a, text_b):
                    continue
                keep, newer = sorted((id_a, id_b))
                pairs.append(NearDuplicate(keep, newer, scope, round(score, 1)))
    pairs.sort(key=lambda pair: (-pair.score, pair.keep_id, pair.newer_id))
    return pairs


def _live_rows(store) -> list[tuple[int, str, str]]:
    import contextlib

    marks = ", ".join("?" for _ in LIVE_STATUSES)
    with contextlib.closing(store.connect()) as conn:
        return [
            (int(row[0]), row[1] or "", row[2] or "")
            for row in conn.execute(
                f"SELECT id, scope, text FROM claims WHERE status IN ({marks}) "
                "AND COALESCE(visibility, 'public') = 'public'",
                LIVE_STATUSES,
            )
        ]


def _already_proposed(store, pair: NearDuplicate) -> bool:
    for event in store.list_events(claim_id=pair.newer_id, event_type="policy_decision", limit=200):
        try:
            payload = json.loads(event.payload_json or "{}")
        except ValueError:
            continue
        if (isinstance(payload, dict) and payload.get("source") == SOURCE
                and payload.get("related_claim_id") == pair.keep_id):
            return True
    return False


def propose(store, pair: NearDuplicate) -> str | None:
    """File one steward proposal; returns why nothing was filed, or None."""
    newer = store.get_claim(pair.newer_id, include_citations=False)
    keep = store.get_claim(pair.keep_id, include_citations=False)
    if newer is None or keep is None or newer.status not in LIVE_STATUSES or keep.status not in LIVE_STATUSES:
        return "claim_status_changed"
    if _already_proposed(store, pair):
        return "proposal_exists"
    store.record_event(
        claim_id=pair.newer_id, event_type="policy_decision", from_status=newer.status, to_status="superseded",
        details=f"steward_proposal:fuzzy_{DECISION}",
        payload={
            "source": SOURCE, "proposal_type": "fuzzy_dedup", "decision": DECISION, "proposed_status": "superseded",
            "priority": round(pair.score / 100.0, 4), "apply_requested": False,
            "reasons": [{"code": "fuzzy_dedup:token_set_ratio", "probe_type": "rapidfuzz", "severity": "info",
                         "detail": f"Near-duplicate of claim {pair.keep_id} (token_set_ratio {pair.score}, "
                                   "no differing identifiers); operator review required."}],
            "replaced_by_claim_id": pair.keep_id, "related_claim_id": pair.keep_id, "pair_ref": pair.ref,
            "evidence": {"token_set_ratio": pair.score, "scope": pair.scope},
        },
    )
    return None


def run(store, *, limit: int | None = None, threshold: float = THRESHOLD) -> dict[str, Any]:
    """One steward pass: find near-duplicate live pairs and file at most ``limit`` new proposals."""
    if not enabled():
        return {"stopped": "disabled"}
    cap = per_cycle() if limit is None else max(0, int(limit))
    if cap == 0:
        return {"stopped": "cap_zero"}
    try:
        import numpy  # noqa: F401 - process.cdist returns a numpy matrix
        import rapidfuzz  # noqa: F401 - optional extra `dedupe`
    except ImportError:
        return {"stopped": "rapidfuzz_missing"}
    pairs = find_pairs(_live_rows(store), threshold=threshold)
    filed = 0
    skipped: collections.Counter[str] = collections.Counter()
    for pair in pairs:
        if filed >= cap:
            skipped["cap"] += 1
            continue
        reason = propose(store, pair)
        if reason is None:
            filed += 1
        else:
            skipped[reason] += 1
    return {"pairs": len(pairs), "proposed": filed, "skipped": dict(skipped)}

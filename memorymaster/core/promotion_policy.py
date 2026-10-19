"""Single policy point: which claim authors never reach ``confirmed`` automatically.

Operator decision 2026-10-13 (T-1354): claims whose ``source_agent`` is
``llm-stop-hook`` stay ``candidate``. A blind-labelled review rated 30% of the
hook's confirmed claims as obvious or low value, so no automatic path may
promote them. Only an explicit operator/agent call to the lifecycle API
(``transition_claim`` with an ``operator``/``agent`` actor) may still confirm
them; no MCP, CLI or dashboard surface exposes that today, and steward proposals
never target ``confirmed``.

Every automatic promoter consults this module: ``core.lifecycle.transition_claim``
enforces it for all lifecycle jobs (validator, jev revalidation, curation drain,
compaction), and the steward's raw-SQL confirm applies the same predicate.
``precompact-residual`` (T-1633) joins the default set: a residual is extracted by an
LLM from the turns a compaction is about to summarize, so its claims are leads for
review, not facts, and stay ``candidate`` like the stop hook's.
Configure the set with ``MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES``
(comma-separated source agents; set it empty to disable the policy).

T-1591 adds a structural rule on the same gate: a predicate without a subject is
a shifted field (a category label in the predicate slot), not a triple, so it is
never auto-promoted, whoever wrote it. A subject with a predicate and no object
is NOT malformed: it is the normal shape of dream-worker output. (The loopback
"facts" that motivated T-1591 are fixed at the source, in
``govern.jobs.extractor.infer_structure``, not blocked here.)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ENV_NON_AUTO_PROMOTABLE_SOURCES = "MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES"
DEFAULT_NON_AUTO_PROMOTABLE_SOURCES = frozenset({"llm-stop-hook", "precompact-residual"})

# Lifecycle event payload ``actor`` values that mark an explicit decision rather
# than an automatic cycle (cycles stamp ``automation``, see govern.steward).
EXPLICIT_ACTORS = frozenset({"operator", "agent"})


class PromotionPolicyBlocked(ValueError):
    """An automatic path tried to confirm a claim its author may not auto-promote."""


def non_auto_promotable_sources() -> frozenset[str]:
    raw = os.environ.get(ENV_NON_AUTO_PROMOTABLE_SOURCES)
    if raw is None:
        return DEFAULT_NON_AUTO_PROMOTABLE_SOURCES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def auto_promotion_blocked(source_agent: str | None) -> bool:
    agent = str(source_agent or "").strip()
    return bool(agent) and agent in non_auto_promotable_sources()


def _blank(value: object) -> bool:
    return not str(value or "").strip()


def is_malformed_triple(subject: object, predicate: object) -> bool:
    """A predicate with no subject: the category label landed in the wrong slot."""
    return not _blank(predicate) and _blank(subject)


def structure_blocks_auto_promotion(subject: object, predicate: object) -> bool:
    return is_malformed_triple(subject, predicate)


def claim_auto_promotion_blocked(claim: Any) -> bool:
    """Source policy OR structure policy, read off a claim-like object or row mapping."""
    get = claim.get if isinstance(claim, Mapping) else lambda name: getattr(claim, name, None)
    return auto_promotion_blocked(get("source_agent")) or structure_blocks_auto_promotion(
        get("subject"), get("predicate")
    )


def is_explicit_actor(event_payload: dict[str, object] | None) -> bool:
    return str((event_payload or {}).get("actor") or "").strip().lower() in EXPLICIT_ACTORS

"""Single policy point: which claim authors never reach ``confirmed`` automatically.

Operator decision 2026-10-13 (T-1354): claims whose ``source_agent`` is
``llm-stop-hook`` stay ``candidate``. A blind-labelled review rated 30% of the
hook's confirmed claims as obvious or low value, so no automatic path may
promote them. A steward proposal approved by the operator, or any other
explicit human/agent action, may still confirm them.

Every automatic promoter consults this module: ``core.lifecycle.transition_claim``
enforces it for all lifecycle jobs (validator, jev revalidation, curation drain,
compaction), and the steward's raw-SQL confirm applies the same predicate.
Configure the set with ``MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES``
(comma-separated source agents; set it empty to disable the policy).
"""

from __future__ import annotations

import os

ENV_NON_AUTO_PROMOTABLE_SOURCES = "MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES"
DEFAULT_NON_AUTO_PROMOTABLE_SOURCES = frozenset({"llm-stop-hook"})

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


def is_explicit_actor(event_payload: dict[str, object] | None) -> bool:
    return str((event_payload or {}).get("actor") or "").strip().lower() in EXPLICIT_ACTORS

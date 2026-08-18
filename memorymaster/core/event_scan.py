"""Time-bounded scans of the steward-proposal event log.

WHY THIS EXISTS: two readers of the proposal queue used a global row cap as if
it were a time window --- ``list_events(event_type="policy_decision",
limit=max(limit * 6, 500))`` in the steward queue and ``limit=2000`` in recall.
A row cap is not a window. It is a budget spent by whatever the log happens to
contain, and `list_events` returns NEWEST first, so when the cap is reached the
rows that fall off are the OLDEST --- the proposals that have been waiting
longest and most need review. Measured on the production log, the steward side
was at 362 of 600 (60% consumed) and rising.

The consequences were not symmetric, and both were silent:

  * a proposal past the cap vanishes from the operator queue while still
    unresolved, and vanishes from the recall demotion set, so the claim someone
    declared outdated goes back to ranking at full score;
  * a *resolution* past the audit-side cap makes an already-resolved proposal
    read as pending again, and re-approving it lands in the failure R2 fixed.

The fix is to bound by TIME. A window is a declared policy: it means the same
thing next month regardless of how much unrelated bookkeeping the log absorbed
(487k `deterministic_adjust=+0.000` rows of 2.4M, ~15k/day). A row cap silently
means less every day. The row ceiling that remains is a memory guard, not a
window, and hitting it is logged rather than passed off as an empty tail.

Scanning both event types with the SAME ``since`` is sound: a resolution can
never predate the proposal it resolves, so every proposal inside the window has
its resolution inside the window too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROPOSAL_EVENT_TYPE = "policy_decision"
RESOLUTION_EVENT_TYPE = "audit"

#: How far back the proposal queue looks. A proposal still unresolved after
#: this long is abandoned, not queued; the number is a policy the reader can
#: state, unlike a row cap whose meaning drifts with unrelated event volume.
PROPOSAL_WINDOW_DAYS = 400

#: Memory guard, deliberately far above any plausible volume for these two
#: types (362 and 230 rows respectively in production). Reaching it is a bug
#: report, not a quietly truncated tail.
PROPOSAL_SCAN_CEILING = 50_000


@dataclass(frozen=True, slots=True)
class ProposalEventScan:
    """Proposal and resolution events over one declared window."""

    proposals: list[Any]
    resolutions: list[Any]
    since: str
    window_days: int
    ceiling_hit: bool


def window_start(days: int) -> str:
    """ISO-8601 UTC timestamp ``days`` in the past.

    Formatted exactly like stored ``created_at`` (second precision, explicit
    offset) so the SQLite string comparison and the Postgres timestamp
    comparison agree on the boundary row.
    """
    moment = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return moment.replace(microsecond=0).isoformat()


def scan_proposal_events(
    source: Any,
    *,
    window_days: int = PROPOSAL_WINDOW_DAYS,
    ceiling: int = PROPOSAL_SCAN_CEILING,
) -> ProposalEventScan:
    """Read proposals and their resolutions over one time window.

    ``source`` is anything exposing ``list_events(event_type=, limit=, since=)``
    --- a ``MemoryService`` or a store. Raises whatever the source raises:
    callers that must not fail decide that for themselves, and a caller that
    swallows the error has to be able to tell it apart from an empty result.
    """
    since = window_start(window_days)
    proposals = list(source.list_events(event_type=PROPOSAL_EVENT_TYPE, limit=ceiling, since=since))
    resolutions = list(source.list_events(event_type=RESOLUTION_EVENT_TYPE, limit=ceiling, since=since))
    ceiling_hit = len(proposals) >= ceiling or len(resolutions) >= ceiling
    if ceiling_hit:
        logger.warning(
            "proposal event scan hit the %d-row ceiling (proposals=%d, resolutions=%d, "
            "window=%dd): the oldest rows in the window were dropped and pending "
            "proposals may be missing from the queue",
            ceiling,
            len(proposals),
            len(resolutions),
            window_days,
        )
    return ProposalEventScan(
        proposals=proposals,
        resolutions=resolutions,
        since=since,
        window_days=int(window_days),
        ceiling_hit=ceiling_hit,
    )

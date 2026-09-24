"""Authorized bounded candidate admission before hybrid semantic ranking."""
from __future__ import annotations

from typing import Callable

from memorymaster.core.models import Claim
from memorymaster.core.security import is_sensitive_claim
from memorymaster.core.temporal_policy import claim_is_temporally_current

_HYBRID_CANDIDATE_POOL_MIN = 1_000
_HYBRID_CANDIDATE_POOL_CAP = 2_000
_HYBRID_CANDIDATE_PAGE_SIZE = 256
# Lexical window growth (review F-14). The FTS stream used to be truncated to
# its row budget in SQL before visibility/sensitivity filters ran, so matching
# foreign-private or sensitive rows could hide every authorized match. The
# window now grows geometrically until the budget holds authorized rows or the
# matches run out. The ceiling (_LEXICAL_SCAN_CAP rows, shared across the
# planner OR terms) bounds the hot recall-hook path on pathological corpora.
# Total rows fetched stay under 1.34x the final window only while growth is
# unclamped; when the last step is clamped to the ceiling, the total is up to
# about 2.34x the cap (18,392 rows measured for 3 terms, no authorized match).
_LEXICAL_WINDOW_GROWTH = 4
_LEXICAL_SCAN_CAP = 8_192


def page_authorized_lexical(
    fetch: Callable[[int], tuple[list[Claim], bool]],
    limit: int,
    authorize: Callable[[list[Claim]], list[Claim]],
    *,
    terms: int = 1,
) -> list[Claim]:
    """Grow the lexical window until ``limit`` rows survive ``authorize``.

    ``fetch(window)`` returns the matches for that window and whether the
    store ran out of matches (fewer rows than requested). ``fetch`` runs one
    query per planner OR term, so the ceiling is shared across ``terms``.
    """
    window = max(1, int(limit))
    ceiling = max(window, _LEXICAL_SCAN_CAP // max(1, int(terms)))
    while True:
        rows, exhausted = fetch(window)
        authorized = authorize(rows)
        if len(authorized) >= limit or exhausted or window >= ceiling:
            return authorized[:limit]
        window = min(ceiling, window * _LEXICAL_WINDOW_GROWTH)


def hybrid_candidates(service, query_text, *, limit, statuses, normalized_scopes,
                      requesting_agent, include_sensitive, use_llm_rerank,
                      visibility_filter) -> list[Claim]:
    """Bound resident/embedded rows; page past unauthorized corpus entries."""
    candidate_pool_limit = min(
        _HYBRID_CANDIDATE_POOL_CAP,
        max(limit * 20, _HYBRID_CANDIDATE_POOL_MIN, 50 if use_llm_rerank else 0),
    )
    # Reserve capacity for lexical retrieval even at large caller limits.
    # Otherwise a full broad stream could exhaust the total cap before an
    # exact lexical match is admitted.  The reservation remains bounded and
    # grows modestly with the displayed window.
    lexical_reserve = min(
        _HYBRID_CANDIDATE_POOL_CAP // 2,
        max(limit * 6, 60),
    )
    broad_candidate_limit = max(0, candidate_pool_limit - lexical_reserve)
    # list_claims without text is intentionally confidence/recency ordered.
    # A useful exact lexical match can therefore fall outside even the
    # bounded broad pool.  Query it independently against the same
    # SQLite-authorized tenant/scope/status corpus, then deduplicate after
    # applying temporal, sensitivity and per-agent visibility filters.  No
    # claim reaches the embedding provider until all of those filters hold.
    def _authorized_candidates(claims: list[Claim]) -> list[Claim]:
        eligible = [claim for claim in claims if claim.status in statuses and claim_is_temporally_current(claim)]
        if not include_sensitive:
            eligible = [claim for claim in eligible if not is_sensitive_claim(claim)]
        # Visibility: filter out private claims from other agents (parity
        # with the legacy path; without this the hybrid path leaks
        # cross-agent data).
        return visibility_filter(eligible, requesting_agent)

    lexical_candidates = (
        service._legacy_candidates(
            query_text,
            lexical_reserve,
            statuses,
            normalized_scopes,
            authorize=_authorized_candidates,
        )
        if query_text.strip()
        else []
    )
    # Page until the authorized quota is filled or the scoped corpus ends.
    # A raw-row cap would let private/sensitive noise starve an authorized
    # semantic match. Memory and embedding work remain bounded; scan work
    # scales with the number of in-scope rows rejected by authority filters.
    candidates: list[Claim] = []
    cursor = ""
    while len(candidates) < broad_candidate_limit:
        page, next_cursor = service.store.list_claims_page(
            limit=_HYBRID_CANDIDATE_PAGE_SIZE,
            cursor=cursor,
            status_in=list(statuses),
            include_archived=False,
            include_citations=True,
            scope_allowlist=normalized_scopes,
            tenant_id=service.tenant_id,
        )
        candidates.extend(_authorized_candidates(page)[:broad_candidate_limit - len(candidates)])
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    candidates_by_id = {claim.id: claim for claim in candidates}
    for claim in lexical_candidates:  # already authorized while paging
        if len(candidates_by_id) >= _HYBRID_CANDIDATE_POOL_CAP:
            break
        candidates_by_id.setdefault(claim.id, claim)
    candidates = list(candidates_by_id.values())
    return candidates

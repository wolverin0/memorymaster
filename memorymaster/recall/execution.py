"""Shared AND-first execution of immutable retrieval plans."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memorymaster.recall.planner import RetrievalPlan


def execute_planned_search(
    plan: RetrievalPlan,
    query_rows: Callable[..., list[dict[str, Any]]],
    **options: Any,
) -> list[dict[str, Any]]:
    """Keep exact matches first, filling only unoccupied slots from expansion.

    Callers supply their existing authorization and access-recording options;
    both searches receive identical options and the original result limit.
    """
    exact_text = plan.query_text.strip()
    expanded = plan.search_text != exact_text
    rows = query_rows(
        query_text=exact_text if expanded else plan.search_text, **options
    )
    if not expanded or len(rows) >= plan.limit:
        return rows

    seen = {row["claim"].id for row in rows}
    for row in query_rows(query_text=plan.search_text, **options):
        claim_id = row["claim"].id
        if claim_id in seen:
            continue
        rows.append(row)
        seen.add(claim_id)
        if len(rows) == plan.limit:
            break
    return rows

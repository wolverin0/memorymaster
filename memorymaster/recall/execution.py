"""Shared AND-first execution of immutable retrieval plans."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memorymaster.recall.planner import RetrievalPlan


def execute_planned_search(
    plan: RetrievalPlan,
    query_rows: Callable[..., list[dict[str, Any]]],
    *,
    access_recorder: Callable[..., None] | None = None,
    **options: Any,
) -> list[dict[str, Any]]:
    """Keep exact matches first, filling only unoccupied slots from expansion.

    Both searches receive identical authorization options and the original
    result limit. When record_accesses is enabled, defer it to access_recorder
    for the final merged rows so discarded candidates never count as accesses.
    """
    record_accesses = options.get("record_accesses", False)
    if record_accesses:
        if access_recorder is None:
            raise ValueError("record_accesses requires an access_recorder")
        options["record_accesses"] = False

    exact_text = plan.query_text.strip()
    expanded = plan.search_text != exact_text
    rows = query_rows(
        query_text=exact_text if expanded else plan.search_text, **options
    )
    if expanded and len(rows) < plan.limit:
        seen = {row["claim"].id for row in rows}
        for row in query_rows(query_text=plan.search_text, **options):
            claim_id = row["claim"].id
            if claim_id in seen:
                continue
            rows.append(row)
            seen.add(claim_id)
            if len(rows) == plan.limit:
                break
    if record_accesses:
        access_recorder(rows, query_text=plan.query_text)
    return rows

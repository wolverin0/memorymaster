#!/usr/bin/env python3
"""Repair claims the old candidate dedupe archived although they were declared corrections (defect X-1).

Victim pair: A superseded by B (A.replaced_by = B), B archived by dedupe with B.replaced_by = A and
B.supersedes_claim_id = A. Repair: B back to confirmed, B.replaced_by cleared, A stays superseded by B.

    python scripts/repair_dedupe_victims.py --db PATH            # dry-run (default), exit 0
    python scripts/repair_dedupe_victims.py --db PATH --apply    # exit 1 if any pair could not be repaired

Each pair goes through the sanctioned helpers: revival (archived -> candidate, reingest_revival reason),
``transition_claim`` to confirmed with the explicit ``operator`` actor, then ``store.clear_replaced_by``.
Refusals that can be foreseen (dreaming source review) are checked before anything is written.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from memorymaster.core.lifecycle import REVIVAL_REASON_PREFIX, transition_claim
from memorymaster.core.models import STATUS_TRANSITION_EVENT_TYPES
from memorymaster.core.promotion_policy import claim_auto_promotion_blocked
from memorymaster.dreaming.source_review import review_allows_confirmation
from memorymaster.stores.storage import SQLiteStore

TAG = "x1_dedupe_victim"
EXPLICIT_RETIREMENTS = {"friendly_forget_claim", "ao7_rollback"}
_TYPES = ",".join("?" for _ in STATUS_TRANSITION_EVENT_TYPES)


def find_pairs(store: SQLiteStore) -> list[tuple[int, int, str | None]]:
    """(A, B, B's last retiring detail) for the victim shape; the 'dedup' reason is required."""
    out = []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT a.id AS a, b.id AS b FROM claims a JOIN claims b ON a.replaced_by_claim_id = b.id "
            "AND b.replaced_by_claim_id = a.id WHERE a.status = 'superseded' AND b.status = 'archived' "
            "ORDER BY a.id"
        ).fetchall()
        for r in rows:
            ev = conn.execute(
                "SELECT details FROM events WHERE claim_id = ? "
                f"AND event_type IN ({_TYPES}) AND (to_status = 'archived' OR details LIKE '%dedup%') "
                "ORDER BY id DESC LIMIT 1",
                (r["b"], *STATUS_TRANSITION_EVENT_TYPES),
            ).fetchone()
            details = str(ev["details"] or "") if ev is not None else ""
            if "dedup" in details or details in EXPLICIT_RETIREMENTS:  # explicit ones are listed, then skipped
                out.append((int(r["a"]), int(r["b"]), ev["details"]))
    return out


def classify(store: SQLiteStore, a_id: int, b_id: int) -> tuple[str, str]:
    a = store.get_claim(a_id, include_citations=False)
    b = store.get_claim(b_id, include_citations=False)
    if a is None or b is None:
        return "failed", "claim vanished"
    if b.supersedes_claim_id != a.id:
        return "skipped_not_declared", "B does not declare supersedes=A"
    if store.latest_archive_reason(b.id) in EXPLICIT_RETIREMENTS:
        return "skipped_explicit_forget", "B was archived by forget/rollback"
    if (a.scope, a.tenant_id) != (b.scope, b.tenant_id):
        return "skipped_scope_mismatch", "A and B differ in scope/tenant"
    if not review_allows_confirmation(store, b.id):
        return "failed", "confirm would be refused (dreaming source review missing)"
    note = " (source/structure policy overridden by operator actor)" if claim_auto_promotion_blocked(b) else ""
    return "repair", "revive B -> confirmed, clear B.replaced_by" + note


def repair(store: SQLiteStore, a_id: int, b_id: int) -> None:
    payload = {"repair": TAG, "pair": [a_id, b_id]}
    b = store.get_claim(b_id, include_citations=False)
    store.apply_status_transition(
        b, to_status="candidate", reason=f"{REVIVAL_REASON_PREFIX}{TAG}:{b.source_agent or 'unknown'}",
        event_type="transition", event_payload=payload,
    )
    transition_claim(store, b_id, "confirmed", reason=TAG, event_payload={**payload, "actor": "operator"})
    store.clear_replaced_by(b_id, expected_replaced_by=a_id, reason=TAG, event_payload=payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    store = SQLiteStore(args.db)
    pairs = find_pairs(store)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(pairs)} victim pair(s)")
    failed = 0
    for a_id, b_id, _ in pairs:
        a = store.get_claim(a_id, include_citations=False)
        b = store.get_claim(b_id, include_citations=False)
        verdict, why = classify(store, a_id, b_id)
        print(f"A={a_id} ({a.status}, {a.created_at}) B={b_id} ({b.status}, {b.created_at}, "
              f"updated {b.updated_at}) scope={b.scope} -> {verdict}: {why}")
        if verdict == "failed":
            failed += 1
        elif verdict == "repair" and args.apply:
            try:
                repair(store, a_id, b_id)
                print(f"  repaired {a_id}/{b_id}")
            except Exception as exc:  # noqa: BLE001 - report the pair, keep going
                failed += 1
                print(f"  FAILED {a_id}/{b_id}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Repair claims the old candidate dedupe archived although they were declared corrections (defect X-1).

Victim pair: A superseded by B (A.replaced_by = B), B archived by dedupe with B.replaced_by = A and
B.supersedes_claim_id = A. Repair: B back to confirmed, B.replaced_by cleared, A stays superseded by B.

    python scripts/repair_dedupe_victims.py --db PATH            # dry-run (default), exit 0
    python scripts/repair_dedupe_victims.py --db PATH --apply    # exit 1 if any pair could not be repaired
    --allow-policy-override   also repair a B whose author never auto-promotes or that has no citation

Each pair is repaired by ONE transaction (``store.restore_declared_correction``): revival event, confirmation
event (actor ``operator``) and pointer clear commit together or not at all. Pairs that are not safe to touch
are listed with a ``skipped_*`` reason and left alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from memorymaster.core.models import STATUS_TRANSITION_EVENT_TYPES
from memorymaster.core.promotion_policy import claim_auto_promotion_blocked
from memorymaster.dreaming.source_review import review_allows_confirmation
from memorymaster.stores.storage import SQLiteStore

TAG = "x1_dedupe_victim"
EXPLICIT_RETIREMENTS = {"friendly_forget_claim", "ao7_rollback"}
# Markers written by the store's redact_claim_payload (stores/_storage_write_claims.py): "[REDACTED...]" / "[ERASED...]".
REDACTION_PREFIXES = ("[REDACTED", "[ERASED")
PROPOSAL_DETAILS = "steward_proposal:superseded_candidate"  # govern.ingest_governance.PROPOSAL_DETAILS
LIVE = ("candidate", "confirmed")
_TYPES = ",".join("?" for _ in STATUS_TRANSITION_EVENT_TYPES)

# Start from archived rows with a pointer and reach A by primary key (no full-table self-join).
PAIRS_SQL = (
    "SELECT b.id AS b, a.id AS a FROM claims b JOIN claims a ON a.id = b.replaced_by_claim_id "
    "WHERE b.status = 'archived' AND b.replaced_by_claim_id IS NOT NULL "
    "AND a.status = 'superseded' AND a.replaced_by_claim_id = b.id ORDER BY a.id"
)


def find_pairs(store: SQLiteStore) -> list[tuple[int, int]]:
    """(A, B) for the victim shape whose B was last retired by dedupe (explicit forgets are listed, then skipped)."""
    out = []
    with store.connect() as conn:
        for r in conn.execute(PAIRS_SQL).fetchall():
            ev = conn.execute(
                "SELECT details FROM events WHERE claim_id = ? "
                f"AND event_type IN ({_TYPES}) AND (to_status = 'archived' OR details LIKE '%dedup%') "
                "ORDER BY id DESC LIMIT 1",
                (r["b"], *STATUS_TRANSITION_EVENT_TYPES),
            ).fetchone()
            details = str(ev["details"] or "") if ev is not None else ""
            if "dedup" in details or details in EXPLICIT_RETIREMENTS:
                out.append((int(r["a"]), int(r["b"])))
    return out


def _redacted(claim) -> bool:
    return any(str(v or "").startswith(REDACTION_PREFIXES)
               for v in (claim.text, claim.subject, claim.predicate, claim.object_value))


def _newer_value(store: SQLiteStore, a, b) -> str | None:
    """Reason if some other live claim already holds B's tuple or declares a supersession of A or B."""
    with store.connect() as conn:
        if b.subject and b.predicate:
            row = conn.execute(
                "SELECT id FROM claims WHERE id NOT IN (?, ?) AND status IN ('candidate','confirmed') "
                "AND subject = ? AND predicate = ? AND scope = ? AND COALESCE(tenant_id,'') = ? LIMIT 1",
                (a.id, b.id, b.subject, b.predicate, b.scope, b.tenant_id or ""),
            ).fetchone()
            if row:
                return "another live claim holds the same subject+predicate"
        row = conn.execute(
            "SELECT id FROM claims WHERE id NOT IN (?, ?) AND status IN ('candidate','confirmed') "
            "AND supersedes_claim_id IN (?, ?) LIMIT 1", (a.id, b.id, a.id, b.id),
        ).fetchone()
        if row:
            return "another live claim declares supersedes A or B"
        try:
            row = conn.execute(
                "SELECT l.source_id FROM claim_links l JOIN claims c ON c.id = l.source_id "
                "WHERE l.link_type = 'supersedes' AND l.target_id IN (?, ?) AND l.source_id NOT IN (?, ?) "
                "AND c.status IN ('candidate','confirmed') LIMIT 1", (a.id, b.id, a.id, b.id),
            ).fetchone()
        except Exception:  # noqa: BLE001 - no links table on this schema: nothing to find
            row = None
        if row:
            return "another live claim holds a supersedes link to A or B"
        for target in (a.id, b.id):
            for e in conn.execute(
                "SELECT payload_json FROM events WHERE claim_id = ? AND event_type = 'policy_decision' "
                "AND details = ?", (target, PROPOSAL_DETAILS),
            ).fetchall():
                try:
                    other = json.loads(e["payload_json"] or "{}").get("replaced_by_claim_id")
                except (ValueError, TypeError, AttributeError):
                    continue
                if isinstance(other, int) and other != b.id:
                    c = conn.execute("SELECT status FROM claims WHERE id = ?", (other,)).fetchone()
                    if c is not None and c["status"] in LIVE:
                        return "a live supersession proposal names another replacement"
    return None


def classify(store: SQLiteStore, a_id: int, b_id: int, *, allow_policy_override: bool) -> tuple[str, str]:
    a = store.get_claim(a_id, include_citations=False)
    b = store.get_claim(b_id, include_citations=True)
    if a is None or b is None:
        return "failed", "claim vanished"
    if b.supersedes_claim_id != a.id:
        return "skipped_not_declared", "B does not declare supersedes=A"
    if store.latest_archive_reason(b.id) in EXPLICIT_RETIREMENTS:
        return "skipped_explicit_forget", "B was archived by forget/rollback"
    if (a.scope, a.tenant_id) != (b.scope, b.tenant_id):
        return "skipped_scope_mismatch", "A and B differ in scope/tenant"
    if _redacted(b):
        return "skipped_redacted", "B text or structure carries redaction markers"
    newer = _newer_value(store, a, b)
    if newer:
        return "skipped_newer_value", newer
    if not review_allows_confirmation(store, b.id):
        return "failed", "confirm would be refused (dreaming source review missing)"
    why = []
    if claim_auto_promotion_blocked(b):
        why.append("author/structure never auto-promotes")
    if not b.citations:
        why.append("no citations")
    if why and not allow_policy_override:
        return "skipped_policy", "; ".join(why) + " (use --allow-policy-override)"
    return "repair", "restore B to confirmed, clear B.replaced_by" + (f" [override: {'; '.join(why)}]" if why else "")


def repair(store: SQLiteStore, a_id: int, b_id: int) -> None:
    b = store.get_claim(b_id, include_citations=False)
    store.restore_declared_correction(
        b_id, a_id, reason=f"{TAG}:{b.source_agent or 'unknown'}",
        event_payload={"repair": TAG, "pair": [a_id, b_id], "actor": "operator"},
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-policy-override", action="store_true")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    store = SQLiteStore(args.db)
    pairs = find_pairs(store)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(pairs)} victim pair(s)")
    failed = 0
    for a_id, b_id in pairs:
        a = store.get_claim(a_id, include_citations=False)
        b = store.get_claim(b_id, include_citations=True)
        verdict, why = classify(store, a_id, b_id, allow_policy_override=args.allow_policy_override)
        print(f"A={a_id} ({a.status}, created {a.created_at}) B={b_id} ({b.status}, created {b.created_at}, "
              f"updated {b.updated_at}) scope={b.scope} B.source={b.source_agent} B.citations={len(b.citations)} "
              f"B.tuple={'yes' if b.subject and b.predicate else 'no'} -> {verdict}: {why}")
        if verdict == "failed":
            failed += 1
        elif verdict == "repair" and args.apply:
            try:
                repair(store, a_id, b_id)
                print(f"  repaired {a_id}/{b_id}")
            except Exception as exc:  # noqa: BLE001 - the transaction rolled back; report, keep going
                failed += 1
                print(f"  FAILED {a_id}/{b_id} (rolled back): {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

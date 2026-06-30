from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

from memorymaster.core import observability
from memorymaster.core.config import get_config
from memorymaster.core.lifecycle import transition_claim

logger = logging.getLogger(__name__)

# Hebbian/Ebbinghaus tuning. Floor keeps a decayed edge traversable (never 0,
# so a recall path is never fully erased — matches the MemPalace forgetting
# curve where memories fade but leave a trace). Default lambda gives a ~35-day
# half-life: weight*EXP(-0.02*35) ≈ weight*0.5.
EDGE_WEIGHT_FLOOR = 0.01
DEFAULT_EDGE_DECAY_LAMBDA = 0.02


def _parse_iso(dt: str) -> datetime:
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _edge_decay_enabled() -> bool:
    """Hebbian/Ebbinghaus edge decay is RECALL-ALTERING — default OFF.

    Gated behind MEMORYMASTER_HEBBIAN_DECAY so default behavior is byte-identical:
    when unset/falsey, decay_entity_edges() never mutates a single weight and
    find_related_claims ordering is unchanged from the pre-feature baseline.
    """
    raw = os.environ.get("MEMORYMASTER_HEBBIAN_DECAY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _edge_decay_lambda() -> float:
    raw = os.environ.get("MEMORYMASTER_HEBBIAN_DECAY_LAMBDA", "").strip()
    if not raw:
        return DEFAULT_EDGE_DECAY_LAMBDA
    try:
        val = float(raw)
        return val if val >= 0 else DEFAULT_EDGE_DECAY_LAMBDA
    except ValueError:
        return DEFAULT_EDGE_DECAY_LAMBDA


def decay_entity_edges(store, *, now: datetime | None = None) -> dict:
    """Ebbinghaus forgetting curve over entity-graph edges.

    For each edge: ``weight = MAX(floor, weight * EXP(-lambda * elapsed_days))``
    where elapsed_days is measured from ``last_reinforced_at`` (Hebbian stamp).
    Edges missing the timestamp are default-filled to ``created_at`` (or NOW)
    so they participate on the next pass instead of decaying from epoch.

    RECALL-ALTERING: gated behind ``MEMORYMASTER_HEBBIAN_DECAY`` (default OFF).
    When disabled, returns ``{"enabled": False, "decayed": 0}`` and touches
    nothing. Migration-safe: if ``entity_edges`` (or the column) doesn't exist,
    returns a clean ``skipped`` result rather than raising.

    Computed in Python (not SQL ``EXP``) so SQLite and Postgres behave
    identically without engine-specific functions.
    """
    if not _edge_decay_enabled():
        return {"enabled": False, "decayed": 0}

    lam = _edge_decay_lambda()
    now = now or datetime.now(timezone.utc)
    # The steward passes the MemoryService store (exposes .connect()); tests and
    # the EntityGraph path expose ._connect(). Accept either so the job is usable
    # from both without a second adapter.
    open_conn = getattr(store, "connect", None) or getattr(store, "_connect")
    conn = open_conn()
    try:
        skipped = {"enabled": True, "skipped": "missing entity_edges.last_reinforced_at", "decayed": 0}
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(entity_edges)").fetchall()}
        except Exception:
            # Postgres / non-sqlite stores have no PRAGMA; fall back to a probe
            # SELECT and treat any error as a graceful skip (table/column absent).
            cols = set()
        if cols:
            # PRAGMA succeeded: empty set = no such table; column absent = un-migrated.
            if not cols or "last_reinforced_at" not in cols:
                return skipped
        else:
            try:
                conn.execute("SELECT last_reinforced_at FROM entity_edges LIMIT 1")
            except Exception:
                return skipped

        rows = conn.execute(
            "SELECT source_id, target_id, relation, weight, last_reinforced_at, created_at "
            "FROM entity_edges"
        ).fetchall()

        decayed = 0
        floored = 0
        backfilled = 0
        for row in rows:
            stamp = row["last_reinforced_at"]
            if not stamp:
                # Default-fill: anchor to created_at (or NOW) so the edge starts
                # its decay clock from a real point instead of decaying instantly.
                stamp = row["created_at"] or now.isoformat()
                conn.execute(
                    "UPDATE entity_edges SET last_reinforced_at = ? "
                    "WHERE source_id = ? AND target_id = ? AND relation = ?",
                    (stamp, row["source_id"], row["target_id"], row["relation"]),
                )
                backfilled += 1

            try:
                elapsed_days = max((now - _parse_iso(stamp)).total_seconds() / 86400.0, 0.0)
            except (ValueError, TypeError):
                continue
            if elapsed_days <= 0:
                continue

            old_weight = float(row["weight"])
            new_weight = max(EDGE_WEIGHT_FLOOR, old_weight * math.exp(-lam * elapsed_days))
            if new_weight == old_weight:
                continue
            conn.execute(
                "UPDATE entity_edges SET weight = ? "
                "WHERE source_id = ? AND target_id = ? AND relation = ?",
                (new_weight, row["source_id"], row["target_id"], row["relation"]),
            )
            decayed += 1
            if new_weight <= EDGE_WEIGHT_FLOOR:
                floored += 1

        conn.commit()
        return {
            "enabled": True,
            "processed": len(rows),
            "decayed": decayed,
            "floored": floored,
            "backfilled": backfilled,
            "lambda": lam,
        }
    finally:
        conn.close()


def run(
    store,
    limit: int = 200,
    stale_threshold: float | None = None,
    dry_run: bool = False,
) -> dict:
    cfg = get_config()
    if stale_threshold is None:
        stale_threshold = cfg.stale_threshold
    decay_rates = cfg.decay_rates
    claims = store.find_for_decay(limit=limit)
    now = datetime.now(timezone.utc)

    if dry_run:
        planned_decay = []
        skipped_future = []
        decayed = 0
        transitioned = 0

        for claim in claims:
            updated_dt = _parse_iso(claim.updated_at)
            raw_age_seconds = (now - updated_dt).total_seconds()
            age_days = max(raw_age_seconds / 86400.0, 0.0)
            if age_days <= 0:
                if raw_age_seconds < 0:
                    skipped_future.append({"claim_id": claim.id, "updated_at": claim.updated_at})
                continue

            rate = decay_rates.get(claim.volatility, decay_rates["medium"])
            new_conf = max(0.0, claim.confidence - (rate * age_days))
            will_stale = new_conf < stale_threshold
            planned_decay.append(
                {
                    "claim_id": claim.id,
                    "from_status": claim.status,
                    "to_status": "stale" if will_stale else claim.status,
                    "old_confidence": claim.confidence,
                    "new_confidence": new_conf,
                    "age_days": age_days,
                    "decay_rate": rate,
                }
            )
            decayed += 1
            if will_stale:
                transitioned += 1

        return {
            "dry_run": True,
            "processed": len(claims),
            "decayed": decayed,
            "to_stale": transitioned,
            "planned_decay": planned_decay,
            "planned_transitions": [
                {
                    "claim_id": row["claim_id"],
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "old_confidence": row["old_confidence"],
                    "new_confidence": row["new_confidence"],
                }
                for row in planned_decay
                if row["to_status"] == "stale"
            ],
            "skipped_future": skipped_future,
        }

    decayed = 0
    transitioned = 0

    for claim in claims:
        updated_dt = _parse_iso(claim.updated_at)
        raw_age_seconds = (now - updated_dt).total_seconds()
        age_days = max(raw_age_seconds / 86400.0, 0.0)
        if age_days <= 0:
            # F-10 fix (overnight audit 2026-05-04): when raw_age_seconds < 0
            # the claim's updated_at is in the FUTURE — clock skew, malformed
            # ISO, or DST glitch. Previously this was silently swallowed
            # forever (no decay, no event, no log) and the corrupted timestamp
            # never surfaced. Record a "decay" event so operators can find
            # these via SELECT * FROM events WHERE event_type='decay' AND
            # details LIKE 'skipped:future%'. Cheap, defensive, no behavior
            # change for normal claims (raw_age_seconds=0 just-touched still
            # silently continues).
            if raw_age_seconds < 0:
                try:
                    store.record_event(
                        claim_id=claim.id,
                        event_type="decay",
                        details=f"skipped: future updated_at={claim.updated_at}",
                    )
                except Exception:
                    # Don't let event recording failure crash the decay loop
                    pass
            continue

        rate = decay_rates.get(claim.volatility, decay_rates["medium"])
        new_conf = max(0.0, claim.confidence - (rate * age_days))
        store.set_confidence(
            claim.id,
            new_conf,
            details=f"decay_rate={rate:.3f};age_days={age_days:.3f}",
        )
        decayed += 1

        if new_conf < stale_threshold:
            transition_claim(
                store,
                claim_id=claim.id,
                to_status="stale",
                reason=f"confidence fell below threshold: {new_conf:.3f}",
                event_type="decay",
            )
            transitioned += 1

    observability.bump_decay_run("success")
    return {"processed": len(claims), "decayed": decayed, "to_stale": transitioned}

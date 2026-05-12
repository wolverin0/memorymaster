from __future__ import annotations

from datetime import datetime, timezone

from memorymaster.config import get_config
from memorymaster.lifecycle import transition_claim

def _parse_iso(dt: str) -> datetime:
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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

    return {"processed": len(claims), "decayed": decayed, "to_stale": transitioned}

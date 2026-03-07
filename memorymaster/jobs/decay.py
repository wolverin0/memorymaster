from __future__ import annotations

from datetime import datetime, timezone

from memorymaster.lifecycle import transition_claim

DECAY_BY_VOLATILITY = {
    "low": 0.01,
    "medium": 0.03,
    "high": 0.06,
}


def _parse_iso(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


def run(store, limit: int = 200, stale_threshold: float = 0.35) -> dict[str, int]:
    claims = store.find_for_decay(limit=limit)
    now = datetime.now(timezone.utc)
    decayed = 0
    transitioned = 0

    for claim in claims:
        updated_dt = _parse_iso(claim.updated_at)
        age_days = max((now - updated_dt).total_seconds() / 86400.0, 0.0)
        if age_days <= 0:
            continue

        rate = DECAY_BY_VOLATILITY.get(claim.volatility, DECAY_BY_VOLATILITY["medium"])
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

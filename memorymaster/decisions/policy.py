"""Logging policy: deterministic exploration, per-surface breaker, USD/RPM budget.

Exploration is derived from ``sha256(decision_id + surface)`` so a decision can be
replayed exactly.  Every exploration result carries the full distribution the
logging policy used (``available_actions`` aligned with ``propensities``, summing
to 1) and the chosen action's propensity, which is what off-policy evaluation
needs.  Probability from Jev is not a propensity: a deterministic policy has
propensity 1 for its action; only exploration creates counterfactual support.

Breaker and budget state come from the shared ledger so several hook processes
agree; an unreadable ledger fails closed (no spend it cannot account for): a
failed read of the breaker's open-until mark is ``unknown``, never "not open".
While a surface's breaker is open, one shadow probe per ``breaker_probe_s`` is
claimed atomically in the ledger; every other call sends nothing.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.ledger import DecisionLedger, LedgerReadError, utc_iso
from memorymaster.decisions.questions import BoundQuestion, default_thresholds

POLICY_VERSION = "jev-policy/4.9.0-1"
BREAKER_CONSECUTIVE = 5
BREAKER_WINDOW = timedelta(minutes=10)
BREAKER_FALLBACK_SHARE = 0.30
BREAKER_MIN_DECISIONS = 10
BREAKER_OPEN_FOR = timedelta(minutes=15)
RANKING_TOP_K = 5
PL_TEMPERATURE = 0.25


def _digest(decision_id: str, surface: str, salt: str = "") -> bytes:
    return hashlib.sha256(f"{decision_id}{surface}{salt}".encode("utf-8")).digest()


def exploration_u(decision_id: str, surface: str) -> float:
    """Deterministic uniform number in [0, 1) for one decision on one surface."""
    return int.from_bytes(_digest(decision_id, surface)[:8], "big") / 2**64


def randomization_id(decision_id: str, surface: str) -> str:
    return _digest(decision_id, surface).hex()


@dataclass(frozen=True)
class ExplorationResult:
    action: Any
    arm: str
    available_actions: list[Any]
    propensities: list[float]
    chosen_propensity: float
    randomization_id: str
    u: float

    def propensities_json(self) -> str:
        return json.dumps([{"action": a, "p": p} for a, p in zip(self.available_actions, self.propensities)],
                          ensure_ascii=False, separators=(",", ":"))


def explore_binary(decision_id: str, surface: str, *, policy_action: Any, safe_alternative: Any | None,
                   rate: float, arm_name: str) -> ExplorationResult:
    """Take ``safe_alternative`` with probability ``rate``; otherwise the policy action."""
    u = exploration_u(decision_id, surface)
    rid = randomization_id(decision_id, surface)
    rate = min(max(float(rate), 0.0), 1.0) if math.isfinite(rate) else 0.0
    if safe_alternative is None or safe_alternative == policy_action or rate <= 0.0:
        return ExplorationResult(policy_action, "policy", [policy_action], [1.0], 1.0, rid, u)
    actions = [policy_action, safe_alternative]
    propensities = [1.0 - rate, rate]
    if u < rate:
        return ExplorationResult(safe_alternative, arm_name, actions, propensities, rate, rid, u)
    return ExplorationResult(policy_action, "policy", actions, propensities, 1.0 - rate, rid, u)


def plackett_luce_log_weights(scores: Mapping[str, float], items: Sequence[str],
                              temperature: float = PL_TEMPERATURE) -> dict[str, float]:
    """PL log-weights ``(score - top) / temperature``: finite for any finite score gap.

    Raises ``ValueError`` for a non-finite score: a NaN weight would log NaN propensities.
    """
    values = {item: float(scores.get(item, 0.0)) for item in items}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Plackett-Luce scores must be finite")
    top = max(values.values(), default=0.0)
    return {item: (value - top) / temperature for item, value in values.items()}


def plackett_luce_weights(scores: Mapping[str, float], items: Sequence[str],
                          temperature: float = PL_TEMPERATURE) -> dict[str, float]:
    """Unnormalized PL weights (may underflow to 0.0 for large gaps; exploration uses log space)."""
    return {item: math.exp(value) for item, value in plackett_luce_log_weights(scores, items, temperature).items()}


def _log_sum_exp(values: Sequence[float]) -> float:
    peak = max(values)
    if peak == -math.inf:
        return -math.inf
    return peak + math.log(sum(math.exp(value - peak) for value in values))


def plackett_luce_log_probability(order: Sequence[str], log_weights: Mapping[str, float]) -> float:
    """``log P(order)`` computed in log space (no underflow, no 0/0)."""
    remaining = list(order)
    total = 0.0
    for item in order:
        normalizer = _log_sum_exp([log_weights[other] for other in remaining])
        if normalizer == -math.inf:  # only zero weights left: each is equally likely
            total -= math.log(len(remaining))
        else:
            total += log_weights[item] - normalizer
        remaining.remove(item)
    return total


def plackett_luce_probability(order: Sequence[str], weights: Mapping[str, float]) -> float:
    logs = {item: math.log(weights[item]) if weights[item] > 0 else -math.inf for item in order}
    return math.exp(plackett_luce_log_probability(order, logs))


def orderings(items: Sequence[str]) -> list[list[str]]:
    return [list(p) for p in itertools.permutations(items)]


def explore_ranking(decision_id: str, surface: str, *, policy_order: Sequence[str], scores: Mapping[str, float],
                    rate: float, top_k: int = RANKING_TOP_K) -> ExplorationResult:
    """Explore the order of the top-k by Plackett-Luce over relevance scores.

    Logged distribution over every ordering of the top-k:
    ``(1 - rate) * [ordering == policy] + rate * PL(ordering)``.
    """
    order = list(policy_order)
    u = exploration_u(decision_id, surface)
    rid = randomization_id(decision_id, surface)
    rate = min(max(float(rate), 0.0), 1.0) if math.isfinite(rate) else 0.0
    top, rest = order[:top_k], order[top_k:]
    if len(top) < 2 or rate <= 0.0:
        return ExplorationResult(order, "policy", [top], [1.0], 1.0, rid, u)
    log_weights = plackett_luce_log_weights(scores, top)
    actions = orderings(top)
    propensities = [(1.0 - rate) * (candidate == top)
                    + rate * math.exp(plackett_luce_log_probability(candidate, log_weights))
                    for candidate in actions]
    if u < rate:
        rng = random.Random(int.from_bytes(_digest(decision_id, surface, ":pl")[:8], "big"))
        pool = list(top)
        chosen: list[str] = []
        while pool:
            # Weights relative to the pool's best item: the largest is 1, so the total is never 0.
            peak = max(log_weights[item] for item in pool)
            relative = [math.exp(log_weights[item] - peak) for item in pool]
            pick = rng.random() * sum(relative)
            for item, weight in zip(pool, relative):
                if pick < weight:
                    break
                pick -= weight
            else:  # rounding fell through: take the pool's best item
                item = pool[relative.index(1.0)]
            chosen.append(item)
            pool.remove(item)
        arm = "explore_order"
    else:
        chosen, arm = top, "policy"
    chosen_propensity = propensities[actions.index(chosen)]
    return ExplorationResult(chosen + rest, arm, actions, propensities, chosen_propensity, rid, u)


# ------------------------------------------------------------------ breaker ---

class Breaker:
    """Per-surface circuit breaker backed by the ledger (shared across processes)."""

    def __init__(self, ledger: DecisionLedger) -> None:
        self.ledger = ledger

    @staticmethod
    def _key(surface: str) -> str:
        return f"breaker:{surface}"

    def state(self, surface: str, *, now: datetime | None = None) -> str:
        """``"open"``, ``"closed"`` or ``"unknown"`` (the ledger could not be read).

        ``unknown`` must be treated as open: a watermark read that met another
        process's lock is not an absent watermark.
        """
        moment = now or datetime.now(timezone.utc)
        try:
            open_until = self.ledger.get_watermark(self._key(surface), strict=True)
        except LedgerReadError:
            return "unknown"
        if open_until and utc_iso(moment) < open_until:
            return "open"
        requested = self.ledger.requested_decisions(surface, utc_iso(moment))
        if requested is None:
            return "unknown"  # cannot see failures: do not act on Jev
        if self._should_trip(requested, moment):
            self.ledger.set_watermark(self._key(surface), utc_iso(moment + BREAKER_OPEN_FOR))
            return "open"
        return "closed"

    def is_open(self, surface: str, *, now: datetime | None = None) -> bool:
        """Open or unknown: fails closed."""
        return self.state(surface, now=now) != "closed"

    def claim_probe(self, surface: str, probe_s: float, *, now: datetime | None = None) -> bool:
        """Whether this caller may send ``surface``'s shadow probe while its breaker is open.

        Only when nothing was sent on the surface for ``probe_s`` seconds and no
        other process claimed the probe meanwhile; an unwritable ledger never probes.
        """
        moment = now or datetime.now(timezone.utc)
        interval = float(probe_s) if math.isfinite(float(probe_s)) and float(probe_s) > 0 else 0.0
        claimed = self.ledger.claim_breaker_probe(surface, now_iso=utc_iso(moment),
                                                  since_iso=utc_iso(moment - timedelta(seconds=interval)))
        return claimed is True

    @staticmethod
    def _should_trip(rows: Sequence[Mapping[str, Any]], moment: datetime) -> bool:
        # Both rules look only at the last 10 minutes: old failures never chain with a new one.
        window_start = utc_iso(moment - BREAKER_WINDOW)
        recent = [r for r in rows if r["ts"] >= window_start]
        last = recent[:BREAKER_CONSECUTIVE]
        if len(last) == BREAKER_CONSECUTIVE and all(r["transport_outcome"] != "ok" for r in last):
            return True
        if len(recent) < BREAKER_MIN_DECISIONS:
            return False
        failed = sum(1 for r in recent if r["transport_outcome"] != "ok")
        return failed / len(recent) > BREAKER_FALLBACK_SHARE


# ------------------------------------------------------------------- budget ---

class TokenBucket:
    def __init__(self, per_minute: int, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(max(per_minute, 1))
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self._clock = monotonic
        self._last = monotonic()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = self._clock()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class Budget:
    """Daily USD cap from the ledger plus RPM (ledger-wide and in-process bucket)."""

    def __init__(self, ledger: DecisionLedger, config: DecisionConfig,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.ledger = ledger
        self.config = config
        self.bucket = TokenBucket(config.rpm_cap, monotonic)

    def check(self, *, now: datetime | None = None, estimated_cost: float = 0.0) -> str | None:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        spend = self.ledger.spend_since(utc_iso(day_start))
        recent = self.ledger.requests_since(utc_iso(moment - timedelta(seconds=60)))
        if spend is None or recent is None:
            return "ledger_unavailable"
        if spend + max(estimated_cost, 0.0) > self.config.daily_usd_cap:
            return "budget_exhausted"
        if recent >= self.config.rpm_cap:
            return "budget_exhausted"
        if not self.bucket.take():
            return "budget_exhausted"
        return None


# --------------------------------------------------------------- thresholds ---

def resolve_thresholds(ledger: DecisionLedger, bound: Iterable[BoundQuestion], *,
                       register: bool = True) -> dict[str, dict[str, float]] | None:
    """Thresholds per ``id@vN`` from ``question_versions`` (seeded by code defaults).

    ``None`` when the stored thresholds cannot be read: the engine then sends
    nothing rather than decide with thresholds it did not see.
    """
    specs = {b.spec.key: b.spec for b in bound}
    if register:
        ledger.register_questions(specs.values())
    stored = ledger.thresholds_many((spec.id, spec.version) for spec in specs.values())
    if stored is None:
        return None
    return {key: stored.get((spec.id, spec.version), default_thresholds(spec)) for key, spec in specs.items()}


__all__ = [
    "BREAKER_CONSECUTIVE",
    "Breaker",
    "Budget",
    "ExplorationResult",
    "POLICY_VERSION",
    "TokenBucket",
    "explore_binary",
    "explore_ranking",
    "exploration_u",
    "orderings",
    "plackett_luce_log_probability",
    "plackett_luce_log_weights",
    "plackett_luce_probability",
    "plackett_luce_weights",
    "randomization_id",
    "resolve_thresholds",
]

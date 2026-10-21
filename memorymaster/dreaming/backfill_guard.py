"""Stage permissions and durable pause semantics, independent of live beat state."""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from memorymaster.dreaming.latency_guard import GuardDecision

MANUAL_LATCHES = {"operator_paused", "quality_paused", "budget_paused", "latency_paused_manual", "retired"}
CHANNEL_THRESHOLDS = {"dense": (150.0, 300.0), "recall": (900.0, 950.0)}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _active_healthy(active: dict[str, Any] | None, now: float) -> bool:
    if not isinstance(active, dict) or active.get("passed") is not True:
        return False
    stamp = active.get("checked_at")
    if not _finite(stamp) or not 0 <= now - stamp <= 60:
        return False
    channels = active.get("channels")
    if not isinstance(channels, dict):
        return False
    for name, (soft, hard) in CHANNEL_THRESHOLDS.items():
        channel = channels.get(name, {})
        if (channel.get("n") != 5 or channel.get("error") or not _finite(channel.get("p95_ms"))
                or not _finite(channel.get("max_ms")) or channel["p95_ms"] >= soft or channel["max_ms"] >= hard):
            return False
    return True


def run_quiet_hours_active_probes(dense_probe: Callable[[], Any] | None = None,
                                 recall_probe: Callable[[], Any] | None = None, *,
                                 service_factory: Callable[[], Any] | None = None,
                                 scope: str | None = None, now: float | None = None,
                                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Call injected read-only probes five times; receipts contain timings only.

    With no injected probes, require an explicit read-only service factory and
    authorized scope. Never construct an implicit service or default database.
    """
    if dense_probe is None or recall_probe is None:
        if service_factory is None or not scope:
            raise ValueError("active probes require an explicit read-only service and scope")
        from memorymaster.recall.dense_recall import search

        service = service_factory()
        if getattr(service.store, "read_only", False) is not True:
            raise ValueError("active recall probe requires a read-only service")
        dense_probe = dense_probe or (lambda: search("backfill health canary", [scope], k=1))
        recall_probe = recall_probe or (lambda: service.query_rows(
            "backfill health canary", scope_allowlist=[scope], limit=1,
            include_candidates=False, include_stale=False, include_conflicted=False,
            retrieval_mode="legacy", record_accesses=False, requesting_agent="dream-worker"))
    channels: dict[str, Any] = {}
    for name, probe in (("dense", dense_probe), ("recall", recall_probe)):
        values = []
        try:
            for _ in range(5):
                start = clock()
                probe()
                elapsed = (clock() - start) * 1000
                if not _finite(elapsed):
                    raise ValueError("invalid probe timing")
                values.append(elapsed)
            channels[name] = {"n": 5, "median_ms": statistics.median(values),
                              "p95_ms": max(values), "max_ms": max(values)}
        except Exception:  # noqa: BLE001 - never serialize private exception text
            channels[name] = {"n": len(values), "error": "probe_failed"}
    receipt = {"passed": True, "checked_at": time.time() if now is None else now,
               "channels": channels, "thresholds": CHANNEL_THRESHOLDS,
               "mapping": {"dense": "dense_search", "recall": "authorized_sqlite_recall"}}
    receipt["passed"] = _active_healthy(receipt, receipt["checked_at"])
    return receipt


def evaluate_backfill_guard(passive: GuardDecision, *, stage: str,
                            active: dict[str, Any] | None = None, now: float | None = None) -> GuardDecision:
    if stage not in {"extract", "consolidate", "apply"}:
        raise ValueError("unknown backfill stage")
    if passive.action in {"capture_only", "skip", "pause"}:
        return GuardDecision("pause", passive.reasons, passive.metrics)
    malformed = passive.action != "allow" or any(r.startswith(("guard_error", "malformed")) for r in passive.reasons)
    missing = [name for name in ("dense", "hook") if not isinstance(passive.metrics.get(name), dict)
               or not isinstance(passive.metrics[name].get("n"), int) or passive.metrics[name]["n"] < 5
               or not _finite(passive.metrics[name].get("p95_ms"))]
    degraded = malformed or bool(missing) or any(r.startswith("insufficient_data") for r in passive.reasons)
    current = time.time() if now is None else now
    probed = _active_healthy(active, current)
    if stage == "extract":
        if degraded and probed:
            return GuardDecision("allow", ["concurrency_one", "active_probes_healthy"], passive.metrics)
        flags = ["malformed_monitoring"] if malformed else ["insufficient_data"] if missing else []
        return GuardDecision("allow", ["concurrency_one"] + passive.reasons + flags, passive.metrics)
    if degraded and not probed:
        reasons = ["active_probes_required"] + (["configuration_repair_required"] if malformed else [])
        return GuardDecision("pause", reasons, passive.metrics)
    return GuardDecision("allow", ["active_probes_healthy"] if degraded else passive.reasons, passive.metrics)


@dataclass
class PauseLatch:
    state: str = "running"
    pause_times: list[float] = field(default_factory=list)
    healthy_checks: list[float] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def observe(self, decision: GuardDecision, now: float) -> str:
        if self.state in MANUAL_LATCHES:
            return self.state
        if decision.action != "allow":
            if self.state != "latency_paused":
                self.pause_times = [t for t in self.pause_times if now - t <= 3600] + [now]
            self.state = "latency_paused_manual" if len(self.pause_times) >= 3 else "latency_paused"
            if "configuration_repair_required" in decision.reasons:
                self.state = "latency_paused_manual"
            self.healthy_checks.clear()
        elif self.state == "latency_paused":
            # Insufficient passive evidence is never a healthy recovery signal.
            if any(r.startswith(("insufficient_data", "guard_error", "malformed")) for r in decision.reasons):
                self.healthy_checks.clear()
                return self.state
            if not self.healthy_checks or now - self.healthy_checks[-1] >= 60:
                self.healthy_checks.append(now)
            if len(self.healthy_checks) >= 3:
                self.state = "running"
                self.healthy_checks.clear()
        return self.state

    def resume(self, *, evidence: dict[str, Any], now: float | None = None) -> None:
        if self.state == "retired":
            raise ValueError("retired cohorts cannot resume")
        if self.state in {"quality_paused", "budget_paused", "latency_paused_manual"}:
            if not evidence.get("diagnosis") or not evidence.get("disposition") or evidence.get("passed") is not True:
                raise ValueError("resume requires diagnosis, disposition and fresh passing evidence")
            stamp = evidence.get("checked_at")
            if not _finite(stamp) or not 0 <= (time.time() if now is None else now) - stamp <= 60:
                raise ValueError("resume evidence is stale")
        self.state = "running"
        self.evidence = evidence
        self.healthy_checks.clear()

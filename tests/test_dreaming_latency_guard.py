from types import SimpleNamespace

import pytest

from memorymaster.dreaming.backfill_guard import (
    PauseLatch, evaluate_backfill_guard, run_quiet_hours_active_probes,
)
from memorymaster.dreaming.latency_guard import GuardDecision, _hook_metrics


def _active(now=100):
    return {"passed": True, "checked_at": now, "channels": {
        key: {"n": 5, "p95_ms": 20, "max_ms": 30} for key in ("dense", "recall")}}


def _healthy():
    return GuardDecision("allow", ["within_bounds"], {
        "dense": {"n": 5, "p95_ms": 20}, "hook": {"n": 5, "p95_ms": 600}})


def test_hook_timing_and_latency_formats_pair_without_losing_same_format_events(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "hook.log"
    timing = "[2026-10-20T12:00:00Z] hook=recall event=timing total_ms=1000\n"
    legacy = timing.replace("event=timing", "event=latency_total")
    path.write_text(timing * 5 + legacy * 5)
    assert _hook_metrics(path, datetime(2026, 10, 20, tzinfo=timezone.utc)) == {"n": 5, "p95_ms": 1000}
    path.write_text(legacy * 10)
    assert _hook_metrics(path, datetime(2026, 10, 20, tzinfo=timezone.utc))["n"] == 10


@pytest.mark.parametrize("reason", ["insufficient_data", "guard_error:ValueError", "malformed"])
def test_quiet_or_degraded_monitoring_allows_stateless_extraction_only(reason):
    passive = GuardDecision("allow", [reason])
    assert evaluate_backfill_guard(passive, stage="extract", now=100).action == "allow"
    for stage in ("consolidate", "apply"):
        assert evaluate_backfill_guard(passive, stage=stage, now=100).action == "pause"
        assert evaluate_backfill_guard(passive, stage=stage, active=_active(), now=100).action == "allow"


def test_each_missing_passive_channel_requires_fresh_active_probes():
    passive = _healthy()
    passive.metrics["hook"]["n"] = 0
    assert evaluate_backfill_guard(passive, stage="apply", now=100).action == "pause"
    for active in (_active(39), _active(101), {"passed": True, "checked_at": 100}):
        assert evaluate_backfill_guard(passive, stage="apply", active=active, now=100).action == "pause"
    assert evaluate_backfill_guard(passive, stage="apply", active=_active(), now=100).action == "allow"


@pytest.mark.parametrize("action", ["capture_only", "skip"])
@pytest.mark.parametrize("stage", ["extract", "consolidate", "apply"])
def test_passive_breach_cannot_be_overridden_by_active_probes(action, stage):
    assert evaluate_backfill_guard(GuardDecision(action, ["dense_soft"]), stage=stage,
                                   active=_active(), now=100).action == "pause"


def test_active_probes_take_five_samples_per_channel_and_keep_no_content():
    ticks = iter(i / 1000 for i in range(20))
    calls = []
    result = run_quiet_hours_active_probes(lambda: calls.append("dense"), lambda: calls.append("recall"),
                                          clock=lambda: next(ticks), now=100)
    assert result["passed"] is True
    assert calls == ["dense"] * 5 + ["recall"] * 5
    assert result["channels"]["recall"]["n"] == 5
    assert result["mapping"]["recall"] == "authorized_sqlite_recall"


def test_wal_contention_probe_failure_cannot_authorize_application():
    def locked():
        raise RuntimeError("private database path: locked")

    result = run_quiet_hours_active_probes(lambda: None, locked, now=100)
    assert result["passed"] is False
    assert "private" not in str(result)
    assert evaluate_backfill_guard(GuardDecision("allow", ["insufficient_data"]), stage="apply",
                                   active=result, now=100).action == "pause"


def test_real_probe_adapter_requires_read_only_service_and_disables_access_writes(monkeypatch):
    from importlib import import_module

    calls = []
    service = SimpleNamespace(store=SimpleNamespace(read_only=True),
                              query_rows=lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(import_module("memorymaster.recall.dense_recall"), "search", lambda *args, **kwargs: None)
    receipt = run_quiet_hours_active_probes(service_factory=lambda: service, scope="project:test", now=100)
    assert receipt["passed"]
    assert len(calls) == 5 and all(c["record_accesses"] is False for c in calls)
    assert all(c["scope_allowlist"] == ["project:test"] for c in calls)
    service.store.read_only = False
    with pytest.raises(ValueError, match="read-only"):
        run_quiet_hours_active_probes(service_factory=lambda: service, scope="project:test")


def test_latency_pause_recovers_after_three_spaced_active_observations():
    latch = PauseLatch()
    assert latch.observe(GuardDecision("pause"), 0) == "latency_paused"
    quiet = GuardDecision("allow", ["insufficient_data"])
    assert latch.observe(evaluate_backfill_guard(quiet, stage="extract", now=1), 1) == "latency_paused"
    for stamp in (60, 90, 120):
        result = evaluate_backfill_guard(quiet, stage="apply", active=_active(stamp), now=stamp)
        assert latch.observe(result, stamp) == "latency_paused"
    assert latch.observe(evaluate_backfill_guard(quiet, stage="apply", active=_active(180), now=180), 180) == "running"


def test_third_pause_latches_manual_and_scheduler_cannot_clear_it():
    latch = PauseLatch()
    for episode in range(3):
        stamp = episode * 300
        state = latch.observe(GuardDecision("pause"), stamp)
        for offset in (60, 120, 180):
            latch.observe(_healthy(), stamp + offset)
    assert state == latch.state == "latency_paused_manual"
    with pytest.raises(ValueError):
        latch.resume(evidence={"passed": True})
    with pytest.raises(ValueError, match="stale"):
        latch.resume(evidence={"passed": True, "diagnosis": "fixed", "disposition": "retry", "checked_at": 0}, now=100)
    latch.resume(evidence={"passed": True, "diagnosis": "fixed", "disposition": "retry", "checked_at": 100}, now=100)
    assert latch.state == "running"


def test_guard_configuration_failure_requires_manual_repair():
    result = evaluate_backfill_guard(GuardDecision("allow", ["guard_error:ValueError"]), stage="apply", now=100)
    latch = PauseLatch()
    assert latch.observe(result, 100) == "latency_paused_manual"

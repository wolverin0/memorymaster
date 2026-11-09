"""T-2123: more captures per beat tick and more candidates per consolidation call, daily spend caps untouched."""

from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import MAX_CONSOLIDATE_CANDIDATES_CLAMP, DreamConfig, DreamWorker
from memorymaster.surfaces import scheduled_task

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]
PerCaptureExtractor = runpy.run_path("tests/test_beat_worker_cap.py")["_PerCaptureExtractor"]
CANDIDATES_ENV = "MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CANDIDATES"


def _pairs(count: int):
    candidate = H["_Extractor"]().extract([], scope="project:test", capture_hash="hash").candidates[0]
    return [({"id": 1, "scope": "project:test"}, replace(candidate, candidate_id=f"candidate-{n}")) for n in range(count)]


def _batch_sizes(config: DreamConfig, count: int) -> list[int]:
    worker = DreamWorker(None, None, None, H["_Consolidator"](), config=config)
    return [len(batch) for batch in worker._candidate_batches(_pairs(count))]


def test_defaults_are_fifteen_candidates_per_call_and_six_captures_per_tick(monkeypatch) -> None:
    monkeypatch.delenv(CANDIDATES_ENV, raising=False)
    assert DreamConfig().max_consolidate_candidates == DreamConfig.from_env().max_consolidate_candidates == 15
    assert _batch_sizes(DreamConfig(), 16) == [15, 1]
    assert scheduled_task.BEAT_MAX_SESSIONS == scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES == 6


def test_candidates_per_call_env_override_and_clamp(monkeypatch) -> None:
    monkeypatch.setenv(CANDIDATES_ENV, "8")
    assert DreamConfig.from_env().max_consolidate_candidates == 8
    assert _batch_sizes(DreamConfig.from_env(), 20) == [8, 8, 4]
    monkeypatch.setenv(CANDIDATES_ENV, "1000")
    config = DreamConfig.from_env()
    assert config.max_consolidate_candidates == 1000  # the config keeps the ask; the batching clamps it
    sizes = _batch_sizes(config, MAX_CONSOLIDATE_CANDIDATES_CLAMP + 5)
    assert sizes == [MAX_CONSOLIDATE_CANDIDATES_CLAMP, 5]
    assert _batch_sizes(DreamConfig(max_consolidate_candidates=0), 3) == [1, 1, 1]  # never below one


def test_the_old_hard_limit_of_five_is_gone_and_one_call_carries_the_whole_scope(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    for tag in "ABCDEFGH":  # 8 captures sharing one project scope: 5-per-call needed 4 calls, 15 needs 2
        H["_capture"](ledger, session_hash=f"s{tag}", extra_text=f" {tag}")
    consolidator = H["_Consolidator"]()
    worker = DreamWorker(ledger, H["_service"](tmp_path), PerCaptureExtractor(), consolidator,
                         config=DreamConfig(), now=lambda: NOW)

    summary = worker.run(apply_candidates=False)

    assert summary["consolidated"] == 8 and summary["errors"] == 0
    assert len(consolidator.scopes) == 2  # one personal call and one project call


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 6), ("5", 5), ("0", 1), ("-3", 1), ("999", scheduled_task.BEAT_MAX_PER_TICK_CLAMP), ("abc", 6),
])
def test_beat_limits_read_env_and_are_clamped(monkeypatch, raw, expected) -> None:
    env = scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES_ENV
    if raw is None:
        monkeypatch.delenv(env, raising=False)
    else:
        monkeypatch.setenv(env, raw)
    assert scheduled_task._beat_limit(env, scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES) == expected


def test_beat_tick_passes_env_limits_and_keeps_the_spend_caps(tmp_path, monkeypatch) -> None:
    from memorymaster.dreaming.latency_guard import GuardDecision

    monkeypatch.setenv(scheduled_task.BEAT_MAX_SESSIONS_ENV, "7")
    monkeypatch.setenv(scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES_ENV, "6")
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")
    monkeypatch.delenv("MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CALLS_DAILY", raising=False)
    monkeypatch.setattr(scheduled_task, "_log_path", lambda mode: tmp_path / f"{mode}.log")
    seen: dict = {}
    monkeypatch.setattr("memorymaster.dreaming.capture.scan_active_transcripts", lambda ledger, **kw: {})
    monkeypatch.setattr("memorymaster.dreaming.latency_guard.evaluate", lambda *a, **k: GuardDecision("allow", [], {}))
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream",
                        lambda db, ws, **kw: seen.update(kw) or {"ok": True, "errors": 0})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert scheduled_task.main(["beat", "--db", str(tmp_path / "memory.db"), "--workspace", str(workspace)]) == 0

    config = seen["config"]
    assert (config.max_sessions, config.max_consolidate_captures_per_run) == (7, 6)
    assert config.max_input_tokens_daily == 2_000_000 and config.max_consolidate_calls_daily == 12


@pytest.mark.parametrize("budget", ["calls", "tokens"])
def test_daily_budgets_still_defer_with_the_higher_throughput_defaults(tmp_path, budget) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [H["_capture"](ledger, session_hash=f"s{tag}", extra_text=f" {tag}") for tag in "ABC"]
    consolidator = H["_Consolidator"]()
    prior = ledger.start_run(True, consolidator.model, "fixture", now=NOW)
    ledger.record_provider_call(prior, provider=consolidator.provider, model=consolidator.model, outcome="ok",
                                latency_ms=10, structured_valid=True, input_tokens=2_000_000, output_tokens=1,
                                http_status=200, now=NOW)
    ledger.finish_run(prior, "completed", {}, now=NOW)
    config = DreamConfig(max_consolidate_calls_daily=1) if budget == "calls" else DreamConfig()
    assert config.max_consolidate_candidates == 15 and config.max_consolidate_captures_per_run == 0
    worker = DreamWorker(ledger, H["_service"](tmp_path), PerCaptureExtractor(), consolidator,
                         config=config, now=lambda: NOW)

    summary = worker.run(apply_candidates=False)

    assert consolidator.scopes == []  # the exhausted cap stopped every call
    assert summary["consolidated"] == 0 and summary["deferred_consolidate_budget"] == 3
    assert {ledger.get_capture(i)["state"] for i in ids} == {"extracted"}


@pytest.mark.parametrize("bad", [0, 7, 11])
def test_a_failing_packed_batch_is_bisected_not_retried_per_capture(tmp_path, bad) -> None:
    """One bad candidate in a 12-capture, 12-candidate batch must not burn a call per capture."""
    import math

    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [H["_extracted_capture"](ledger, session_hash=f"s{n}", candidate_id=f"c{n:02d}") for n in range(12)]

    class Poisoned(H["_Consolidator"]):
        sizes: list[int] = []
        failed = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            if any(c.candidate_id == f"c{bad:02d}" for c in candidates):
                type(self).failed += 1
                raise ValueError("invalid candidate")
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, H["_service"](tmp_path), H["_NoCall"](), Poisoned(),
                          config=DreamConfig(jev_triage_enabled=False), now=lambda: NOW).run(apply_candidates=False)

    depth = math.ceil(math.log2(12))
    assert Poisoned.sizes[0] == 12  # one packed call carried every capture
    assert Poisoned.failed <= depth + 1  # failing calls follow the bisection path
    assert len(Poisoned.sizes) <= 2 * depth + 1  # the old per-capture retry used 13 calls here
    states = [ledger.get_capture(i)["state"] for i in ids]
    assert states[bad] == "retryable"
    assert [s for n, s in enumerate(states) if n != bad] == ["consolidated"] * 11  # the good captures still apply
    assert summary["consolidated"] == 11

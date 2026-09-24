"""Jev read paths at production scale: identical answers, bounded time and memory.

The dashboard metrics, the weekly review queue and the operational review's
``jev_decisions`` check read the decisions ledger on every page load and every
review run.  A live recall hook writes ~300 decisions x 80 item rows a day, each
with a redacted state and a 120-ordering exploration distribution, so these reads
must never load ``state_redacted`` (or any other column they do not use) for every
decision, must not build one dict per item row, and must read the ledger once per
check instead of once per window.

* Differential: on a mixed ledger (every surface, fallbacks, held candidates,
  paraphrase pairs, operator labels) the optimized code returns exactly what the
  frozen pre-optimization code (``_jev_reference_reads``) returns.
* Performance: on the verifier's ledger (4000 recall decisions x 80 item rows over
  14 days) metrics over 7 days and the review queue take < 2 s, the operational
  check < 3 s, and each peaks below 150 MB of Python allocations.  Skipped when
  ``MEMORYMASTER_SKIP_PERF`` is set.
"""
from __future__ import annotations

import os
import time
import tracemalloc
from datetime import timedelta

import _jev_reference_reads as ref
import pytest
from _jev_synthetic_ledger import NOW, write_synthetic_ledger

from memorymaster.decisions import metrics as mt
from memorymaster.operations import operational_review as review
from memorymaster.surfaces import jev_review as jr
from memorymaster.surfaces.jev_review import ReadOnlyLedger


@pytest.fixture(autouse=True)
def _no_jev_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_JEV_", "MEMORYMASTER_DECISIONS_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def mixed(tmp_path_factory):
    path = tmp_path_factory.mktemp("jev-mixed") / "decisions.db"
    return ReadOnlyLedger(write_synthetic_ledger(path, decisions=1500, candidates=6, days=26, seed=5, mixed=True))


# ------------------------------------------------------------- differential ---

@pytest.mark.parametrize("days", [1, 7, 30])
def test_metrics_match_the_reference_implementation(mixed, days):
    since, until = NOW - timedelta(days=days), NOW
    expected = ref.compute_metrics(mixed, since=since, until=until, daily_usd_cap=0.01)
    actual = mt.compute_metrics(mixed, since=since, until=until, daily_usd_cap=0.01)
    assert set(actual["surfaces"]) == {"recall", "revalidate", "ingest", "dedup", "hints"}
    assert actual["calibration"] and actual["drift"] and actual["questions"]
    assert actual == expected


def test_metrics_match_the_reference_for_a_past_window(mixed):
    since, until = NOW - timedelta(days=20), NOW - timedelta(days=9, hours=5)
    assert mt.compute_metrics(mixed, since=since, until=until) == ref.compute_metrics(mixed, since=since, until=until)


def test_weekly_calibration_windows_match_four_reference_metric_runs(mixed):
    week = timedelta(days=7)
    windows = [(NOW - maturity - week - shift, NOW - shift)
               for maturity in (timedelta(hours=1), timedelta(days=7)) for shift in (timedelta(0), week)]
    actual = mt.calibration_windows(mixed, windows)
    expected = [ref.compute_metrics(mixed, since=since, until=until)["calibration"] for since, until in windows]
    assert any(entry for entry in expected)
    assert actual == expected


def test_ece_rises_match_the_reference(mixed, monkeypatch):
    # A negative rise threshold reports every comparable question, so the comparison itself is exercised.
    for threshold in (review.JEV_ECE_RISE, -1.0):
        monkeypatch.setattr(review, "JEV_ECE_RISE", threshold)
        monkeypatch.setattr(ref, "JEV_ECE_RISE", threshold)
        expected = ref._jev_ece_rises(mixed, NOW)
        assert review._jev_ece_rises(mixed, NOW) == expected
    assert expected, "the mixed ledger must have comparable weeks"


@pytest.mark.parametrize(("days", "size"), [(7, 20), (3, 5), (30, 60)])
def test_review_queue_matches_the_reference_implementation(mixed, days, size):
    expected = ref.select_review_queue(mixed, now=NOW, days=days, size=size)
    actual = jr.select_review_queue(mixed, now=NOW, days=days, size=size)
    assert len(expected) == size
    if size >= 20:
        assert {reason for entry in expected for reason in entry["reasons"]} >= {
            "near_threshold", "legacy_disagreement", "steward_disagreement", "score_decile"}
    assert actual == expected


# -------------------------------------------------------------- performance ---

perf = pytest.mark.skipif(bool(os.environ.get("MEMORYMASTER_SKIP_PERF")),
                          reason="MEMORYMASTER_SKIP_PERF is set")
PEAK_BYTES = 150 * 1024 * 1024


@pytest.fixture(scope="module")
def verifier_ledger(tmp_path_factory):
    if os.environ.get("MEMORYMASTER_SKIP_PERF"):
        pytest.skip("MEMORYMASTER_SKIP_PERF is set")
    path = tmp_path_factory.mktemp("jev-perf") / "decisions.db"
    return write_synthetic_ledger(path, decisions=4000, candidates=20, days=14, seed=0)


def _measure(call):
    started = time.perf_counter()
    result = call()
    elapsed = time.perf_counter() - started
    tracemalloc.start()
    try:
        call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, elapsed, peak


@perf
def test_metrics_over_7_days_are_fast_and_lean(verifier_ledger):
    ledger = ReadOnlyLedger(verifier_ledger)
    report, elapsed, peak = _measure(lambda: jr.metrics_payload(ledger, now=NOW, days=7))
    assert report["surfaces"]["recall"]["volume"] > 1900
    assert elapsed < 2.0, f"metrics 7d took {elapsed:.2f} s"
    assert peak < PEAK_BYTES, f"metrics 7d peaked at {peak / 2**20:.0f} MB"


@perf
def test_review_queue_is_fast_and_lean(verifier_ledger):
    ledger = ReadOnlyLedger(verifier_ledger)
    queue, elapsed, peak = _measure(lambda: jr.select_review_queue(ledger, now=NOW))
    assert len(queue) == jr.REVIEW_SIZE
    assert elapsed < 2.0, f"review queue took {elapsed:.2f} s"
    assert peak < PEAK_BYTES, f"review queue peaked at {peak / 2**20:.0f} MB"


@perf
def test_operational_check_is_fast_and_lean(verifier_ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    config = review.ReviewConfig(db=tmp_path / "memory.db", decisions_db=verifier_ledger)
    result, elapsed, peak = _measure(lambda: review.check_jev_decisions(config, now=NOW))
    assert result.verdict in (review.Verdict.PASS, review.Verdict.WARN), result.detail
    assert elapsed < 3.0, f"check_jev_decisions took {elapsed:.2f} s"
    assert peak < PEAK_BYTES, f"check_jev_decisions peaked at {peak / 2**20:.0f} MB"

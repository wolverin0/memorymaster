"""R10 — the provider-failure signal must have a producer.

WHY THIS EXISTS. `operational_health` counts provider failures and alerts above
a threshold of 0. A repo-wide sweep found **no `record_event` anywhere that
writes one**: `llm_provider` logged a warning on timeout, non-zero exit and
OSError and returned "". Of 2,410,028 production events, 4 matched the check's
pattern -- and they were `transition` rows.

So the check was inert twice over: R1 fixed the window it scanned, but there was
still nothing in the log to find. A `provider_failures: 0` meant "nothing here
is observable", which is the exact substitution this whole remediation is about
-- "the machine ran" read as "the work happened".

Two things had to be true for the fix to count, and both are pinned here:
  1. a real provider failure lands in the event log, and the health check finds it;
  2. it does so WITHOUT recreating R9 -- a provider that fails on every call must
     not write a row per call.

Fail-without-fix status, verified by stashing the tracked half of the fix: 4 of
the 6 tests below fail on the unfixed tree, with the four failures naming the
four distinct defects (no queryable trace, no attribution, no producer evidence,
no wiring). The other two exercise `core/provider_health.py` itself, which does
not exist before the fix -- they pin its contract (throttling; read-only stores
are never claimed as producers) rather than R10's absence.
"""
from __future__ import annotations

import gc
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from memorymaster.core import llm_provider, observability, provider_health
from memorymaster.core.service import MemoryService
from memorymaster.govern.operational_health import _provider_failure_count


@pytest.fixture(autouse=True)
def _clean_signal_state():
    """provider_health holds process-wide sink + throttle state."""
    provider_health.reset()
    observability.reset_metrics()
    yield
    provider_health.reset()
    observability.reset_metrics()


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "r10.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _fail_every_http_call(monkeypatch, exc: Exception) -> None:
    def _boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


@pytest.mark.unit
def test_a_provider_failure_becomes_a_queryable_event(tmp_path: Path, monkeypatch) -> None:
    """The core of R10: the health check must be able to FIND a failure.

    Before the fix `_http_post` logged and returned "", so this scan saw nothing
    no matter how wide its window was.
    """
    svc = _svc(tmp_path)
    provider_health.register_store_sink(svc.store)
    _fail_every_http_call(monkeypatch, TimeoutError("provider did not answer"))

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.delenv("MEMORYMASTER_LLM_KEY_ROTATION", raising=False)
    assert llm_provider._call_google("prompt", "text") == ""

    result = _provider_failure_count(svc)

    assert result["count"] >= 1, "a provider failure left no queryable trace"
    assert result["structured_events"] >= 1, "the failure was not recorded as a typed signal"
    assert "google" in result["by_provider"], (
        f"the failure was not attributed to a provider: {result['by_provider']}"
    )


@pytest.mark.unit
def test_an_http_error_is_attributed_to_its_provider(tmp_path: Path, monkeypatch) -> None:
    """The 429/5xx branch is the one an outage actually takes."""
    svc = _svc(tmp_path)
    provider_health.register_store_sink(svc.store)
    _fail_every_http_call(
        monkeypatch,
        urllib.error.HTTPError("http://provider", 503, "unavailable", {}, None),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")

    assert llm_provider._call_openai("prompt", "text") == ""

    result = _provider_failure_count(svc)
    assert result["by_provider"].get("openai", 0) >= 1
    assert observability.metric_value(
        "llm_provider_failures_total", provider="openai", reason="http_503"
    ) == 1


@pytest.mark.unit
def test_the_new_producer_does_not_become_the_next_flood(tmp_path: Path, monkeypatch) -> None:
    """R9's lesson applied forward. A misconfigured provider fails on EVERY
    call; a row per call would rebuild the 489k-row flood in a new place.

    One row per (provider, reason) per interval, carrying the count it stands
    for -- so nothing is lost and nothing floods.
    """
    svc = _svc(tmp_path)
    provider_health.register_store_sink(svc.store)

    for _ in range(50):
        provider_health.record_provider_failure("google", "http_429")

    rows = [
        e
        for e in svc.list_events(event_type=provider_health.PROVIDER_FAILURE_EVENT_TYPE, limit=500)
        if provider_health.parse_provider_failure(e) is not None
    ]
    assert len(rows) == 1, f"50 identical failures wrote {len(rows)} rows"
    assert observability.metric_value(
        "llm_provider_failures_total", provider="google", reason="http_429"
    ) == 50, "the counter must still see every failure"

    # And the single row must say how many it represents, or suppression would
    # be its own inert signal.
    monkeypatch.setenv("MEMORYMASTER_PROVIDER_FAILURE_EVENT_INTERVAL_SECONDS", "0")
    provider_health.record_provider_failure("google", "http_429")
    written = [
        provider_health.parse_provider_failure(e)
        for e in svc.list_events(event_type=provider_health.PROVIDER_FAILURE_EVENT_TYPE, limit=500)
    ]
    assert any((p or {}).get("occurrences", 0) == 50 for p in written), (
        f"the suppressed failures were not accounted for: {written}"
    )


@pytest.mark.unit
def test_a_zero_says_whether_it_is_evidence(tmp_path: Path, monkeypatch) -> None:
    """The honesty requirement. A producer only exists in a process holding a
    writable store, so `0` still has to distinguish "nothing failed" from
    "nothing here has ever been able to report a failure"."""
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    monkeypatch.delenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", raising=False)
    svc = _svc(tmp_path)

    unproven = _provider_failure_count(svc)
    assert unproven["count"] == 0
    assert unproven["producers"]["google"] == "unproven", (
        "a 0 from a log that has never carried this signal was reported as an all-clear"
    )

    provider_health.register_store_sink(svc.store)
    provider_health.record_provider_failure("google", "timeout")

    proven = _provider_failure_count(svc)
    assert proven["producers"]["google"] == "observed"


@pytest.mark.unit
def test_creating_a_store_wires_the_producer(tmp_path: Path) -> None:
    """`llm_provider` has no store handle and observability counters are
    in-memory only, so the sink is what carries the signal across the process
    boundary to the checker. Nothing wires it at the ~25 call sites -- the store
    factory does, once."""
    from memorymaster.stores.store_factory import create_store

    assert not provider_health.sink_registered()
    store = create_store(str(tmp_path / "wired.db"))
    assert provider_health.sink_registered(), "a writable store left no durable producer"

    # And the answer must stop being yes once the store is gone. A registered
    # sink over a dead weak reference would be this remediation's own bug:
    # a mechanism reporting that it is wired while doing nothing.
    del store
    gc.collect()
    assert not provider_health.sink_registered(), (
        "the sink reported itself wired after its store was collected"
    )


@pytest.mark.unit
def test_a_read_only_store_is_not_registered(tmp_path: Path) -> None:
    """A read-only store cannot write; claiming it as the producer would be the
    same lie in a new place."""
    from memorymaster.stores.store_factory import create_store

    db = tmp_path / "ro.db"
    MemoryService(db, workspace_root=tmp_path).init_db()
    provider_health.reset()

    create_store(str(db), read_only=True)

    assert not provider_health.sink_registered()

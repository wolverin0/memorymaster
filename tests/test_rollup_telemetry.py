"""Tests for the rollup_telemetry feature.

Each test anchors on WHY the telemetry matters, not just the mechanics:

* Recall pressure must be observable per source agent so operators can see
  which agents are reading memory (mirror of ingest telemetry).
* The recall counter must increment on a served query WITHOUT bleeding into
  the ingest counter — they answer different operational questions.
* Session-level activity must be wired on query so the usage rollup can show
  *who* is actively recalling, not just aggregate counts.
* The MCP rollup tool must expose aggregate visibility (counters + sessions)
  while never leaking sensitive claim text.
* metrics_text must emit a well-formed Prometheus counter family so an
  external scraper can ingest the new ``recalls_queried_total`` series.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from memorymaster.core import observability
from memorymaster.core.service import MemoryService
from memorymaster.surfaces import mcp_server
from memorymaster.surfaces.session_tracker import SessionTracker


@pytest.fixture(autouse=True)
def _clean_metrics():
    """Each test starts from a clean metric registry (counters are global)."""
    observability.reset_metrics()
    yield
    observability.reset_metrics()


def test_bump_recalls_queried():
    """A recall counter must accumulate per source agent so operators can see
    which agent is driving recall load — and an agent that never queried must
    read zero (no phantom attribution)."""
    observability.bump_recalls_queried("agent-x")
    observability.bump_recalls_queried("agent-x")

    assert observability.metric_value("recalls_queried_total", source_agent="agent-x") == 2
    # An unrelated agent must not inherit another agent's recall count.
    assert observability.metric_value("recalls_queried_total", source_agent="agent-y") == 0


def _row_with_claim(claim_id: int) -> dict:
    return {"claim": SimpleNamespace(id=claim_id, status="confirmed", visibility="public")}


def test_record_accesses_emits_counter():
    """Serving a query must bump the recall counter exactly once, and must NOT
    touch the ingest counter — recall and ingest are distinct signals and a
    cross-counter bleed would silently corrupt usage dashboards."""
    svc = MemoryService.__new__(MemoryService)  # bypass DB/store construction
    svc.store = SimpleNamespace(read_only=False, db_path="")  # no record_access* methods
    svc.source_agent = "recall-agent"
    svc.session_id = None

    before_ingest = observability.metric_value("claims_ingested_total", source_agent="recall-agent")
    svc._record_accesses([_row_with_claim(1), _row_with_claim(2)], query_text="hello")

    assert observability.metric_value("recalls_queried_total", source_agent="recall-agent") == 1
    # No cross-counter bleed: a recall must never look like an ingest.
    assert observability.metric_value("claims_ingested_total", source_agent="recall-agent") == before_ingest


def test_record_accesses_no_results_does_not_count():
    """An empty result set is not a recall worth counting — telemetry should
    reflect *served* memory, otherwise the metric overstates useful recall."""
    svc = MemoryService.__new__(MemoryService)
    svc.store = SimpleNamespace(read_only=False, db_path="")
    svc.source_agent = "recall-agent"
    svc.session_id = None

    svc._record_accesses([], query_text="nothing")

    assert observability.metric_value("recalls_queried_total", source_agent="recall-agent") == 0


def test_session_tracker_wired_on_query(tmp_path):
    """Binding a session to the service must record query activity, so the
    usage rollup can attribute live recall to a concrete agent session — the
    whole point of session-scoped telemetry."""
    db_path = str(tmp_path / "sessions.db")
    tracker = SessionTracker(db_path)
    session_id = tracker.start_session("seeded-agent")

    svc = MemoryService.__new__(MemoryService)
    svc.store = SimpleNamespace(read_only=False, db_path=db_path)
    svc.source_agent = "seeded-agent"
    svc.session_id = session_id

    svc._record_accesses([_row_with_claim(7)], query_text="topic")

    active = {s["id"]: s for s in tracker.get_active_sessions()}
    assert active[session_id]["queries_made"] == 1


def test_default_service_has_no_session_binding():
    """Default-safe guarantee: a freshly constructed service binds no session,
    so recall behaviour is byte-identical to pre-feature — no surface that
    forgets to opt in can accidentally start mutating agent_sessions."""
    svc = MemoryService.__new__(MemoryService)
    # Mirror what __init__ sets without touching the real store/DB.
    svc.store = SimpleNamespace(read_only=False, db_path="")
    svc.source_agent = None
    svc.session_id = None

    # No session_id => record_activity is never invoked even though a query is
    # served (would raise if it tried to open an empty db_path).
    svc._record_accesses([_row_with_claim(1)], query_text="q")

    assert svc.session_id is None
    assert observability.metric_value("recalls_queried_total", source_agent="unknown") == 1


def test_get_usage_rollup_mcp_tool(tmp_path):
    """The rollup payload must surface both recall counters and active sessions,
    be JSON-serialisable for transport, and must NOT echo claim text — this is
    an operator/observability surface, not a memory read path."""
    db_path = str(tmp_path / "rollup.db")
    tracker = SessionTracker(db_path)
    tracker.start_session("dash-agent")
    observability.bump_recalls_queried("dash-agent")

    payload = mcp_server._usage_rollup(db_path)

    # Must be valid JSON (transport-safe).
    encoded = json.dumps(payload)
    assert "recalls_queried_total" in payload
    assert "active_sessions" in payload
    assert payload["recalls_queried_total"] == 1
    assert any(s["agent_id"] == "dash-agent" for s in payload["active_sessions"])

    # Session rows expose only metadata columns, never free-text claim payloads.
    secret = "super-secret-claim-body"
    assert secret not in encoded
    for session in payload["active_sessions"]:
        assert set(session).issubset(
            {"id", "agent_id", "session_start", "last_activity", "claims_ingested", "queries_made"}
        )


def test_metrics_text_includes_recalls():
    """metrics_text must render the recall series as a valid Prometheus counter
    family (HELP + TYPE + a labelled sample) so an external scraper can ingest
    it without choking on a malformed family."""
    observability.bump_recalls_queried("scrape-agent")
    text = observability.metrics_text()

    assert "# HELP recalls_queried_total" in text
    assert "# TYPE recalls_queried_total counter" in text
    assert re.search(
        r'^recalls_queried_total\{source_agent="scrape-agent"\} 1$',
        text,
        flags=re.MULTILINE,
    )

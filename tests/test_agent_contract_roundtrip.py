"""End-to-end round-trip tests for the P4 3-beat agent contract (BEAT 3).

Intent (anchored on the contract, not the implementation):

  The 3-beat contract requires that when a non-Claude agent (Codex / generic /
  Hermes) closes a session via the DOCUMENTED ingest path with an explicit
  ``source_agent``, that claim is:
    (1) actually persisted (recall/query returns it), AND
    (2) attributed to that exact agent in the per-agent provenance view.

  This is what makes provenance meaningful (P3 made ``source_agent`` reliable) and
  what the Codex BEAT-3 reference script (``scripts/agent_session_end_ingest.py``)
  exists to guarantee even when the agent forgets to ingest.

These tests would FAIL if:
  * the reference script ever raw-INSERTs instead of routing through
    ``service.ingest`` (attribution + filter would not run reliably),
  * ``source_agent`` were dropped on the ingest path (the regression P4 fixes for
    the old Codex autologger),
  * the provenance panel stopped grouping by ``source_agent``,
  * the <=3-distilled batch cap were removed (the intake batch fence test).

All tests use a tmp DB — never the live ``memorymaster.db``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable for the reference-script unit-level assertions.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_session_end_ingest as session_end  # noqa: E402

from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.surfaces.dashboard import _provenance_rows  # noqa: E402


@pytest.fixture
def svc(tmp_path):
    """Real MemoryService on a tmp SQLite DB (never the live memorymaster.db)."""
    service = MemoryService(db_target=str(tmp_path / "contract.db"), workspace_root=tmp_path)
    service.init_db()
    return service


def _agent_row(rows: list[dict], agent: str) -> dict | None:
    for row in rows:
        if row["agent"] == agent:
            return row
    return None


# ---------------------------------------------------------------------------
# Core contract: documented ingest path -> recall AND provenance attribution
# ---------------------------------------------------------------------------


def test_documented_ingest_with_source_agent_is_recallable_and_attributed(svc):
    """BEAT 3 round-trip: a claim ingested via service.ingest with an explicit
    source_agent must be (1) queryable and (2) counted under that agent in the
    provenance view. This is the whole point of the contract."""
    claim = svc.ingest(
        text="The codex autologger must set source_agent or claims land as unknown.",
        citations=[CitationInput(source="codex-session", locator="project:demo")],
        claim_type="constraint",
        subject="codex-autologger",
        predicate="attribution",
        scope="project:demo",
        confidence=0.6,
        source_agent="codex-session",
    )
    assert claim.source_agent == "codex-session"

    # (1) recall/query returns it (candidates included — a fresh claim is a candidate)
    hits = svc.query("codex autologger source_agent", limit=10, include_candidates=True)
    assert any(getattr(h, "id", None) == claim.id for h in hits), (
        f"ingested claim {claim.id} not recallable: {[getattr(h, 'id', None) for h in hits]}"
    )

    # (2) provenance view counts it under codex-session
    rows = _provenance_rows(svc)
    codex = _agent_row(rows, "codex-session")
    assert codex is not None, f"codex-session missing from provenance rows: {rows}"
    assert codex["total"] >= 1
    assert codex["last_ingest"] is not None


def test_provenance_isolates_distinct_agents(svc):
    """Provenance must isolate per agent — two agents must not be merged into one
    bucket, otherwise the panel can't show 'how much each agent contributes'."""
    svc.ingest(
        text="Hermes VM bridge writes through MCP ingest, never raw INSERT.",
        citations=[CitationInput(source="hermes-vm", locator="project:demo")],
        scope="project:demo",
        source_agent="hermes-vm",
    )
    svc.ingest(
        text="Claude session-end hook ingests at most three distilled learnings.",
        citations=[CitationInput(source="claude-session", locator="project:demo")],
        scope="project:demo",
        source_agent="claude-session",
    )
    rows = _provenance_rows(svc)
    agents = {r["agent"] for r in rows}
    assert {"hermes-vm", "claude-session"} <= agents
    assert _agent_row(rows, "hermes-vm")["total"] >= 1
    assert _agent_row(rows, "claude-session")["total"] >= 1


def test_null_source_agent_collapses_to_visible_bucket(svc):
    """A claim ingested WITHOUT source_agent must still be visible in provenance
    under an explicit '<null>' bucket — these are exactly the rows the Codex
    BEAT-3 script exists to convert into a clean 'codex-session' total, so they
    must not silently vanish from the panel."""
    svc.ingest(
        text="A claim with no attribution should be visible as unattributed.",
        citations=[CitationInput(source="x", locator="project:demo")],
        scope="project:demo",
    )
    rows = _provenance_rows(svc)
    # source_agent NULL or default-tagged ("unknown") both must be visible.
    visible = {r["agent"] for r in rows}
    assert ("<null>" in visible) or ("unknown" in visible), f"unattributed bucket missing: {rows}"


# ---------------------------------------------------------------------------
# Reference script: distilled ingest sets attribution + caps the batch
# ---------------------------------------------------------------------------


def test_reference_script_ingests_with_attribution_via_service(svc, tmp_path, monkeypatch):
    """The Codex/generic reference script must route distilled learnings through
    service.ingest with the requested source_agent — closing the BEAT-3 gap. We
    stub the LLM distill step so the test is deterministic and offline; the
    ingest path (attribution + provenance) is the real assertion."""
    learnings = [
        {"text": "The 403 was caused by a missing RLS policy on the claims table.",
         "claim_type": "fact", "subject": "rls", "predicate": "root_cause"},
    ]
    monkeypatch.setattr(session_end, "_extract_assistant_text", lambda _p: "non-empty transcript text")
    monkeypatch.setattr(session_end, "_distill", lambda _t: learnings)

    db_path = svc.store.db_path if hasattr(svc.store, "db_path") else str(tmp_path / "contract.db")
    ingested = session_end.run(
        db_path,
        str(tmp_path / "rollout.jsonl"),  # path content is irrelevant — _extract is stubbed
        source_agent="codex-session",
        cwd=str(tmp_path),
    )
    assert ingested == 1

    rows = _provenance_rows(svc)
    codex = _agent_row(rows, "codex-session")
    assert codex is not None and codex["total"] >= 1


def test_reference_script_requires_non_empty_source_agent(svc, tmp_path):
    """source_agent is NON-NEGOTIABLE: the script must refuse to ingest with an
    empty attribution rather than silently writing an 'unknown' claim."""
    with pytest.raises(ValueError):
        session_end.ingest_learnings(
            str(tmp_path / "contract.db"),
            [{"text": "anything at all here", "claim_type": "fact"}],
            source_agent="   ",
            cwd=str(tmp_path),
        )


def test_reference_script_caps_at_three_learnings(svc, tmp_path):
    """The <=3 distilled norm must be enforced: even if a (buggy or tampered)
    distiller returns 5 learnings, the script ingests at most MAX_LEARNINGS and
    fences the batch via intake_batch_max."""
    db_path = svc.store.db_path if hasattr(svc.store, "db_path") else str(tmp_path / "contract.db")
    many = [
        {"text": f"Distinct learning number {i} about the build pipeline.",
         "claim_type": "fact", "subject": f"topic{i}", "predicate": "note"}
        for i in range(5)
    ]
    ingested = session_end.ingest_learnings(
        db_path, many, source_agent="codex-session", cwd=str(tmp_path)
    )
    assert ingested <= session_end.MAX_LEARNINGS == 3


def test_reference_script_drops_sensitive_learnings(svc, tmp_path):
    """The sensitivity filter is the firewall: a distilled learning carrying a
    secret must be dropped BEFORE ingest, never persisted. This guards the
    non-negotiable 'do not weaken the sensitivity filter' boundary."""
    db_path = svc.store.db_path if hasattr(svc.store, "db_path") else str(tmp_path / "contract.db")
    secret = "The API key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ingested = session_end.ingest_learnings(
        db_path,
        [{"text": secret, "claim_type": "fact", "subject": "leak", "predicate": "token"}],
        source_agent="codex-session",
        cwd=str(tmp_path),
    )
    assert ingested == 0, "sensitive learning must be dropped, not ingested"
    rows = _provenance_rows(svc)
    assert _agent_row(rows, "codex-session") is None


# ---------------------------------------------------------------------------
# Transcript extraction works for both Claude-style and Codex-rollout shapes
# ---------------------------------------------------------------------------


def test_provenance_http_route_returns_agent_buckets(svc, tmp_path):
    """The /api/provenance GET route must serve the per-agent buckets end-to-end,
    so the dashboard panel can render them. Exercises the real HTTP handler, not
    just the helper, to catch route-registration regressions."""
    import http.client
    import threading

    from memorymaster.surfaces.dashboard import create_dashboard_server

    svc.ingest(
        text="Codex session-end ingest must set source_agent for provenance.",
        citations=[CitationInput(source="codex-session", locator="project:demo")],
        scope="project:demo",
        source_agent="codex-session",
    )
    server = create_dashboard_server(
        service=svc,
        db_target="prov-test.db",
        host="127.0.0.1",
        port=0,
        operator_log_jsonl=tmp_path / "op.jsonl",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/provenance")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert body["ok"] is True
    assert body["attribution"]["recall_attributed"] is False
    agents = {row["agent"] for row in body["agents"]}
    assert "codex-session" in agents


def test_extract_handles_codex_rollout_and_claude_shapes(tmp_path):
    """The extractor must read both Claude-Code transcripts (role under `message`)
    and Codex rollout JSONL (role under `payload`), so the same reference script
    serves both agent classes."""
    claude_line = {"message": {"role": "assistant", "content": "Claude decided to use WAL mode for the store because it prevents corruption under concurrency."}}
    codex_line = {"payload": {"role": "assistant", "content": "Codex found the deadlock came from holding the write lock during fsync."}}
    path = tmp_path / "mixed.jsonl"
    path.write_text(json.dumps(claude_line) + "\n" + json.dumps(codex_line) + "\n", encoding="utf-8")
    text = session_end._extract_assistant_text(str(path))
    assert "WAL mode" in text
    assert "deadlock" in text

"""Fixes from the fresh-eyes gap audit (artifacts/2026-07-01-fresh-eyes-gap-audit.html).

WHY these tests exist: the audit found the LLM-built features fail at SEAMS, not
units — tested-but-unexercised paths, cross-feature gaps, single-bound edge
cases. Each test here anchors on the seam that was broken, phrased so it fails
again if the seam re-opens.

A1 born-inverted hole · A2 dead SessionTracker · B1 checkpoint×holder seam ·
B2 CLI holder · B3 volunteer gate default · B4 unwired PreToolUse hook ·
B5 holder read filter.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memorymaster.core.models import CitationInput, _parse_iso_strict
from memorymaster.core.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    s = MemoryService(db_target=str(tmp_path / "fixes.db"), workspace_root=tmp_path)
    s.init_db()
    return s


# --- A1: bitemporal single-bound hole ---------------------------------------

def test_past_valid_until_alone_cannot_create_inverted_row(svc):
    """THE hole: valid_until alone in the past used to combine with the store's
    valid_from=now auto-populate into a born-inverted row. Now valid_from is
    backdated so the stored interval can never invert — and ingestion still
    succeeds (rejecting would break the legitimate 'stopped being true at X')."""
    claim = svc.ingest(
        "The subscription was valid until last month",
        [CitationInput(source="test")],
        scope="project:test",
        valid_until="2026-06-01T00:00:00+00:00",
    )
    stored = svc.store.get_claim(claim.id)
    vf = _parse_iso_strict("valid_from", stored.valid_from)
    vu = _parse_iso_strict("valid_until", stored.valid_until)
    assert vf is not None and vu is not None
    assert vf <= vu, f"born-inverted row: valid_from={stored.valid_from} > valid_until={stored.valid_until}"


def test_future_valid_until_alone_keeps_now_auto_populate(svc):
    """The non-buggy half must stay intact: a FUTURE valid_until without
    valid_from still auto-populates valid_from=now (now < future, no inversion)."""
    future = "2030-01-01T00:00:00+00:00"
    claim = svc.ingest(
        "Contract runs until 2030",
        [CitationInput(source="test")],
        scope="project:test",
        valid_until=future,
    )
    stored = svc.store.get_claim(claim.id)
    vf = _parse_iso_strict("valid_from", stored.valid_from)
    assert vf is not None
    # valid_from is "now", not backdated to the future bound.
    assert vf <= datetime.now(timezone.utc)
    assert vf <= _parse_iso_strict("valid_until", stored.valid_until)


# --- A2: SessionTracker was runtime-dead ------------------------------------

@pytest.fixture(autouse=True)
def _clear_telemetry_session_cache():
    from memorymaster.surfaces import mcp_server as m

    m._TELEMETRY_SESSION_IDS.clear()
    yield
    m._TELEMETRY_SESSION_IDS.clear()


def test_mcp_service_factory_binds_a_telemetry_session(tmp_path):
    """THE dead feature: nothing ever called start_session, so
    get_usage_rollup's session half was always []. The MCP _service() factory
    must now bind a real session so per-session telemetry is alive."""
    from memorymaster.surfaces.mcp_server import _service

    db = str(tmp_path / "mcp.db")
    svc = _service(db, str(tmp_path))
    assert isinstance(svc.session_id, int) and svc.session_id > 0
    assert svc.source_agent  # counters get a real label, not "unknown"


def test_query_activity_reaches_the_session_rollup(tmp_path):
    """End-to-end proof the rollup half is no longer decorative: a real query
    through a factory-built service increments queries_made in agent_sessions."""
    from memorymaster.surfaces.mcp_server import _service
    from memorymaster.surfaces.session_tracker import SessionTracker

    db = str(tmp_path / "mcp2.db")
    svc = _service(db, str(tmp_path))
    svc.init_db()
    svc.ingest("a durable fact", [CitationInput(source="t")], scope="project:x")
    # include_candidates so the fresh (candidate) claim is actually SERVED —
    # _record_accesses only records activity for served rows.
    rows = svc.query_rows("durable fact", limit=5, include_candidates=True)
    assert rows, "query served nothing — activity would legitimately not record"
    sessions = SessionTracker(db).get_active_sessions()
    assert sessions, "session rollup still empty — SessionTracker dead again"
    assert any(s.get("queries_made", 0) >= 1 for s in sessions)


def test_session_is_reused_within_a_process(tmp_path):
    """One session per DB per process — not a new row per tool call."""
    from memorymaster.surfaces.mcp_server import _service

    db = str(tmp_path / "mcp3.db")
    a = _service(db, str(tmp_path))
    b = _service(db, str(tmp_path))
    assert a.session_id == b.session_id


# --- B1: checkpoint × holder seam --------------------------------------------

def test_checkpoint_batch_items_carry_holder(svc):
    """Cross-feature seam: checkpoint (batch 1) predated holder (batch 2) and
    silently dropped it. Batch items must be at field parity with ingest_claim."""
    from memorymaster.surfaces.mcp_server import _checkpoint_batch

    res = _checkpoint_batch(
        svc,
        [{"text": "codex believes the cache is stale", "holder": "codex"}],
        default_scope="project:test",
        workspace=".",
        source_agent="test-agent",
    )
    assert res["ingested"] == 1
    assert svc.store.get_claim(res["claim_ids"][0]).holder == "codex"


# --- B2: CLI --holder ---------------------------------------------------------

def test_cli_ingest_accepts_holder_flag():
    """The CLI was an ingest surface that couldn't set holder at all."""
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args(
        ["ingest", "--text", "x", "--source", "s|l|e", "--holder", "gonzalo"]
    )
    assert args.holder == "gonzalo"


# --- B3: volunteer_context default gate ---------------------------------------

def test_volunteer_default_gate_actually_gates(tmp_path):
    """volunteer_context's whole point vs query_for_context is the confidence
    gate — at the old default (0.0) it gated nothing. The default must now
    exclude low-confidence claims while keeping high-confidence ones.

    Ingest goes through the MCP ingest_claim tool with the SAME workspace so
    the derived project scope matches volunteer_context's scope allowlist
    (mirrors tests/test_push_volunteer_context.py)."""
    from memorymaster.surfaces.mcp_server import ingest_claim, init_db, volunteer_context

    db = str(tmp_path / "volunteer.db")
    ws = str(tmp_path)
    init_db(db=db, workspace=ws)
    ingest_claim(text="volunteerfixtoken weak guess", db=db, workspace=ws,
                 confidence=0.3, source_agent="t")
    ingest_claim(text="volunteerfixtoken strong fact", db=db, workspace=ws,
                 confidence=0.9, source_agent="t")
    res = volunteer_context(query="volunteerfixtoken", db=db, workspace=ws)
    assert res["ok"] is True
    assert res["min_confidence"] >= 0.5, "default gate is open again — tool adds nothing"
    included_texts = json.dumps(res.get("claims", [])) + str(res.get("output", ""))
    assert "strong fact" in included_texts
    assert "weak guess" not in included_texts


# --- B5: holder read filter ----------------------------------------------------

def test_list_claims_filters_by_holder(svc):
    """holder was WRITE-ONLY: no surface could read claims back by holder,
    making the takes-vs-facts field decorative. The read filter closes it."""
    svc.ingest("alice thinks X", [CitationInput(source="t")], scope="project:test", holder="alice")
    svc.ingest("bob thinks Y", [CitationInput(source="t")], scope="project:test", holder="bob")
    svc.ingest("holderless fact", [CitationInput(source="t")], scope="project:test")

    alice = svc.list_claims(holder="alice")
    assert [c.holder for c in alice] == ["alice"]
    everyone = svc.list_claims()
    assert len(everyone) == 3  # no filter → unchanged behavior


# --- B4: PreToolUse hook was shipped unwired ------------------------------------

@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    """Redirect setup_hooks' filesystem targets into tmp_path (never real HOME)."""
    import memorymaster.surfaces.setup_hooks as sh

    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    monkeypatch.setattr(sh, "HOME", home)
    monkeypatch.setattr(sh, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(sh, "CLAUDE_JSON", home / ".claude.json")
    monkeypatch.setattr(sh, "CODEX_DIR", home / ".codex")
    return claude_dir


def test_installer_registers_pretooluse_hook_when_opted_in(hermetic_home):
    """The template was COPIED to disk but never REGISTERED — README's 'opt-in
    hook' silently required hand-editing settings.json. --pretooluse must now
    write a real PreToolUse registration."""
    import memorymaster.surfaces.setup_hooks as sh

    sh.install_hooks({"provider": "ollama", "api_key": "", "model": "llama3.2:3b"},
                     include_pretooluse=True)
    settings = json.loads((hermetic_home / "settings.json").read_text(encoding="utf-8"))
    ptu = settings["hooks"].get("PreToolUse", [])
    ours = [h for h in ptu if "memorymaster-pretooluse-recall" in json.dumps(h)]
    assert len(ours) == 1
    assert ours[0]["matcher"] == "Grep|Glob"


def test_installer_default_does_not_register_pretooluse(hermetic_home):
    """Opt-in means opt-in: the default install must NOT add the extra
    per-Grep/Glob recall injection."""
    import memorymaster.surfaces.setup_hooks as sh

    sh.install_hooks({"provider": "ollama", "api_key": "", "model": "llama3.2:3b"})
    settings = json.loads((hermetic_home / "settings.json").read_text(encoding="utf-8"))
    ptu = settings["hooks"].get("PreToolUse", [])
    assert not [h for h in ptu if "memorymaster-pretooluse-recall" in json.dumps(h)]

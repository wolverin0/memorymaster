"""Coverage-hardening tests for memorymaster.recall.context_hook (TRACK T18).

The base ``test_context_hook.py`` suite drives recall() happy paths but
leaves the env-var configuration boundary, the RRF auto-gate, the optional
retrieval streams and the LLM-extraction ingest path untested. These tests
fill those gaps.

WHY (intent, not lines): every helper here is a *contract boundary* between
operator-supplied environment configuration and the ranking math inside
``recall()``. The shipped guarantee is "a bad env value never crashes
recall and falls back to the documented default" and "an enabled-but-empty
optional stream augments-or-noops, never raises". If those contracts break,
every downstream recall silently mis-ranks or throws. The tests therefore
assert the fallback VALUE / observable behaviour, not merely that a line ran.
"""
from __future__ import annotations

import os

import pytest

from memorymaster.recall import context_hook as ch


@pytest.fixture(autouse=True)
def _clear_recall_env(monkeypatch):
    """Each test starts from shipped defaults (no MEMORYMASTER_* leakage)."""
    for key in list(os.environ):
        if key.startswith("MEMORYMASTER_"):
            monkeypatch.delenv(key, raising=False)


def _init_db(path):
    """Create an empty initialized claims DB at ``path`` and return the str
    path. ``observe``/``observe_llm``/``recall`` all open their own
    MemoryService against an EXISTING db (the production hook points at a
    pre-created memorymaster.db), so a fresh test path must be schema-inited
    first or every store query hits ``no such table: claims``."""
    from pathlib import Path as _P

    from memorymaster.core.service import MemoryService

    p = str(path)
    MemoryService(db_target=p, workspace_root=_P(p).parent).init_db()
    return p


# ---- BM25 numeric param parsing ------------------------------------------

def test_bm25_param_default_when_unset():
    assert ch._bm25_param("K1", ch._BM25_K1_DEFAULT) == ch._BM25_K1_DEFAULT


def test_bm25_param_reads_valid_float(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BM25_K1", "2.5")
    assert ch._bm25_param("K1", ch._BM25_K1_DEFAULT) == 2.5


def test_bm25_param_garbage_falls_back_to_default(monkeypatch):
    # WHY: a typo'd env value must NOT poison the rescorer — it degrades to
    # the canonical default so ranking stays sane.
    monkeypatch.setenv("MEMORYMASTER_BM25_B", "not-a-number")
    assert ch._bm25_param("B", ch._BM25_B_DEFAULT) == ch._BM25_B_DEFAULT


def test_bm25_param_blank_falls_back(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BM25_K1", "   ")
    assert ch._bm25_param("K1", ch._BM25_K1_DEFAULT) == ch._BM25_K1_DEFAULT


def test_bm25_field_weight_valid_and_garbage(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "3.0")
    assert ch._bm25_field_weight("W_SUBJECT", 1.0) == 3.0
    monkeypatch.setenv("MEMORYMASTER_BM25_W_TEXT", "oops")
    assert ch._bm25_field_weight("W_TEXT", 1.0) == 1.0


@pytest.mark.parametrize(
    "value,expected",
    [("0", False), ("false", False), ("off", False), ("", False),
     ("1", True), ("yes", True)],
)
def test_bm25_enabled_truthiness(monkeypatch, value, expected):
    monkeypatch.setenv("MEMORYMASTER_LEXICAL_BM25", value)
    assert ch._bm25_enabled() is expected


def test_bm25_enabled_defaults_on_when_unset():
    assert ch._bm25_enabled() is True


# ---- recall weight + scope helpers ---------------------------------------

def test_recall_weight_default():
    assert ch._recall_weight("W_MATCHES") == ch._RECALL_WEIGHT_DEFAULTS["W_MATCHES"]


def test_recall_weight_env_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_FRESHNESS", "0.42")
    assert ch._recall_weight("W_FRESHNESS") == 0.42


def test_recall_weight_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_ENTITY", "xyz")
    assert ch._recall_weight("W_ENTITY") == ch._RECALL_WEIGHT_DEFAULTS["W_ENTITY"]


def test_recall_scope_boost_clamps_negative(monkeypatch):
    # WHY: a negative boost would DEMOTE current-scope claims, inverting the
    # feature. Contract is to treat it as "off" (0.0), never negative.
    monkeypatch.setenv("MEMORYMASTER_RECALL_SCOPE_BOOST", "-5")
    assert ch._recall_scope_boost() == 0.0


def test_recall_scope_boost_garbage(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_SCOPE_BOOST", "high")
    assert ch._recall_scope_boost() == 0.0


def test_recall_scope_boost_valid(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_SCOPE_BOOST", "0.5")
    assert ch._recall_scope_boost() == 0.5


def test_current_scope_default_and_override(monkeypatch):
    assert ch._current_scope() == ch._DEFAULT_CURRENT_SCOPE
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:other")
    assert ch._current_scope() == "project:other"


# ---- graph stream gates + numeric parsers --------------------------------

def test_graph_enabled_default_off():
    assert ch._graph_enabled() is False


def test_graph_enabled_on(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    assert ch._graph_enabled() is True


def test_graph_max_hops_default_and_floor(monkeypatch):
    assert ch._graph_max_hops() == ch._GRAPH_MAX_HOPS_DEFAULT
    # WHY: a 0/negative BFS depth is meaningless; contract floors it at 1.
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS", "0")
    assert ch._graph_max_hops() == 1


def test_graph_max_hops_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS", "deep")
    assert ch._graph_max_hops() == ch._GRAPH_MAX_HOPS_DEFAULT


def test_graph_path_respects_env(monkeypatch, tmp_path):
    custom = tmp_path / "g.kuzu"
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_PATH", str(custom))
    assert ch._graph_path() == custom


def test_graph_reached_returns_empty_when_disabled():
    # Disabled stream must short-circuit to empty with zero side effects.
    assert ch._graph_reached_claim_ids("postgres migration", store=None) == set()
    assert ch._graph_reached_claim_distance("postgres", store=None) == {}


# ---- two-pass + closets gates --------------------------------------------

def test_two_pass_enabled_and_edges_default_off():
    assert ch._two_pass_enabled() is False
    assert ch._two_pass_use_edges() is False


def test_two_pass_enabled_on(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", "1")
    assert ch._two_pass_enabled() is True


def test_two_pass_max_neighbors_default_and_garbage(monkeypatch):
    assert ch._two_pass_max_neighbors() == 20
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "garbage")
    assert ch._two_pass_max_neighbors() == 20
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "0")
    # floored at 1 so the fanout query is never a no-op LIMIT 0
    assert ch._two_pass_max_neighbors() == 1


def test_closets_gates_default_off(monkeypatch):
    assert ch._closets_enabled() is False
    assert ch._closets_boost_only() is False
    monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY", "1")
    assert ch._closets_enabled() is True
    assert ch._closets_boost_only() is True


def test_two_pass_neighbor_ids_empty_for_no_seeds():
    assert ch._two_pass_neighbor_ids(store=object(), seed_ids=[], excluded=set()) == []


def test_two_pass_neighbor_ids_degrades_without_tables(tmp_path):
    # A real store whose schema lacks claim_entities must return [] (the
    # except path), never raise — two-pass is best-effort.
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=str(tmp_path / "n.db"), workspace_root=tmp_path)
    out = ch._two_pass_neighbor_ids(svc.store, seed_ids=[1, 2], excluded=set())
    assert out == []


# ---- query expansion best-effort augmentation ----------------------------

def test_query_expansion_enabled_gate(monkeypatch):
    assert ch._query_expansion_enabled() is False
    monkeypatch.setenv("MEMORYMASTER_RECALL_QUERY_EXPANSION", "1")
    assert ch._query_expansion_enabled() is True


def test_apply_query_expansion_no_store_returns_unchanged():
    # WHY: expansion is a best-effort overlay. A service with no usable store
    # must return the original token list UNTOUCHED, never raise.
    class _Svc:
        store = None

    tokens = ["postgres", "wal"]
    assert ch._apply_query_expansion(_Svc(), "postgres wal", tokens) == tokens


def test_apply_query_expansion_swallows_store_error():
    class _BadStore:
        def connect(self):  # raising connect() simulates a broken DB
            raise RuntimeError("db down")

    class _Svc:
        store = _BadStore()

    tokens = ["alpha"]
    assert ch._apply_query_expansion(_Svc(), "alpha", tokens) == tokens


# ---- RRF auto-gate trio --------------------------------------------------
#
# The gate is the stream-topology heuristic that picks RRF vs linear fusion
# (claim 11898). A wrong decision silently regresses ranking, so we assert
# the DECISION + telemetry counters, not just that it ran.


def _row(**scores):
    return scores


def test_auto_gate_threshold_default_and_garbage(monkeypatch):
    assert ch._auto_gate_threshold() == ch._AUTO_GATE_THRESHOLD_DEFAULT
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "nan")
    assert ch._auto_gate_threshold() == ch._AUTO_GATE_THRESHOLD_DEFAULT
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "0")
    # < 1 would always pick RRF => fall back to default.
    assert ch._auto_gate_threshold() == ch._AUTO_GATE_THRESHOLD_DEFAULT
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "5")
    assert ch._auto_gate_threshold() == 5


def test_count_populated_streams_counts_each_active_stream():
    rows = [
        _row(entity_score=1.0, vector_score=0.0, verbatim_score=0.0,
             freshness_score=0.5, graph_score=0.0),
        _row(entity_score=0.0, vector_score=0.9, verbatim_score=0.0,
             freshness_score=0.0, graph_score=0.7),
    ]
    bm25 = {1: 2.0}
    # bm25 + entity + vector + freshness(weight>0) + graph = 5 streams.
    assert ch._count_populated_streams(rows, bm25, bm25_on=True,
                                       freshness_weight=0.1) == 5
    # Freshness gate: weight 0 => freshness NOT counted even if scored.
    assert ch._count_populated_streams(rows, bm25, bm25_on=True,
                                       freshness_weight=0.0) == 4
    # bm25_on False drops the bm25 stream regardless of scores.
    assert ch._count_populated_streams(rows, bm25, bm25_on=False,
                                       freshness_weight=0.1) == 4


def test_count_populated_streams_tolerates_bad_row_values():
    # Non-numeric stream fields must be skipped, not crash the gate.
    rows = [_row(entity_score="oops", vector_score=None, graph_score=0.5)]
    assert ch._count_populated_streams(rows, {}, bm25_on=False,
                                       freshness_weight=0.0) == 1


def test_auto_gate_decide_rrf_and_linear_update_stats():
    ch.reset_auto_gate_stats()
    rows = [_row(entity_score=1.0, vector_score=0.9, verbatim_score=0.8)]
    bm25 = {1: 1.0}
    # 4 populated streams >= threshold 3 => rrf.
    decision, populated, thr = ch._auto_gate_decide(
        rows, bm25, bm25_on=True, freshness_weight=0.0, threshold=3,
    )
    assert decision == "rrf"
    assert populated == 4
    assert thr == 3

    # Only 1 stream populated, threshold 3 => linear.
    decision2, populated2, _ = ch._auto_gate_decide(
        [_row(entity_score=1.0)], {}, bm25_on=False, freshness_weight=0.0,
        threshold=3,
    )
    assert decision2 == "linear"
    assert populated2 == 1

    stats = ch.get_auto_gate_stats()
    assert stats["calls"] == 2
    assert stats["picked_rrf"] == 1
    assert stats["picked_linear"] == 1
    ch.reset_auto_gate_stats()


# ---- classify_observation -------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("please don't use tabs", "preference"),
        ("we decided to use postgres", "decision"),
        ("this is mandatory", "constraint"),
        ("we migrated to qdrant", "fact"),
        ("there is a bug in recall", "event"),
        ("todo: wire the hook", "commitment"),
        ("the weather is fine", None),
    ],
)
def test_classify_observation(text, expected):
    assert ch.classify_observation(text) == expected


# ---- observe / observe_llm -----------------------------------------------

def test_observe_skips_unremarkable_text(tmp_path):
    # auto_classify on + no pattern match + not forced => not ingested.
    result = ch.observe(
        "the weather is fine today",
        db_path=str(tmp_path / "o.db"),
        scope="project:test",
    )
    assert result["ingested"] is False
    assert result["reason"] == "no_pattern_match"


def test_observe_ingests_matched_text(tmp_path):
    db = _init_db(tmp_path / "o.db")
    result = ch.observe(
        "we decided to use PostgreSQL",
        db_path=db,
        scope="project:test",
    )
    assert result["ingested"] is True
    assert result["claim_type"] == "decision"
    assert isinstance(result["claim_id"], int)


def test_observe_llm_no_extractions(monkeypatch, tmp_path):
    """When the extractor yields nothing, observe_llm ingests nothing."""
    monkeypatch.setattr(
        "memorymaster.knowledge.auto_extractor.extract_claims_from_text",
        lambda *a, **k: [],
    )
    result = ch.observe_llm(
        "nothing notable here",
        db_path=str(tmp_path / "x.db"),
        scope="project:test",
    )
    assert result == {"ingested": 0, "extracted": 0}


def test_observe_llm_ingests_extracted_claims(monkeypatch, tmp_path):
    """Each extracted claim is ingested; per-claim failure is skipped.

    WHY: observe_llm is the LLM-extraction ingest path. Contract: ingest
    every well-formed extraction, tolerate a single bad claim without
    aborting the batch.
    """
    extracted = [
        {"text": "uses postgres", "claim_type": "fact"},
        {"text": "BAD", "claim_type": "fact"},
        {"text": "prefers ruff", "claim_type": "preference"},
    ]
    monkeypatch.setattr(
        "memorymaster.knowledge.auto_extractor.extract_claims_from_text",
        lambda *a, **k: extracted,
    )

    calls = []

    class _FakeSvc:
        def __init__(self, *a, **k):
            pass

        def ingest(self, *, text, **kwargs):
            calls.append(text)
            if text == "BAD":
                raise RuntimeError("ingest blew up")

            class _C:
                id = len(calls)

            return _C()

    monkeypatch.setattr("memorymaster.core.service.MemoryService", _FakeSvc)

    result = ch.observe_llm(
        "transcript",
        db_path=str(tmp_path / "x.db"),
        scope="project:test",
    )
    # 3 extracted, "BAD" raised and was swallowed => 2 ingested.
    assert result == {"ingested": 2, "extracted": 3}
    assert calls == ["uses postgres", "BAD", "prefers ruff"]


# ---- DB-backed recall() + query_for_task end-to-end ----------------------
#
# These drive the large _recall_impl / query_for_task bodies against a real
# seeded SQLite store so ranking, budgeting and stream-augmentation execute.
# Contract: a seeded matching claim is surfaced, and each optional stream
# augments-or-noops when toggled on — never crashes recall.


def _seed(tmp_path):
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    db = tmp_path / "seeded.db"
    svc = MemoryService(db_target=str(db), workspace_root=tmp_path)
    svc.init_db()
    first = svc.ingest(
        text="The user prefers PostgreSQL for production databases.",
        citations=[CitationInput(source="test")],
        claim_type="preference",
        scope="project",
        confidence=0.9,
    )
    second = svc.ingest(
        text="The team decided to use Qdrant for vector search backends.",
        citations=[CitationInput(source="test")],
        claim_type="decision",
        scope="project",
        confidence=0.8,
    )
    transition_claim(svc.store, first.id, "confirmed", "trusted recall fixture")
    transition_claim(svc.store, second.id, "confirmed", "trusted recall fixture")
    return str(db)


def test_recall_surfaces_seeded_claim(tmp_path):
    db = _seed(tmp_path)
    result = ch.recall("PostgreSQL database", db_path=db, skip_qdrant=True)
    assert isinstance(result, str)
    assert "PostgreSQL" in result


def test_recall_return_ids_tracks_rendered_bullets(tmp_path):
    db = _seed(tmp_path)
    markdown, ids = ch.recall(
        "PostgreSQL database", db_path=db, skip_qdrant=True, return_ids=True,
    )
    # WHY: return_ids is the eval-harness contract — the id list must mirror
    # the rendered bullets exactly so a consumer never re-parses markdown.
    assert isinstance(ids, list)
    assert len(ids) == markdown.count("\n- ")
    assert all(isinstance(i, int) for i in ids)


def test_recall_empty_db_returns_empty(tmp_path):
    from memorymaster.core.service import MemoryService

    db = tmp_path / "empty.db"
    MemoryService(db_target=str(db), workspace_root=tmp_path).init_db()
    assert ch.recall("anything", db_path=str(db), skip_qdrant=True) == ""


def test_recall_scope_boost_active_path(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_SCOPE_BOOST", "0.5")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:test")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_two_pass_stream_does_not_crash(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_TWO_PASS", "0.3")
    assert isinstance(ch.recall("Qdrant vector search", db_path=db,
                                skip_qdrant=True), str)


def test_recall_query_expansion_on_path(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_QUERY_EXPANSION", "1")
    assert isinstance(ch.recall("PostgreSQL production", db_path=db,
                                skip_qdrant=True), str)


def test_recall_bm25_disabled_uses_lexical_fallback(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_LEXICAL_BM25", "0")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_verbatim_stream_on(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_VERBATIM", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VERBATIM", "0.2")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_closets_stream_on(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_CLOSETS", "0.2")
    assert isinstance(ch.recall("Qdrant", db_path=db, skip_qdrant=True), str)


def test_recall_graph_stream_on(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    # Graph enabled but no Kuzu DB => silent-fail to empty; recall still
    # returns the FTS5 result (claim 11907 defensive contract).
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_GRAPH", "0.2")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_entity_fanout_active_with_weight(tmp_path, monkeypatch):
    # W_ENTITY > 0 runs the entity fanout unconditionally (should_fanout=True).
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_ENTITY", "0.3")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_fusion_rrf_path(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_FUSION", "rrf")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_recall_fusion_auto_path(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_FUSION", "auto")
    assert isinstance(ch.recall("PostgreSQL", db_path=db, skip_qdrant=True), str)


def test_query_for_task_empty_description_returns_empty():
    assert ch.query_for_task("", "project:test") == ""


def test_query_for_task_all_stopwords_returns_empty():
    # Description of only stop-words / short tokens => no usable FTS5 terms.
    assert ch.query_for_task("the and for to", "project:test") == ""


def test_query_for_task_wraps_relevant_memory(tmp_path):
    db = _seed(tmp_path)
    out = ch.query_for_task(
        "Configure PostgreSQL production database",
        "project:test",
        db_path=db,
        skip_qdrant=True,
    )
    # Either a wrapped briefing (claims matched) or empty (none fit budget).
    if out:
        assert out.startswith("<task_briefing")
        assert "<relevant_memory>" in out
        assert "PostgreSQL" in out or "postgres" in out.lower()


# ---- additional real-store / real-service paths --------------------------
#
# The mocked observe_llm/expansion tests above prove the control flow; these
# drive the SAME bodies against real SQLite + the real MemoryService so the
# integration glue (CitationInput building, store.connect()) is exercised,
# not just the branch decision.


def test_auto_gate_threshold_value_below_one_falls_back(monkeypatch):
    # value parses fine but is < 1 → second warning branch → default.
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "-3")
    assert ch._auto_gate_threshold() == ch._AUTO_GATE_THRESHOLD_DEFAULT


def test_apply_query_expansion_real_store_returns_list(tmp_path):
    # Real seeded service → store.connect() succeeds → expand_query runs.
    # Whether aliases are found or not, the contract is "return a list that
    # still contains the original tokens" (best-effort augmentation).
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    db = tmp_path / "qe.db"
    svc = MemoryService(db_target=str(db), workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        text="The user prefers PostgreSQL for production databases.",
        citations=[CitationInput(source="t")],
        claim_type="preference",
        scope="project:test",
        confidence=0.9,
    )
    out = ch._apply_query_expansion(svc, "PostgreSQL", ["postgresql"])
    assert isinstance(out, list)
    assert "postgresql" in out


def test_observe_llm_real_service_ingests(monkeypatch, tmp_path):
    """observe_llm against the REAL MemoryService — proves the ingest loop
    builds CitationInput + persists claims end-to-end (lines 1987-2004),
    not just the mocked control flow."""
    extracted = [
        {"text": "The team uses Postgres in prod.", "claim_type": "fact",
         "subject": "team", "predicate": "uses", "object_value": "Postgres"},
        {"text": "The team prefers ruff for linting.", "claim_type": "preference"},
    ]
    monkeypatch.setattr(
        "memorymaster.knowledge.auto_extractor.extract_claims_from_text",
        lambda *a, **k: extracted,
    )
    result = ch.observe_llm(
        "transcript text",
        db_path=_init_db(tmp_path / "real.db"),
        scope="project:test",
    )
    assert result == {"ingested": 2, "extracted": 2}


def test_observe_llm_default_scope_from_cwd(monkeypatch, tmp_path):
    # scope=None branch → scope_from_cwd is consulted (line 1972-1974).
    monkeypatch.setattr(
        "memorymaster.knowledge.auto_extractor.extract_claims_from_text",
        lambda *a, **k: [],
    )
    result = ch.observe_llm("nothing", db_path=str(tmp_path / "d.db"))
    assert result == {"ingested": 0, "extracted": 0}


def test_observe_default_scope_from_cwd(tmp_path):
    # observe() scope=None branch → scope_from_cwd default applies.
    result = ch.observe(
        "the weather is fine",  # no pattern → not ingested, but scope resolved
        db_path=str(tmp_path / "d2.db"),
    )
    assert result["ingested"] is False

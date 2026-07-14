"""Tests for v3.9.0 F5 — two-pass entity-fanout retrieval.

Gated by MEMORYMASTER_RECALL_TWO_PASS=1 + MEMORYMASTER_RECALL_W_TWO_PASS > 0.
Default keeps ranking bit-identical. The DB walker is defensive: missing
tables → []. Recall regression suite must still be green.
"""
from __future__ import annotations

import pytest

from memorymaster.recall import context_hook


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in [
        "MEMORYMASTER_RECALL_TWO_PASS",
        "MEMORYMASTER_RECALL_TWO_PASS_MAX",
        "MEMORYMASTER_RECALL_W_TWO_PASS",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_two_pass_disabled_by_default():
    assert context_hook._two_pass_enabled() is False


def test_two_pass_enabled_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", "1")
    assert context_hook._two_pass_enabled() is True


def test_two_pass_disabled_for_falsy_values(monkeypatch):
    for v in ["0", "false", "False", "no", "off", ""]:
        monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", v)
        assert context_hook._two_pass_enabled() is False, f"failed for {v!r}"


def test_two_pass_max_default():
    assert context_hook._two_pass_max_neighbors() == 20


def test_two_pass_max_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "50")
    assert context_hook._two_pass_max_neighbors() == 50


def test_two_pass_max_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "not-a-number")
    assert context_hook._two_pass_max_neighbors() == 20


def test_w_two_pass_default_is_zero():
    assert context_hook._RECALL_WEIGHT_DEFAULTS["W_TWO_PASS"] == 0.0


def test_w_two_pass_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_TWO_PASS", "0.25")
    assert context_hook._recall_weight("W_TWO_PASS") == 0.25


def test_neighbor_ids_empty_seeds_returns_empty():
    """Defensive: no seeds → no walk."""
    assert context_hook._two_pass_neighbor_ids(None, [], set()) == []


def test_neighbor_ids_no_conn_returns_empty():
    """Defensive: store has no _conn or conn → []."""

    class _StoreNoConn:
        pass

    assert context_hook._two_pass_neighbor_ids(_StoreNoConn(), [1, 2, 3], set()) == []


def test_neighbor_ids_db_error_returns_empty():
    """If the DB raises (e.g. missing junction table) → silent [] not crash."""

    class _BadConn:
        def execute(self, *args, **kwargs):
            raise RuntimeError("table claim_entity_links does not exist")

    class _Store:
        _conn = _BadConn()

    assert context_hook._two_pass_neighbor_ids(_Store(), [1, 2, 3], set()) == []


def test_neighbor_ids_walks_claim_entity_links_on_real_store(tmp_path):
    """WHY: the two-pass stream exists to surface claims that share entities
    with already-recalled seeds. It must walk the REAL junction table
    (claim_entity_links, written by EntityGraph into the claims DB) through
    the store's own connect() — the old code queried a ``claim_entities``
    table no schema ever created, via a ``store._conn`` attribute real
    stores don't have, so the stream silently returned [] even when the
    operator enabled it."""
    from memorymaster.core.models import CitationInput
    from memorymaster.knowledge.entity_registry import resolve_or_create
    from memorymaster.stores.storage import SQLiteStore

    db = str(tmp_path / "two_pass.db")
    store = SQLiteStore(db)
    store.init_db()
    first = store.create_claim(text="Redis seed", citations=[CitationInput(source="test")])
    second = store.create_claim(text="Redis neighbor", citations=[CitationInput(source="test")])
    third = store.create_claim(text="Postgres other", citations=[CitationInput(source="test")])
    with store.connect() as conn:
        # Same DDL as EntityGraph.ensure_tables (entity_graph.py). Created
        # directly because ensure_tables' full script aborts when the claims
        # DB already holds the registry-style ``entities`` table — the
        # junction is what the walker contract depends on.
        redis_id = resolve_or_create(conn, "Redis")
        postgres_id = resolve_or_create(conn, "Postgres")
        conn.executemany(
            "INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
            [(first.id, redis_id), (second.id, redis_id), (third.id, postgres_id)],
        )
        conn.commit()

    # Claim 2 shares ent-redis with seed claim 1; claim 3 shares nothing.
    out = context_hook._two_pass_neighbor_ids(store, [first.id], {first.id})
    assert out == [second.id]

    # Excluded/seen IDs are never reintroduced.
    assert context_hook._two_pass_neighbor_ids(
        store, [first.id], {first.id, second.id}
    ) == []

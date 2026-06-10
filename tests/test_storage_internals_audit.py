"""Regression tests for storage-internals audit fixes (low-severity cluster).

Each test anchors on WHY the behavior matters, not just the mechanics:
the underlying bugs silently dropped data (archived claims invisible),
or burned O(n) work on every init / re-ingest / graph walk.
"""
from __future__ import annotations

from pathlib import Path

from memorymaster.lifecycle import transition_claim
from memorymaster.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "storage-internals-audit.db")
    store.init_db()
    return store


def _cite(label: str = "evidence") -> CitationInput:
    return CitationInput(source="test://storage-internals", locator=label, excerpt=f"{label} excerpt")


def _archive(store: SQLiteStore, claim_id: int) -> None:
    """Drive a claim to archived via the lifecycle helpers (never raw SQL)."""
    transition_claim(store, claim_id, "archived", "archived for audit test", event_type="compactor")


def test_list_claims_status_in_archived_returns_archived(tmp_path: Path) -> None:
    """Asking explicitly for archived claims must NOT be silently filtered out.

    Operators debugging a 'missing claim' rely on list_claims(status_in=['archived'])
    to find retired rows. The implicit archived-exclusion previously clobbered the
    explicit request and returned zero, hiding the very rows the caller asked for.
    """
    store = _store(tmp_path)
    claim = store.create_claim("claim destined for archive", [_cite("arch")])
    _archive(store, claim.id)

    archived = store.list_claims(status_in=["archived"])
    assert [c.id for c in archived] == [claim.id]

    # And the default path still hides archived rows (no regression).
    assert claim.id not in [c.id for c in store.list_claims()]


def test_check_idempotency_returns_existing_with_citations(tmp_path: Path) -> None:
    """Re-ingesting a duplicate idempotency_key must return the SAME claim, citations intact.

    The fix hydrates the existing row from the already-open connection (one SELECT)
    instead of paying extra connection opens via get_claim(); correctness of the
    returned claim — id and citations — is what callers depend on.
    """
    store = _store(tmp_path)
    first = store.create_claim("idempotent claim", [_cite("idem")], idempotency_key="dup-key-1")
    second = store.create_claim("idempotent claim again", [_cite("idem2")], idempotency_key="dup-key-1")

    assert second.id == first.id
    assert second.citations  # citations were hydrated, not dropped
    assert second.citations[0].source == "test://storage-internals"


def test_traverse_relationships_batched_walk_is_correct(tmp_path: Path) -> None:
    """Graph traversal must return all reachable claims at the right depth.

    The N+1 fix batches each BFS level into one neighbor query + one hydrate SELECT.
    This test pins the *behavior* (depths, paths, membership) so the batching can
    never silently drop or mis-depth a node.
    """
    store = _store(tmp_path)
    a = store.create_claim("root claim A", [_cite("a")])
    b = store.create_claim("child claim B", [_cite("b")])
    c = store.create_claim("grandchild claim C", [_cite("c")])
    store.add_claim_link(a.id, b.id, "depends_on")
    store.add_claim_link(b.id, c.id, "depends_on")

    result = store.traverse_relationships(a.id, direction="outgoing", max_depth=3)
    by_id = {r["claim"].id: r for r in result}

    assert set(by_id) == {b.id, c.id}
    assert by_id[b.id]["depth"] == 1
    assert by_id[c.id]["depth"] == 2
    assert by_id[c.id]["path"] == [a.id, b.id, c.id]

    # Depth bound is honored.
    shallow = store.traverse_relationships(a.id, direction="outgoing", max_depth=1)
    assert {r["claim"].id for r in shallow} == {b.id}


def test_fts_rebuild_skipped_when_index_already_consistent(tmp_path: Path) -> None:
    """init_db() on a warm DB must not pay an O(n) FTS rebuild every time.

    The triggers keep claims_fts current, so a re-init with matching row counts
    should skip 'rebuild'. We assert search still works after re-init (the index
    was preserved, not wiped) — the perf win must not cost correctness.
    """
    store = _store(tmp_path)
    claim = store.create_claim("searchable fts token zeta", [_cite("fts")])

    # Re-init: should be a no-op rebuild, but FTS must keep working.
    store.init_db()
    hits = store.list_claims(text_query="zeta")
    assert claim.id in [h.id for h in hits]


def test_backfill_human_ids_assigns_unique_ids(tmp_path: Path) -> None:
    """Every claim must get a unique human_id; re-init must not re-do the work.

    The batched top-level path (no derived_from links) must still produce unique
    ids — human_id collisions break get_claim_by_human_id and the wiki.
    """
    store = _store(tmp_path)
    claims = [store.create_claim(f"human id claim {i}", [_cite(f"h{i}")]) for i in range(5)]
    human_ids = [store.get_claim(c.id).human_id for c in claims]

    assert all(hid for hid in human_ids)
    assert len(set(human_ids)) == len(human_ids)

    # Re-init must be idempotent — ids stay stable.
    store.init_db()
    again = [store.get_claim(c.id).human_id for c in claims]
    assert again == human_ids

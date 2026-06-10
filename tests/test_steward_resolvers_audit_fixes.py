"""Regression tests for the steward-resolvers audit-fix cluster.

Each test anchors on the INTENT (the memory-corruption risk being prevented),
not the implementation, so they keep their value if the internals change.

Covered findings:
  F1/F2 — llm_steward holds a write txn across sleep without WAL/busy_timeout
           and mutates status via raw SQL with no version guard (lost-update).
  F3    — conflict_resolver can point a loser's replaced_by at a non-winner.
  F4    — auto_resolver only compared ADJACENT claims, leaving non-adjacent
           members 'conflicted' in a group it reported as resolved.
  F5    — candidate_dedupe could archive a fresh candidate as a duplicate of a
           retired/contested (superseded/conflicted/archived) claim.
"""
from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from memorymaster import auto_resolver, candidate_dedupe, conflict_resolver, llm_steward
from memorymaster.models import Claim


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _claim(cid: int, conf: float, *, pinned: bool = False, val: str = "v",
           status: str = "confirmed") -> Claim:
    return Claim(
        id=cid, text=f"claim {cid}", idempotency_key=None, normalized_text=None,
        claim_type=None, subject="S", predicate="P", object_value=val,
        scope="project:x", volatility="low", status=status, confidence=conf,
        pinned=pinned, supersedes_claim_id=None, replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_validated_at=None, archived_at=None,
    )


# --------------------------------------------------------------------------- #
# F1/F2 — llm_steward version-CAS prevents lost-update races
# --------------------------------------------------------------------------- #
def _cas_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, status TEXT, version INTEGER, "
        "replaced_by_claim_id INTEGER, subject TEXT, predicate TEXT, "
        "object_value TEXT, confidence REAL, updated_at TEXT)"
    )
    return conn


def test_archive_cas_refuses_when_another_writer_bumped_version():
    """WHY: the steward reads candidates, then writes much later (after an LLM
    round-trip). If a concurrent version-checked writer changed the row, a blind
    UPDATE would silently clobber that newer state. The CAS must refuse."""
    conn = _cas_conn()
    conn.execute("INSERT INTO claims (id, status, version) VALUES (1, 'candidate', 5)")

    applied = llm_steward._archive_candidate_cas(conn, claim_id=1, version=4)

    assert applied is False
    assert conn.execute("SELECT status FROM claims WHERE id=1").fetchone()[0] == "candidate"


def test_archive_cas_applies_and_bumps_version_on_match():
    """WHY: when nothing raced us, the transition must apply AND advance the
    version so the next CAS writer sees the change (optimistic locking)."""
    conn = _cas_conn()
    conn.execute("INSERT INTO claims (id, status, version) VALUES (1, 'candidate', 5)")

    applied = llm_steward._archive_candidate_cas(conn, claim_id=1, version=5)

    row = conn.execute("SELECT status, version FROM claims WHERE id=1").fetchone()
    assert applied is True
    assert row[0] == "archived"
    assert row[1] == 6


def test_archive_cas_refuses_non_candidate_even_at_right_version():
    """WHY: a row that already left 'candidate' (e.g. got superseded) must never
    be re-archived by the steward — that would corrupt a finished lifecycle."""
    conn = _cas_conn()
    conn.execute("INSERT INTO claims (id, status, version) VALUES (2, 'superseded', 1)")

    assert llm_steward._confirm_candidate_cas(
        conn, claim_id=2, version=1, subject="S", predicate="P",
        object_value="x", confidence=0.9,
    ) is False
    assert conn.execute("SELECT status FROM claims WHERE id=2").fetchone()[0] == "superseded"


def test_run_steward_opens_wal_and_busy_timeout(monkeypatch, tmp_path):
    """WHY: the steward holds write transactions across time.sleep(delay). Without
    WAL + a busy_timeout, this writer blocks every other writer and a momentary
    lock becomes an immediate 'database is locked' (busy_timeout=0) → lost writes.
    Since P1 step 3 that envelope is supplied by the canonical open_conn helper
    (WAL + busy_timeout=15000, pinned by tests/test_open_conn.py) — the steward
    must route through it, never a raw sqlite3.connect with ad-hoc pragmas."""
    import inspect

    from memorymaster._storage_shared import open_conn as canonical_open_conn
    src = inspect.getsource(llm_steward.run_steward)
    assert "open_conn(" in src
    assert "sqlite3.connect(" not in src
    assert llm_steward.open_conn is canonical_open_conn
    # And the commit must precede the sleep so the lock is released first.
    assert src.index("conn.commit()") < src.rindex("time.sleep(delay)")


# --------------------------------------------------------------------------- #
# F3 — conflict_resolver picks ONE group winner; no dead replaced_by chains
# --------------------------------------------------------------------------- #
class _ListStore:
    def __init__(self, claims):
        self._claims = claims

    def list_claims(self, **_kw):
        return self._claims


def test_detect_conflicts_single_winner_for_whole_group():
    """WHY: every loser's replaced_by must point at the SURVIVING winner. The old
    pairwise loop reassigned the running winner, so when a pinned/priority claim
    flipped the intermediate winner, earlier losers ended up superseded-by a
    claim that was itself later superseded — a dangling replaced_by chain."""
    # Highest confidence is id=1, but id=3 is PINNED → pinned must win outright.
    group = [
        _claim(1, 0.95, val="a"),
        _claim(2, 0.50, val="b"),
        _claim(3, 0.10, pinned=True, val="c"),
    ]
    pairs = conflict_resolver.detect_conflicts(_ListStore(group))

    winners = {p.winner.id for p in pairs}
    losers = sorted(p.loser.id for p in pairs)
    assert winners == {3}, "all pairs must share the single pinned winner"
    assert losers == [1, 2], "every non-winner is a loser of that one winner"


# --------------------------------------------------------------------------- #
# F4 — auto_resolver resolves a 3+ group down to one survivor
# --------------------------------------------------------------------------- #
class _SupersedeStore:
    """Resolve keeps the higher-id claim and supersedes the lower."""

    def __init__(self, claims):
        self._by_id = {c.id: c for c in claims}

    def get_claim(self, cid, include_citations=False):
        return self._by_id.get(cid)

    def supersede(self, loser_id, winner_id):
        self._by_id[loser_id] = dataclasses.replace(
            self._by_id[loser_id], status="superseded",
            replaced_by_claim_id=winner_id,
        )


def test_resolve_group_pairs_supersedes_non_adjacent_members(monkeypatch):
    """WHY: in a 3+ conflict group the old loop only compared adjacent claims, so
    a claim that lost to its neighbour was never judged against the eventual
    survivor and stayed 'conflicted' while the group was reported resolved."""
    def fake_resolve(store, a, b):
        winner, loser = (a, b) if a.id >= b.id else (b, a)
        store.supersede(loser.id, winner.id)
        return {"resolved": True, "winner_id": winner.id,
                "loser_id": loser.id, "reason": "test"}

    monkeypatch.setattr(auto_resolver, "resolve_conflict_pair", fake_resolve)

    group = [_claim(i, 0.5, val=chr(96 + i), status="conflicted") for i in (1, 2, 3)]
    store = _SupersedeStore(group)

    auto_resolver._resolve_group_pairs(store, group, limit=50)

    still_conflicted = [c for c in store._by_id.values() if c.status == "conflicted"]
    assert len(still_conflicted) <= 1, "group must collapse to one survivor"
    # The non-adjacent member (id=1) must have been superseded, not stranded as
    # 'conflicted' — the old adjacent-only loop left it conflicted forever.
    assert store._by_id[1].status == "superseded"
    assert store._by_id[1].replaced_by_claim_id is not None
    # The single survivor is the highest-id claim (per our fake resolver rule).
    assert store._by_id[3].status == "conflicted"


# --------------------------------------------------------------------------- #
# F5 — candidate_dedupe never dedupes against retired/contested claims
# --------------------------------------------------------------------------- #
@pytest.fixture()
def dedupe_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, scope TEXT, status TEXT);
        CREATE VIRTUAL TABLE claims_fts USING fts5(text, content='claims', content_rowid='id');
        CREATE TRIGGER ai AFTER INSERT ON claims BEGIN
          INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text); END;
        """
    )
    return conn


@pytest.mark.parametrize("retired_status", ["superseded", "conflicted", "archived"])
def test_dedupe_ignores_retired_canonical_candidates(dedupe_conn, retired_status):
    """WHY: archiving a fresh candidate as a duplicate of a retired/contested
    claim drops possibly-newer live information in favour of a dead row."""
    text = "the deploy script lives at scripts slash deploy in the repo"
    dedupe_conn.execute(
        "INSERT INTO claims (id, text, scope, status) VALUES (10, ?, 'project:x', ?)",
        (text, retired_status),
    )

    matches = candidate_dedupe.fts_candidates_in_scope(
        dedupe_conn, scope="project:x", text=text, exclude_id=99,
    )
    assert 10 not in {m[0] for m in matches}

    decision = candidate_dedupe.find_near_duplicate(
        dedupe_conn, candidate_id=99, candidate_text=text, candidate_scope="project:x",
    )
    assert decision.action == "passthrough"
    assert decision.canonical_claim_id is None


def test_dedupe_still_matches_confirmed_canonical(dedupe_conn):
    """WHY: the fix must NOT over-restrict — a genuine confirmed duplicate should
    still be caught so real dedupe keeps working."""
    text = "the deploy script lives at scripts slash deploy in the repo"
    dedupe_conn.execute(
        "INSERT INTO claims (id, text, scope, status) VALUES (11, ?, 'project:x', 'confirmed')",
        (text,),
    )

    decision = candidate_dedupe.find_near_duplicate(
        dedupe_conn, candidate_id=99, candidate_text=text, candidate_scope="project:x",
    )
    assert decision.action == "archive"
    assert decision.canonical_claim_id == 11

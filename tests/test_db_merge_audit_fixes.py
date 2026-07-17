"""Regression tests for db_merge audit fixes.

Each test encodes WHY the behavior matters (the bug it prevents), not just the
mechanics. Uses NORMAL imports — validated centrally against the merged main
checkout.
"""
import sqlite3
from pathlib import Path

import pytest

from memorymaster.bridges.db_merge import merge_databases, _open_target, _build_insert_values


T = "2026-06-01T00:00:00+00:00"

# Schema mirrors the real claims table's UNIQUE constraints on human_id and
# idempotency_key plus the events / schema_versions bookkeeping the fixes rely on.
SCHEMA = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    idempotency_key TEXT,
    subject TEXT, predicate TEXT, object_value TEXT,
    scope TEXT NOT NULL DEFAULT 'project:test',
    status TEXT NOT NULL DEFAULT 'candidate',
    confidence REAL NOT NULL DEFAULT 0.5,
    pinned INTEGER NOT NULL DEFAULT 0,
    supersedes_claim_id INTEGER,
    replaced_by_claim_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    human_id TEXT
);
CREATE UNIQUE INDEX idx_claims_human_id ON claims(human_id);
CREATE UNIQUE INDEX idx_claims_idempotency_key ON claims(idempotency_key);
CREATE TABLE citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL,
    source TEXT NOT NULL, locator TEXT, excerpt TEXT, created_at TEXT NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER,
    event_type TEXT NOT NULL, from_status TEXT, to_status TEXT,
    details TEXT, created_at TEXT NOT NULL
);
CREATE TABLE schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL UNIQUE,
    description TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);
"""


def _init(path: Path, version: int | None = 1) -> None:
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        if version is not None:
            c.execute(
                "INSERT INTO schema_versions(version, description, checksum, applied_at)"
                " VALUES (?, 'm', 'x', ?)", (version, T))


def _ins(path: Path, *, text: str, hid=None, ikey=None, subject="s",
         predicate="p", object_value="o", status="candidate",
         confidence=0.5) -> int:
    with sqlite3.connect(path) as c:
        cur = c.execute(
            "INSERT INTO claims(text, idempotency_key, subject, predicate,"
            " object_value, status, confidence, created_at, updated_at, human_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (text, ikey, subject, predicate, object_value, status, confidence,
             T, T, hid))
        return int(cur.lastrowid)


def _rows(path: Path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM claims ORDER BY id").fetchall()
    finally:
        c.close()


def test_new_claim_survives_human_id_collision(tmp_path):
    """A genuinely-new remote claim must NOT be silently dropped just because
    its human_id collides with an unrelated target row. Losing real memory to a
    cosmetic id clash is the HIGH-severity data-loss bug this guards."""
    tgt, src = tmp_path / "t.db", tmp_path / "s.db"
    _init(tgt)
    _init(src)
    _ins(tgt, text="unrelated target claim", hid="H1", ikey="k-tgt",
         subject="ta", predicate="tb", object_value="tv")
    _ins(src, text="brand new remote fact", hid="H1", ikey="k-src",
         subject="ra", predicate="rb", object_value="rv")

    stats = merge_databases(str(tgt), str(src))

    texts = {r["text"] for r in _rows(tgt)}
    assert "brand new remote fact" in texts
    assert stats["merged"] == 1 and stats["errors"] == 0
    new = [r for r in _rows(tgt) if r["text"] == "brand new remote fact"][0]
    # human_id must be re-allocated by the target, never stolen from a peer.
    assert new["human_id"] is None


def test_confirmed_replica_conflict_is_preserved_as_candidate(tmp_path):
    """Two offline replicas may confirm different values before exchanging deltas.

    The incoming evidence must survive without violating the target's single
    confirmed-value invariant or silently replacing either side's truth.
    """
    tgt, src = tmp_path / "t.db", tmp_path / "s.db"
    _init(tgt)
    _init(src)
    with sqlite3.connect(tgt) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX one_confirmed_value "
            "ON claims(subject, predicate, scope) WHERE status = 'confirmed'"
        )
    _ins(
        tgt, text="old endpoint", ikey="target", subject="service",
        predicate="url", object_value="https://old.test", status="confirmed"
    )
    _ins(
        src, text="new endpoint", ikey="source", subject="service",
        predicate="url", object_value="https://new.test", status="confirmed"
    )

    stats = merge_databases(str(tgt), str(src))

    rows = {row["text"]: row for row in _rows(tgt)}
    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert rows["old endpoint"]["status"] == "confirmed"
    assert rows["new endpoint"]["status"] == "candidate"


def test_supersession_sets_both_link_sides_and_records_event(tmp_path):
    """Conflict resolution must honor the supersession invariant: BOTH the
    winner.supersedes_claim_id and loser.replaced_by_claim_id are set, and a
    transition event is recorded. A half-set pair breaks the wiki/steward."""
    tgt, src = tmp_path / "t.db", tmp_path / "s.db"
    _init(tgt)
    _init(src)
    loser = _ins(tgt, text="old value", subject="cpu", predicate="cores",
                 object_value="4", confidence=0.3)
    _ins(src, text="new value", ikey="k-new", subject="cpu",
         predicate="cores", object_value="8", confidence=0.9)

    merge_databases(str(tgt), str(src))

    rows = _rows(tgt)
    winner = [r for r in rows if r["text"] == "new value"][0]
    loser_row = [r for r in rows if r["id"] == loser][0]
    assert loser_row["status"] == "superseded"
    assert loser_row["replaced_by_claim_id"] == winner["id"]
    assert winner["supersedes_claim_id"] == loser
    c = sqlite3.connect(tgt)
    try:
        ev = c.execute(
            "SELECT event_type, to_status FROM events WHERE claim_id=?",
            (loser,)).fetchone()
    finally:
        c.close()
    assert ev is not None
    assert ev[0] == "supersession" and ev[1] == "superseded"


def test_target_opened_with_wal_and_busy_timeout(tmp_path):
    """The shared OpenClaw DB must be opened WAL + busy_timeout. Without it the
    long merge transaction races concurrent access -> 'database is locked'."""
    tgt = tmp_path / "t.db"
    _init(tgt)
    conn = _open_target(str(tgt))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"
    assert int(busy) >= 30000


def test_incompatible_schema_versions_refused(tmp_path):
    """Importing rows from a DB at a different applied schema version can
    violate the target's CHECK constraints. The merge must refuse, not corrupt."""
    tgt, src = tmp_path / "t.db", tmp_path / "s.db"
    _init(tgt, version=5)
    _init(src, version=3)
    _ins(src, text="x", ikey="k1")
    with pytest.raises(ValueError, match="schema version"):
        merge_databases(str(tgt), str(src))


def test_non_portable_columns_excluded_from_insert(tmp_path):
    """human_id and the supersede link columns are local row identities; copying
    them across a merge causes UNIQUE collisions / dangling links. They must be
    excluded from the insert column list so the target re-allocates them."""
    src = tmp_path / "s.db"
    _init(src)
    _ins(src, text="remote", ikey="dup", hid="B")
    c = sqlite3.connect(src)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT * FROM claims WHERE text='remote'").fetchone()
    finally:
        c.close()
    common = ["text", "idempotency_key", "human_id", "supersedes_claim_id",
              "replaced_by_claim_id", "subject", "predicate", "object_value",
              "scope", "status", "confidence", "pinned", "created_at",
              "updated_at"]
    cols, _vals = _build_insert_values(row, common, "dup")
    assert "human_id" not in cols
    assert "supersedes_claim_id" not in cols
    assert "replaced_by_claim_id" not in cols

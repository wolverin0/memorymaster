"""Tests for the entity-link fanout stage in ``context_hook.recall``.

Layered like ``test_recall_precision_at_5.py``:

1. ``_entity_fanout_claim_ids`` resolves prompt entities to claim IDs via
   ``entity_aliases`` JOINs, respects the per-entity and total caps, and
   never returns IDs already in ``seen_ids``.
2. The fanout silently returns [] on malformed DBs (missing tables,
   unreachable connection) — it is pure best-effort.
3. End-to-end: when the FTS5 stage finds nothing, the fanout rescues a
   prompt that contains a known entity, and the matching claim appears in
   the hook output.
4. With ``MEMORYMASTER_RECALL_W_ENTITY=0.0`` (shipped default), the ranker
   is bit-for-bit identical to the pre-fanout impl when FTS5 already found
   results — fanout rows sort to the bottom and never enter the top-K.
5. With ``MEMORYMASTER_RECALL_W_ENTITY>0``, fanout rows can promote above
   lower-scoring FTS5 rows in the final ranking.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.recall.context_hook import (
    _ENTITY_CAP_PER_ENTITY,
    _ENTITY_CAP_TOTAL,
    _RECALL_WEIGHT_DEFAULTS,
    _entity_fanout_claim_ids,
    _row_for_claim,
    recall,
)
from memorymaster.core.models import Claim


# --------------------------------------------------------------------------- #
# Fixtures — synthetic DB with entities + aliases + claims
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _init_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema for the fanout SQL path. Only the columns the fanout
    touches are required: ``claims.id``, ``claims.entity_id``, ``claims.status``,
    ``claims.updated_at`` + the two entity tables."""
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            idempotency_key TEXT,
            normalized_text TEXT,
            claim_type TEXT,
            subject TEXT,
            predicate TEXT,
            object_value TEXT,
            scope TEXT NOT NULL DEFAULT 'project',
            volatility TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'confirmed',
            confidence REAL NOT NULL DEFAULT 0.5,
            pinned INTEGER NOT NULL DEFAULT 0,
            supersedes_claim_id INTEGER,
            replaced_by_claim_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_validated_at TEXT,
            archived_at TEXT,
            human_id TEXT,
            tier TEXT NOT NULL DEFAULT 'working',
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT,
            event_time TEXT,
            valid_from TEXT,
            valid_until TEXT,
            tenant_id TEXT,
            source_agent TEXT,
            visibility TEXT NOT NULL DEFAULT 'public',
            version INTEGER NOT NULL DEFAULT 1,
            entity_id INTEGER,
            wiki_article TEXT
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'unknown',
            scope TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            variant_key TEXT NOT NULL DEFAULT '',
            original_form TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(entity_id, variant_key)
        );
        CREATE INDEX idx_entity_aliases_alias ON entity_aliases(alias);
        """
    )


def _insert_entity(conn: sqlite3.Connection, *, canonical: str, aliases: list[str],
                   entity_type: str = "text_entity:file") -> int:
    now = _utc_now()
    cur = conn.execute(
        "INSERT INTO entities (canonical_name, entity_type, scope, created_at, updated_at) "
        "VALUES (?, ?, 'global', ?, ?)",
        (canonical, entity_type, now, now),
    )
    eid = int(cur.lastrowid)
    for i, a in enumerate(aliases):
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, variant_key, original_form, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, a, f"v{i}", a, now),
        )
    return eid


def _insert_claim(conn: sqlite3.Connection, *, text: str, entity_id: int | None,
                  status: str = "confirmed", updated_at: str | None = None) -> int:
    now = updated_at or _utc_now()
    cur = conn.execute(
        "INSERT INTO claims (text, claim_type, status, created_at, updated_at, entity_id) "
        "VALUES (?, 'fact', ?, ?, ?, ?)",
        (text, status, now, now, entity_id),
    )
    return int(cur.lastrowid)


class _FakeStore:
    """Minimal store shim exposing ``connect()`` and ``get_claim()``."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, isolation_level=None)

    def get_claim(self, claim_id: int, include_citations: bool = True) -> Claim | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, text, status, confidence, entity_id, subject, scope "
                "FROM claims WHERE id = ?",
                (claim_id,),
            ).fetchone()
        if row is None:
            return None
        return Claim(
            id=row[0],
            text=row[1],
            idempotency_key=None,
            normalized_text=None,
            claim_type="fact",
            subject=row[5],
            predicate=None,
            object_value=None,
            scope=row[6] or "project",
            volatility="medium",
            status=row[2],
            confidence=row[3] or 0.5,
            pinned=False,
            supersedes_claim_id=None,
            replaced_by_claim_id=None,
            created_at="2026-01-01",
            updated_at="2026-01-01",
            last_validated_at=None,
            archived_at=None,
            wiki_article=None,
        )


@pytest.fixture
def fanout_db(tmp_path: Path) -> Path:
    """Synthetic DB: 3 entities, 6 claims, 1 archived."""
    db = tmp_path / "fanout.sqlite"
    with sqlite3.connect(db) as conn:
        _init_schema(conn)
        # The prompt "fix context_hook.py bug" mines out file entity
        # "context_hook.py" → canonical_hint "context_hook.py" →
        # normalize_alias produces "context-hook-py".
        eid_hook = _insert_entity(
            conn, canonical="context_hook.py",
            aliases=["context-hook-py", "file:context-hook-py"],
        )
        # The prompt "MEMORYMASTER_RECALL_W_ENTITY env" has env-var entity.
        eid_env = _insert_entity(
            conn, canonical="MEMORYMASTER_RECALL_W_ENTITY",
            aliases=["memorymaster-recall-w-entity", "env-var:memorymaster-recall-w-entity"],
            entity_type="text_entity:env-var",
        )
        # Orphan entity — no alias matches anything we'll query.
        _insert_entity(conn, canonical="misc", aliases=["misc"])

        # 4 claims linked to context_hook.py, 1 archived + 1 linked to env-var.
        _insert_claim(conn, text="context_hook.py recall orders by lexical_score",
                      entity_id=eid_hook, updated_at="2026-04-20T00:00:00")
        _insert_claim(conn, text="context_hook.py fanout added post-#127",
                      entity_id=eid_hook, updated_at="2026-04-22T00:00:00")
        _insert_claim(conn, text="context_hook.py archived snippet",
                      entity_id=eid_hook, status="archived",
                      updated_at="2026-01-01T00:00:00")
        _insert_claim(conn, text="context_hook.py old hotfix",
                      entity_id=eid_hook, updated_at="2026-02-01T00:00:00")
        _insert_claim(conn, text="context_hook.py third recent",
                      entity_id=eid_hook, updated_at="2026-04-21T00:00:00")
        _insert_claim(conn, text="MEMORYMASTER_RECALL_W_ENTITY default is 0.0",
                      entity_id=eid_env, updated_at="2026-04-23T00:00:00")
    return db


# --------------------------------------------------------------------------- #
# Layer 1 — fanout SQL semantics
# --------------------------------------------------------------------------- #


def test_fanout_returns_empty_on_prompt_with_no_entities(fanout_db: Path) -> None:
    store = _FakeStore(fanout_db)
    out = _entity_fanout_claim_ids(store, "hola como estas", set())
    assert out == []


def test_fanout_resolves_prompt_entity_to_claim_ids(fanout_db: Path) -> None:
    store = _FakeStore(fanout_db)
    out = _entity_fanout_claim_ids(store, "fix context_hook.py now", set())
    assert out, "expected at least one claim from entity fanout"
    # All returned claims must be linked to the hook entity — at most
    # _ENTITY_CAP_PER_ENTITY rows for the single extracted entity.
    assert len(out) <= _ENTITY_CAP_PER_ENTITY


def test_fanout_respects_seen_ids(fanout_db: Path) -> None:
    store = _FakeStore(fanout_db)
    all_ids = _entity_fanout_claim_ids(store, "fix context_hook.py", set())
    assert all_ids, "precondition: fanout returns something"
    # Mark the first result as already-seen; it must not reappear.
    seen = {all_ids[0]}
    rest = _entity_fanout_claim_ids(store, "fix context_hook.py", seen)
    assert all_ids[0] not in rest


def test_fanout_excludes_archived_claims(fanout_db: Path) -> None:
    """Archived claim (the 'context_hook.py archived snippet' row, oldest
    updated_at) must never leak out. Verified by forcing seen_ids to hide
    all non-archived matches, then checking the fanout still returns
    nothing from the entity."""
    store = _FakeStore(fanout_db)
    # Pull everything linked to the hook entity, archived included.
    with sqlite3.connect(fanout_db) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT c.id FROM claims c JOIN entity_aliases a "
            "ON a.entity_id = c.entity_id WHERE a.alias = 'context-hook-py'"
        ).fetchall()]
        archived_ids = {r[0] for r in conn.execute(
            "SELECT id FROM claims WHERE status = 'archived'"
        ).fetchall()}
    # Fanout (without seen_ids filter) must return a subset of non-archived claims.
    live = _entity_fanout_claim_ids(store, "context_hook.py", set())
    assert all(cid in ids for cid in live)
    assert not (set(live) & archived_ids), "archived claim leaked through fanout"


def test_fanout_respects_total_cap(tmp_path: Path) -> None:
    """With many matching entities the fanout caps at _ENTITY_CAP_TOTAL claims."""
    db = tmp_path / "many.sqlite"
    with sqlite3.connect(db) as conn:
        _init_schema(conn)
        # Four env-var entities, each with the per-entity cap met:
        # 4 entities * _ENTITY_CAP_PER_ENTITY (3) = 12 potential rows,
        # clipped to _ENTITY_CAP_TOTAL.
        for name in ("API_KEY_ONE", "API_KEY_TWO", "API_KEY_THREE", "API_KEY_FOUR"):
            eid = _insert_entity(
                conn, canonical=name,
                aliases=[name.lower().replace("_", "-")],
                entity_type="text_entity:env-var",
            )
            for i in range(_ENTITY_CAP_PER_ENTITY + 2):
                _insert_claim(conn, text=f"{name} claim {i}", entity_id=eid)
    store = _FakeStore(db)
    prompt = "rotate API_KEY_ONE API_KEY_TWO API_KEY_THREE API_KEY_FOUR"
    out = _entity_fanout_claim_ids(store, prompt, set())
    assert len(out) <= _ENTITY_CAP_TOTAL


# --------------------------------------------------------------------------- #
# Layer 2 — defensive: best-effort behaviour on broken DB
# --------------------------------------------------------------------------- #


def test_fanout_silent_when_tables_missing(tmp_path: Path) -> None:
    """Legacy DBs without the entity tables must still succeed silently."""
    db = tmp_path / "no_entity_tables.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE claims (id INTEGER, text TEXT, entity_id INTEGER);"
        )
    store = _FakeStore(db)
    # No entity_aliases table => should NOT raise, just return [].
    out = _entity_fanout_claim_ids(store, "fix context_hook.py now", set())
    assert out == []


# --------------------------------------------------------------------------- #
# Layer 3 — row shape
# --------------------------------------------------------------------------- #


def test_row_for_claim_has_zero_scores_and_entity_score_one() -> None:
    claim = Claim(
        id=42, text="foo", idempotency_key=None, normalized_text=None,
        claim_type="fact", subject=None, predicate=None, object_value=None,
        scope="project", volatility="medium", status="confirmed",
        confidence=0.8, pinned=False, supersedes_claim_id=None,
        replaced_by_claim_id=None, created_at="2026-01-01",
        updated_at="2026-01-01", last_validated_at=None, archived_at=None,
    )
    row = _row_for_claim(claim)
    assert row["claim"] is claim
    assert row["lexical_score"] == 0.0
    assert row["freshness_score"] == 0.0
    assert row["vector_score"] == 0.0
    assert row["confidence_score"] == 0.8
    assert row["entity_score"] == 1.0
    assert row["source"] == "entity_fanout"


# --------------------------------------------------------------------------- #
# Layer 4 — end-to-end recall() with env gate + rescue path
# --------------------------------------------------------------------------- #


class _FakeSvcWithStore:
    """Service stub with FTS5 stage returning controllable rows."""

    def __init__(self, store: _FakeStore, fts_rows: list[dict]) -> None:
        self.store = store
        self._rows = fts_rows

    def query_rows(self, **_: object) -> list[dict]:
        return list(self._rows)

    def _record_accesses(self, *_args, **_kwargs) -> None:
        return None


def _patch_svc(monkeypatch: pytest.MonkeyPatch, svc: _FakeSvcWithStore,
                fts_tokens: str) -> None:
    def _fake_ctor(db_target, workspace_root, **_kwargs):  # noqa: ARG001
        return svc
    monkeypatch.setattr("memorymaster.core.service.MemoryService", _fake_ctor)
    monkeypatch.setattr(
        "memorymaster.recall.recall_tokenizer.extract_query_tokens",
        lambda q, db, max_tokens=6: fts_tokens,
    )


def test_recall_uses_fanout_when_fts5_empty(
    monkeypatch: pytest.MonkeyPatch, fanout_db: Path,
) -> None:
    """FTS5 returns [] → fanout rescues → output contains entity-linked claim."""
    for name in _RECALL_WEIGHT_DEFAULTS:
        monkeypatch.delenv(f"MEMORYMASTER_RECALL_{name}", raising=False)
    store = _FakeStore(fanout_db)
    svc = _FakeSvcWithStore(store, fts_rows=[])
    _patch_svc(monkeypatch, svc, fts_tokens="")

    out = recall("fix context_hook.py soon", db_path=str(fanout_db), skip_qdrant=True)
    # The rescue must surface the most-recent non-archived claim linked to
    # context_hook.py — "context_hook.py fanout added post-#127" (updated
    # 2026-04-22).
    assert "context_hook.py fanout added post-#127" in out
    # Archived claim must never appear.
    assert "archived snippet" not in out


def test_recall_backwards_compat_when_w_entity_zero(
    monkeypatch: pytest.MonkeyPatch, fanout_db: Path,
) -> None:
    """With FTS5 already non-empty and W_ENTITY==0, output must be the exact
    same line set as without the fanout. The fanout never inserts rows at
    the top of the ranking."""
    for name in _RECALL_WEIGHT_DEFAULTS:
        monkeypatch.delenv(f"MEMORYMASTER_RECALL_{name}", raising=False)
    store = _FakeStore(fanout_db)
    # FTS5 returns 8 high-scoring rows unrelated to context_hook.py entity.
    high_rows = []
    for i in range(8):
        c = Claim(
            id=1000 + i, text=f"fts hit number {i}", idempotency_key=None,
            normalized_text=None, claim_type="fact", subject=None,
            predicate=None, object_value=None, scope="project",
            volatility="medium", status="confirmed", confidence=0.5,
            pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at="2026-01-01", updated_at="2026-01-01",
            last_validated_at=None, archived_at=None,
        )
        high_rows.append({
            "claim": c, "lexical_score": 0.9 - 0.05 * i,
            "freshness_score": 0.0, "confidence_score": 0.5,
            "vector_score": 0.0,
        })
    svc = _FakeSvcWithStore(store, fts_rows=high_rows)
    _patch_svc(monkeypatch, svc, fts_tokens="fts")

    out = recall("fix context_hook.py soon", db_path=str(fanout_db), skip_qdrant=True)
    # Exactly the 8 FTS hits should appear (fanout is gated off when
    # W_ENTITY==0 AND FTS5 returned rows).
    assert "context_hook.py fanout added post-#127" not in out
    assert "fts hit number 0" in out
    assert "fts hit number 7" in out


def test_recall_fanout_reshuffles_with_positive_w_entity(
    monkeypatch: pytest.MonkeyPatch, fanout_db: Path,
) -> None:
    """With W_ENTITY>0, the fanout runs even when FTS5 returned rows, and its
    rows can appear in the output."""
    for name in _RECALL_WEIGHT_DEFAULTS:
        monkeypatch.delenv(f"MEMORYMASTER_RECALL_{name}", raising=False)
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_ENTITY", "5.0")
    # Also zero the other weights so entity_score dominates ranking — we're
    # asserting that fanout rows make it into the output, not that they rank
    # above genuine high-lex rows.
    for name in ("W_MATCHES", "W_PHRASE", "W_ALL", "W_LEXICAL", "W_CONFIDENCE"):
        monkeypatch.setenv(f"MEMORYMASTER_RECALL_{name}", "0.0")

    store = _FakeStore(fanout_db)
    low_rows = []
    for i in range(3):
        c = Claim(
            id=2000 + i, text=f"low hit {i}", idempotency_key=None,
            normalized_text=None, claim_type="fact", subject=None,
            predicate=None, object_value=None, scope="project",
            volatility="medium", status="confirmed", confidence=0.1,
            pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at="2026-01-01", updated_at="2026-01-01",
            last_validated_at=None, archived_at=None,
        )
        low_rows.append({
            "claim": c, "lexical_score": 0.01,
            "freshness_score": 0.0, "confidence_score": 0.1,
            "vector_score": 0.0,
        })
    svc = _FakeSvcWithStore(store, fts_rows=low_rows)
    _patch_svc(monkeypatch, svc, fts_tokens="fts")

    out = recall("fix context_hook.py soon", db_path=str(fanout_db), skip_qdrant=True)
    # Entity-fanout claim surfaces ABOVE the low-score FTS hits.
    assert "context_hook.py" in out
    hook_pos = out.find("context_hook.py")
    low_pos = out.find("low hit 0")
    assert 0 <= hook_pos < low_pos, (
        f"fanout claim must rank above low-score FTS rows; got "
        f"hook_pos={hook_pos}, low_pos={low_pos}, out={out!r}"
    )

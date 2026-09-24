"""F-13: the hybrid broad stream must filter status in SQL, not after paging.

Review repro ``review-A/r1_status_scan.py``: with 30,001 ``candidate`` rows and a
single ``confirmed`` target, the default hybrid query (statuses confirmed /
stale / conflicted) paged ``list_claims_page`` 118 times and loaded every
candidate row plus citations, only to discard them in Python.  The requested
statuses belong in the page query so rejected statuses never leave SQLite.
"""
from __future__ import annotations

import sqlite3

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

CIT = [CitationInput(source="synthetic-test")]
NOISE_ROWS = 3_000  # > 11 pages of 256 before the fix


class _Provider:
    model = "status-pushdown-test"
    is_semantic = True

    def embed(self, text):
        return [1.0, 0.0]

    def embed_batch(self, texts):
        return [[1.0, 0.0] if "TARGET" in text else [0.0, 1.0] for text in texts]


def _clone(db, src_id: int, n: int, status: str) -> None:
    """Bulk-clone a seed row in the temp DB (fixture only, never a real DB)."""
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)") if r[1] != "id"]
    select = []
    for col in cols:
        if col == "text":
            select.append("text || '-' || n")
        elif col in ("idempotency_key", "human_id"):
            select.append("NULL")
        elif col == "subject":
            select.append("'subj-' || n")
        elif col == "status":
            select.append(f"'{status}'")
        else:
            select.append(col)
    conn.execute(
        f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < {n}) "
        f"INSERT INTO claims ({','.join(cols)}) SELECT {','.join(select)} "
        "FROM claims, s WHERE claims.id = ?",
        (src_id,),
    )
    conn.commit()
    conn.close()


def test_hybrid_broad_stream_pushes_requested_statuses_into_sql(tmp_path):
    db = tmp_path / "status-pushdown.db"
    svc = MemoryService(db, workspace_root=tmp_path, tenant_id="tenant-a")
    svc.init_db()
    seed = svc.store.create_claim(
        text="noise candidate", citations=CIT, confidence=0.99,
        tenant_id="tenant-a", scope="project:x",
    )
    target = svc.store.create_claim(
        text="TARGET orbital relay", citations=CIT, confidence=0.5,
        tenant_id="tenant-a", scope="project:x",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (target.id,))
    _clone(db, seed.id, NOISE_ROWS, "candidate")

    pages = {"calls": 0, "rows": 0}
    original = svc.store.list_claims_page

    def counting(**kwargs):
        claims, cursor = original(**kwargs)
        pages["calls"] += 1
        pages["rows"] += len(claims)
        return claims, cursor

    svc.store.list_claims_page = counting
    svc.embedding_provider = _Provider()
    rows = svc.query_rows(
        "how do satellites talk", retrieval_mode="hybrid", limit=5,
        scope_allowlist=["project:x"],
    )

    assert [row["claim"].id for row in rows] == [target.id]
    assert pages["calls"] == 1, f"paged {pages['calls']} times over rejected statuses"
    assert pages["rows"] == 1, "candidate rows must not be loaded for a confirmed-only query"


def test_sqlite_list_claims_page_status_in_filters_and_keeps_archived_semantics(tmp_path):
    svc = MemoryService(tmp_path / "page.db", workspace_root=tmp_path)
    svc.init_db()
    ids = {}
    for status in ("candidate", "confirmed", "stale", "archived"):
        claim = svc.store.create_claim(text=f"row {status}", citations=CIT, confidence=0.5)
        with svc.store.connect() as conn:
            conn.execute("UPDATE claims SET status=? WHERE id=?", (status, claim.id))
            conn.commit()
        ids[status] = claim.id

    page, _ = svc.store.list_claims_page(limit=10, status_in=["confirmed", "stale"])
    assert sorted(c.id for c in page) == sorted([ids["confirmed"], ids["stale"]])

    archived, _ = svc.store.list_claims_page(limit=10, status_in=["archived"])
    assert [c.id for c in archived] == [ids["archived"]], "explicit archived request is honoured"

    unfiltered, _ = svc.store.list_claims_page(limit=10)
    assert ids["archived"] not in {c.id for c in unfiltered}


def test_postgres_list_claims_page_status_in_uses_psycopg_placeholders():
    from memorymaster.stores.postgres_store import PostgresStore

    executed: list[tuple[str, list[object]]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, list(params or [])))

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    store = PostgresStore.__new__(PostgresStore)
    store.tenant_id = None
    store.connect = lambda: _Conn()  # type: ignore[method-assign]

    claims, cursor = store.list_claims_page(limit=5, status_in=["confirmed", "stale"])

    assert (claims, cursor) == ([], "")
    sql, params = executed[-1]
    assert "status IN (%s,%s)" in sql
    assert "?" not in sql
    assert params[:2] == ["confirmed", "stale"]
    assert "status <> 'archived'" in sql

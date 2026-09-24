"""F-18: init_db leaves the planner statistics the skill-catalog index needs.

Review repro ``review-A/r7b_plan_populated.py``: on a populated database the
tenant-scoped skill-catalog enumeration used ``idx_claims_tenant_id`` (a scan
of every tenant row) until someone ran ANALYZE; afterwards it used the partial
``idx_claims_active_skill_catalog`` index from migration 0025.  The upgrade /
restart path (init_db after migrations) must provide those statistics itself.
"""
from __future__ import annotations

import sqlite3

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

SKILL_SQL = (
    "SELECT id FROM claims WHERE claim_type = 'skill' AND status = 'confirmed' "
    "AND replaced_by_claim_id IS NULL AND tenant_id = ? AND scope IN (?) AND id > ? "
    "ORDER BY id LIMIT 256"
)
PARAMS = ["tenant-a", "project:x", 0]


def _plan(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + SKILL_SQL, PARAMS))
    finally:
        conn.close()


def _claims_analyzed(db) -> bool:
    conn = sqlite3.connect(db)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone() is None:
            return False
        return conn.execute("SELECT 1 FROM sqlite_stat1 WHERE tbl='claims'").fetchone() is not None
    finally:
        conn.close()


def _populate(db, svc, n: int = 20_000) -> None:
    seed = svc.store.create_claim(
        text="ordinary", citations=[CitationInput(source="synthetic-test")],
        confidence=0.5, tenant_id="tenant-a", scope="project:x",
    )
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)") if r[1] != "id"]
    select = [
        "text || '-' || n" if col == "text"
        else "NULL" if col in ("idempotency_key", "human_id")
        else "'subj-' || n" if col == "subject"
        else "'confirmed'" if col == "status"
        else col
        for col in cols
    ]
    conn.execute(
        f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < {n}) "
        f"INSERT INTO claims ({','.join(cols)}) SELECT {','.join(select)} "
        "FROM claims, s WHERE claims.id = ?",
        (seed.id,),
    )
    conn.execute("UPDATE claims SET claim_type='skill' WHERE id IN (5, 500, 15000)")
    conn.commit()
    conn.close()


def test_init_db_on_a_populated_database_lets_the_planner_use_the_skill_index(tmp_path):
    db = tmp_path / "skill-plan.db"
    svc = MemoryService(db, workspace_root=tmp_path, tenant_id="tenant-a")
    svc.init_db()
    assert not _claims_analyzed(db), "an empty claims table must not be given empty statistics"
    _populate(db, svc)
    assert "idx_claims_tenant_id" in _plan(db), "fixture must reproduce the unanalyzed plan"

    MemoryService(db, workspace_root=tmp_path, tenant_id="tenant-a").init_db()

    assert _claims_analyzed(db)
    assert "idx_claims_active_skill_catalog" in _plan(db)
    conn = sqlite3.connect(db)
    try:
        rows = [r[0] for r in conn.execute(SKILL_SQL, PARAMS)]
    finally:
        conn.close()
    assert rows == [5, 500, 15000]

"""F-14: the lexical stream must page until it holds enough AUTHORIZED rows.

Review repro ``review-A/r2_lexical_starvation.py``: 71 foreign-private rows that
match the query out-rank the only authorized match.  The lexical stream was cut
to its row budget in SQL *before* visibility/sensitivity filtering, so the
authorized target never reached ranking in hybrid or legacy mode (recall loss;
no leak).  Both paths now keep paging the lexical stream until the budget is
filled with rows the caller may see, or the matches run out.
"""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.security import is_sensitive_claim
from memorymaster.core.service import MemoryService

CIT = [CitationInput(source="synthetic-test")]


class _Provider:
    model = "lexical-paging-test"
    is_semantic = True

    def embed(self, text):
        return [1.0, 0.0]

    def embed_batch(self, texts):
        return [[1.0, 0.0] if "TARGET" in text else [0.0, 1.0] for text in texts]


def _clone(db, src_id: int, n: int, **overrides) -> None:
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
        elif col in overrides:
            select.append(repr(overrides[col]) if isinstance(overrides[col], str) else str(overrides[col]))
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


def _confirm(db, *ids: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.executemany("UPDATE claims SET status='confirmed' WHERE id=?", [(i,) for i in ids])


def _build(tmp_path, *, blocker: str):
    db = tmp_path / f"lexical-{blocker}.db"
    svc = MemoryService(db, workspace_root=tmp_path, tenant_id="tenant-a")
    svc.init_db()
    filler = svc.store.create_claim(
        text="filler public note", citations=CIT, confidence=0.9,
        tenant_id="tenant-a", scope="project:x",
    )
    _clone(db, filler.id, 1000, status="confirmed", confidence=0.9)
    if blocker == "private":
        noise = svc.store.create_claim(
            text="zebracode zebracode zebracode private", citations=CIT, confidence=0.99,
            tenant_id="tenant-a", scope="project:x", source_agent="other-agent",
            visibility="private",
        )
    else:
        noise = svc.store.create_claim(
            text="zebracode zebracode zebracode AKIAIOSFODNN7EXAMPLE", citations=CIT,
            confidence=0.99, tenant_id="tenant-a", scope="project:x",
        )
        assert is_sensitive_claim(noise), "fixture must exercise sensitivity filtering"
    _clone(db, noise.id, 70, status="confirmed", confidence=0.99)
    target = svc.store.create_claim(
        text="TARGET zebracode public answer", citations=CIT, confidence=0.05,
        tenant_id="tenant-a", scope="project:x",
    )
    _confirm(db, noise.id, target.id, filler.id)
    svc.embedding_provider = _Provider()
    return svc, target


@pytest.mark.parametrize("mode", ["hybrid", "legacy"])
@pytest.mark.parametrize("blocker", ["private", "sensitive"])
def test_unauthorized_lexical_matches_cannot_starve_an_authorized_match(tmp_path, mode, blocker):
    svc, target = _build(tmp_path, blocker=blocker)

    rows = svc.query_rows(
        "zebracode", retrieval_mode=mode, limit=10, scope_allowlist=["project:x"],
        requesting_agent="reader-agent",
    )

    returned = [row["claim"] for row in rows]
    assert target.id in {claim.id for claim in returned}, (
        f"{mode}: 71 {blocker} rows starved the authorized lexical match"
    )
    assert not any(claim.visibility == "private" for claim in returned)
    assert not any(is_sensitive_claim(claim) for claim in returned)


def test_legacy_candidates_without_authorizer_keep_their_row_budget(tmp_path):
    """The unauthorized call shape stays a plain bounded fetch."""
    svc, _ = _build(tmp_path, blocker="private")
    rows = svc._legacy_candidates("zebracode", 10, ["confirmed"], ["project:x"])
    assert len(rows) == 10


def test_planner_or_terms_share_one_scan_ceiling(tmp_path):
    """Verifier cost note: each planner OR term grew its own window to the
    ceiling (up to 32 x 8,192 rows with citations on the recall hot path).
    The ceiling now bounds the rows fetched across all terms of one window."""
    from memorymaster.recall import candidate_pool

    svc = MemoryService(tmp_path / "cap.db", workspace_root=tmp_path)
    svc.init_db()
    row = svc.store.create_claim(text="zebracode unauthorized", citations=CIT, confidence=0.5)
    windows: list[list[int]] = []

    def list_claims(*, limit, text_query, **kwargs):
        if text_query == "t0":
            windows.append([])
        windows[-1].append(limit)
        return [row] * limit  # never exhausted

    svc.store.list_claims = list_claims  # type: ignore[method-assign]
    terms = " OR ".join(f"t{i}" for i in range(8))

    assert svc._legacy_candidates(terms, 10, ["confirmed"], None, authorize=lambda rows: []) == []

    assert all(len(window) == 8 for window in windows)
    assert max(sum(window) for window in windows) <= candidate_pool._LEXICAL_SCAN_CAP
    assert windows[-1][0] >= 10, "each term still gets at least the row budget"

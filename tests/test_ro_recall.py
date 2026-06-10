"""RO recall + access spool tests (P1 WAL-discipline spec step 8, §2.2).

WHY: the recall hook fires on EVERY user prompt and — pre-P1 — was a
writer: ``query_for_context → _record_accesses`` UPDATEs ``access_count``
on the shared 3.47 GB SQLite file that already suffered real btree
corruption (2026-06-05). Under ``MEMORYMASTER_WAL_DISCIPLINE=1`` the recall
service opens the DB strictly read-only (mode=ro + query_only — it can
NEVER take a write lock) and spools its access/feedback signal for the
steward drain. These tests pin the load-bearing requirements:

- RO recall returns the SAME context the RW path returns (the flag must be
  invisible to the model reading the injected block);
- the RO store takes zero write locks (probed with a concurrent writer
  holding the WAL write lock — recall must neither queue nor fail);
- NO signal is lost (the F9 silent-regression fix): access/feedback lines
  land in the spool, the drain replays them through record_accesses_batch
  / FeedbackTracker, and recompute_tiers sees the counts end-to-end;
- flag off = the untouched legacy direct-write path, bit-for-bit.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster import spool
from memorymaster._storage_shared import open_conn
from memorymaster.jobs import spool_drain
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

# FTS5 AND-joins every token — the query must be fully contained in a
# fixture claim or the legacy path matches nothing and records nothing.
QUERY = "qdrant reconciliation"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seeded tmp DB + isolated spool root; WAL-discipline flag UNSET.

    Tests must never touch the real ~/.memorymaster/spool nor inherit the
    dogfood flag from the dev machine's environment (the §5 rollout does
    ``setx MEMORYMASTER_WAL_DISCIPLINE 1`` user-wide).
    """
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool-root"))
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMORYMASTER_WAL_DISCIPLINE", raising=False)
    path = tmp_path / "ro-recall.db"
    svc = MemoryService(path)
    svc.init_db()
    for text in (
        "Qdrant reconciliation runs as a throttled steward cycle phase",
        "The WAL checkpoint discipline truncates the log every cycle",
        "Recall hooks read claims through a read-only connection",
    ):
        svc.ingest(text, [CitationInput(source="session://chat", locator="turn-1")])
    return path


def _ro(db: Path) -> MemoryService:
    return MemoryService(db, read_only=True)


def _query(svc: MemoryService) -> str:
    result = svc.query_for_context(
        QUERY,
        retrieval_mode="legacy",
        include_candidates=True,
    )
    return result.output if hasattr(result, "output") else str(result)


def _access_counts(db: Path) -> list[int]:
    with open_conn(db) as conn:
        return [int(r[0]) for r in conn.execute(
            "SELECT access_count FROM claims ORDER BY id"
        ).fetchall()]


def _spool_envelopes(db: Path) -> list[dict]:
    spool_dir = spool.spool_dir_for(db)
    lines: list[dict] = []
    if spool_dir.exists():
        for path in sorted(spool_dir.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                lines.append(json.loads(raw))
    return lines


def test_ro_recall_returns_identical_context_to_rw(db: Path) -> None:
    """REQUIREMENT (spec step 8): under the flag, recall must return the
    exact context the legacy RW path returns on the same DB — the flag is a
    lock-avoidance mechanism, and any ranking/rendering drift would mean the
    model's memory silently changes when the operator flips it."""
    ro_output = _query(_ro(db))
    rw_output = _query(MemoryService(db))
    # The fixture claim must genuinely match — an empty/"no claims fit"
    # placeholder would make this parity check vacuously true.
    assert "Qdrant reconciliation" in ro_output
    assert ro_output == rw_output


def test_ro_store_takes_zero_write_locks(db: Path) -> None:
    """REQUIREMENT (spec step 8 'concurrent exclusive-lock probe'): the whole
    point of RO recall is removing the per-prompt process from the writer
    set. While a concurrent writer HOLDS the WAL write lock, RO recall must
    complete without queueing on it; and any write attempt through the RO
    store must raise instead of silently contending with the fleet."""
    probe = open_conn(db)
    try:
        probe.execute("BEGIN IMMEDIATE")  # take and hold the write lock
        # Reads complete fine while the write lock is held elsewhere.
        assert "Qdrant reconciliation" in _query(_ro(db))
    finally:
        probe.rollback()
        probe.close()

    ro = _ro(db)
    with pytest.raises(sqlite3.OperationalError):
        with ro.store.connect() as conn:
            conn.execute("UPDATE claims SET access_count = 99")

    # After RO recall, a writer can take the exclusive lock immediately —
    # the RO service left no lingering write/reserved lock behind.
    probe2 = open_conn(db)
    try:
        probe2.execute("BEGIN EXCLUSIVE")
    finally:
        probe2.rollback()
        probe2.close()


def test_ro_recall_spools_access_signal_instead_of_writing(db: Path) -> None:
    """REQUIREMENT (F9 fix, spec §2.2): a naive read-only connection would
    have its access UPDATE eaten by suppress(Exception) — silently killing
    the tiering/decay/quality signal. The RO branch must instead append
    access + feedback envelopes to the spool while the DB rows stay
    untouched. NO signal may be lost."""
    before = _access_counts(db)
    assert _query(_ro(db))
    assert _access_counts(db) == before  # zero DB writes from RO recall

    envelopes = _spool_envelopes(db)
    by_op = {e["op"]: e for e in envelopes}
    assert set(by_op) == {"access", "feedback"}
    assert by_op["access"]["payload"]["claim_ids"]
    assert by_op["access"]["payload"]["query_hash"]
    assert by_op["feedback"]["payload"]["query_text"] == QUERY


def test_drained_access_signal_feeds_tiering_end_to_end(db: Path) -> None:
    """REQUIREMENT (spec step 8): the spooled signal must reach
    recompute_tiers — tier promotion is WHY access recording exists. A
    backdated zero-access claim is 'peripheral'; after 6 RO recalls are
    drained through record_accesses_batch its access_count exceeds the
    core threshold (>5) and recompute_tiers must promote it."""
    rw = MemoryService(db)
    # Backdate creation beyond the 90-day peripheral cutoff (test seeding
    # only — created_at is not a lifecycle status, see claims-lifecycle.md).
    with rw.store.connect() as conn:
        conn.execute("UPDATE claims SET created_at = '2025-01-01T00:00:00+00:00'")
        conn.commit()
    tiers = rw.store.recompute_tiers()
    assert tiers["peripheral"] >= 1  # old + never accessed → peripheral

    ro = _ro(db)
    for _ in range(6):
        assert _query(ro)
    assert all(count == 0 for count in _access_counts(db))  # still unwritten

    drained = spool_drain.run(rw)
    assert drained["quarantined"] == 0
    assert drained["by_op"]["access"] == 6

    counts = _access_counts(db)
    assert max(counts) == 6  # every spooled access reached the DB rows
    rw.store.recompute_tiers()
    with rw.store.connect() as conn:
        tier_rows = conn.execute(
            "SELECT tier FROM claims WHERE access_count > 5"
        ).fetchall()
    assert tier_rows and all(r[0] == "core" for r in tier_rows)


def test_flag_off_keeps_legacy_direct_write_path(db: Path) -> None:
    """REQUIREMENT (governance: flag default OFF, legacy path intact): with
    the flag unset, recall records accesses directly in the DB and the spool
    stays empty — the v3.27 behavior is the untouched else-branch."""
    from memorymaster.context_hook import recall

    before = _access_counts(db)
    rendered = recall(QUERY, db_path=str(db), skip_qdrant=True)
    assert rendered  # fixture claims match — the path actually ran
    assert sum(_access_counts(db)) > sum(before)  # direct UPDATE happened
    assert spool.pending_depth(db) == {"files": 0, "lines": 0}


def test_recall_entrypoint_goes_read_only_under_flag(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIREMENT (spec §2.2): the gate lives in the recall path itself so
    the installed hook template needs only the inherited env var — recall()
    under MEMORYMASTER_WAL_DISCIPLINE=1 must build an RO service, leave the
    DB rows untouched, and spool the access signal."""
    monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")
    from memorymaster.context_hook import recall

    before = _access_counts(db)
    rendered = recall(QUERY, db_path=str(db), skip_qdrant=True)
    assert rendered  # same fixture claims — RO recall still answers
    assert _access_counts(db) == before  # no per-prompt write lock taken
    ops = [e["op"] for e in _spool_envelopes(db)]
    assert "access" in ops and "feedback" in ops


class _FakeEmbedProvider:
    """Deterministic in-memory embedder — no network, no model download."""

    model = "fake-embed-soak"

    def embed(self, text: str) -> list[float]:
        return [1.0, float(len(text) % 7), float(text.count("a") % 5)]


def test_ro_hybrid_vector_scoring_never_writes_embedding_cache(db: Path) -> None:
    """REQUIREMENT (found by the step-12 chaos soak): hybrid retrieval's
    vector hook lazily UPSERTs missing claim embeddings — a WRITE on the
    read path, exactly the F9 class RO recall exists to kill. On the RO
    store this raised 'attempt to write a readonly database' and killed the
    WHOLE per-prompt recall whenever a concurrently-ingested claim had no
    cached embedding yet (invisible on quiet fixtures, near-constant under
    fleet churn). RO stores must score uncached claims in memory: zero rows
    written, no exception, and a usable score for EVERY candidate — while
    the RW path keeps populating the cache exactly as before."""
    claims = MemoryService(db).query(QUERY, include_candidates=True)
    assert claims, "fixture must match or the test is vacuous"

    def embedding_rows() -> int:
        with open_conn(db) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM claim_embeddings").fetchone()[0])

    # Force the uncached case the soak hit: claims exist, cache is empty.
    with open_conn(db) as conn:
        conn.execute("DELETE FROM claim_embeddings")
        conn.commit()

    scores = _ro(db).store.vector_scores(QUERY, claims, _FakeEmbedProvider())
    assert set(scores) == {c.id for c in claims}, "every uncached claim must still get a score"
    assert all(0.0 <= s <= 1.0 for s in scores.values())
    assert embedding_rows() == 0, "the read path wrote to claim_embeddings"

    # Legacy RW behavior is the untouched else-branch: cache fills up.
    rw_scores = MemoryService(db).store.vector_scores(QUERY, claims, _FakeEmbedProvider())
    assert embedding_rows() == len(claims)
    assert rw_scores == scores, "RO in-memory scoring must match the RW cached scoring"

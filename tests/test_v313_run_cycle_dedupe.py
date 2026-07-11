"""Integration test for v3.13 dedupe stage wired into MemoryService.run_cycle.

This test guards against the v3.13.0 ship gap: the dedupe was originally
wired into llm_steward.run_steward which has no production callers. This
test exercises the actual cron path (run_cycle) so we catch any future
regression where the dedupe stage gets unhooked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "v313_run_cycle.db"


def _seed(service: MemoryService, text: str, predicate: str, oid: str) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="session://chat", locator="t", excerpt="seed")],
        subject="user",
        predicate=predicate,
        object_value=oid,
        confidence=0.7,
    )
    return int(claim.id)


def _force_confirmed(db: Path, claim_id: int) -> None:
    con = sqlite3.connect(str(db))
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update")
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert")
    con.execute("DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique")
    con.execute("DROP INDEX IF EXISTS idx_claims_public_confirmed_tuple_unique")
    con.execute(
        "DROP INDEX IF EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique"
    )
    con.execute("UPDATE claims SET status='confirmed' WHERE id=?", (claim_id,))
    con.commit()
    con.close()


def test_run_cycle_emits_dedupe_key_when_disabled(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_ENABLED", raising=False)
    service = MemoryService(tmp_db)
    service.init_db()
    _seed(service, "user prefers vim editor with vim plugins for python", "p", "vim")

    result = service.run_cycle()

    assert "dedupe" in result, "run_cycle must always emit the dedupe key"
    assert result["dedupe"]["enabled"] is False
    assert result["dedupe"]["archived"] == 0
    assert result["dedupe"]["would_archive"] == 0


def test_run_cycle_shadow_records_pair_details(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    service = MemoryService(tmp_db)
    service.init_db()
    canonical_id = _seed(
        service,
        "user prefers vim editor with vim plugins for python development",
        "editor_pref",
        "vim",
    )
    _force_confirmed(tmp_db, canonical_id)

    paraphrase_id = _seed(
        service,
        "user prefers vim editor with vim plugins for python coding",
        "editor_pref_alt",
        "vim",
    )

    result = service.run_cycle()

    assert result["dedupe"]["enabled"] is True
    assert result["dedupe"]["shadow"] is True
    assert result["dedupe"]["would_archive"] >= 1
    assert result["dedupe"]["archived"] == 0

    sample = result["dedupe"]["results"][0]
    assert sample["claim_id"] == paraphrase_id
    assert sample["canonical_id"] == canonical_id
    assert sample["would_archive"] is True
    assert sample["score"] is not None and sample["score"] >= 0.7

    con = sqlite3.connect(str(tmp_db))
    row = con.execute("SELECT status FROM claims WHERE id=?", (paraphrase_id,)).fetchone()
    con.close()
    assert row[0] != "archived", "shadow mode must not actually archive"


def test_run_cycle_active_archives_paraphrase(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    service = MemoryService(tmp_db)
    service.init_db()
    canonical_id = _seed(
        service,
        "user prefers vim editor with vim plugins for python development",
        "editor_pref",
        "vim",
    )
    _force_confirmed(tmp_db, canonical_id)

    paraphrase_id = _seed(
        service,
        "user prefers vim editor with vim plugins for python coding",
        "editor_pref_alt",
        "vim",
    )

    result = service.run_cycle()

    assert result["dedupe"]["archived"] >= 1
    assert result["dedupe"]["would_archive"] == 0

    con = sqlite3.connect(str(tmp_db))
    row = con.execute(
        "SELECT status, replaced_by_claim_id FROM claims WHERE id=?",
        (paraphrase_id,),
    ).fetchone()
    con.close()
    assert row[0] == "archived"
    assert row[1] == canonical_id


def test_run_cycle_dedupe_runs_before_validator(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-dup candidate should be archived BEFORE validator processes
    it. Otherwise validator would waste a classifier prediction on a claim
    we're about to archive."""
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    service = MemoryService(tmp_db)
    service.init_db()
    canonical_id = _seed(
        service,
        "user prefers vim editor with vim plugins for python development",
        "editor_pref",
        "vim",
    )
    _force_confirmed(tmp_db, canonical_id)
    _seed(
        service,
        "user prefers vim editor with vim plugins for python coding",
        "editor_pref_alt",
        "vim",
    )

    result = service.run_cycle()

    assert result["dedupe"]["archived"] >= 1
    assert result["validator"]["candidate_processed"] <= 1, (
        "validator should NOT process the candidate that dedupe archived"
    )

"""E2E tests for v3.13 dedupe wired through run_steward."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster import llm_steward
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "v313.db"


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
    con.execute("UPDATE claims SET status='confirmed' WHERE id=?", (claim_id,))
    con.commit()
    con.close()


def test_dedupe_archives_paraphrase_without_llm(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    extract_calls: list[int] = []

    def _fake_extract(*args, **kwargs):
        extract_calls.append(args[3])
        raise AssertionError("LLM should not be called for deduped paraphrase")

    monkeypatch.setattr(llm_steward, "extract_claim", _fake_extract)
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    stats = llm_steward.run_steward(
        db_path=str(tmp_db),
        api_key="",
        provider="gemini",
        limit=50,
        dry_run=False,
        auto_validate=False,
    )

    assert stats["dedupe_archived"] >= 1
    assert stats["dedupe_passthrough"] == 0
    assert paraphrase_id not in extract_calls

    con = sqlite3.connect(str(tmp_db))
    row = con.execute(
        "SELECT status, replaced_by_claim_id FROM claims WHERE id=?",
        (paraphrase_id,),
    ).fetchone()
    con.close()
    assert row[0] == "archived"
    assert row[1] == canonical_id


def test_dedupe_disabled_passes_to_llm(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    extract_calls: list[int] = []

    def _fake_extract(*args, **kwargs):
        extract_calls.append(args[3])

        class _R:
            action = "archive"
            extractions: list = []

        return _R()

    monkeypatch.setattr(llm_steward, "extract_claim", _fake_extract)
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_ENABLED", raising=False)
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")

    stats = llm_steward.run_steward(
        db_path=str(tmp_db),
        api_key="",
        provider="gemini",
        limit=50,
        dry_run=False,
        auto_validate=False,
    )

    assert stats["dedupe_archived"] == 0
    assert paraphrase_id in extract_calls


def test_dedupe_shadow_mode_does_not_archive(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def _fake_extract(*args, **kwargs):
        class _R:
            action = "archive"
            extractions: list = []

        return _R()

    monkeypatch.setattr(llm_steward, "extract_claim", _fake_extract)
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    stats = llm_steward.run_steward(
        db_path=str(tmp_db),
        api_key="",
        provider="gemini",
        limit=50,
        dry_run=False,
        auto_validate=False,
    )

    assert stats["dedupe_would_archive"] >= 1
    assert stats["dedupe_archived"] == 0

    con = sqlite3.connect(str(tmp_db))
    row = con.execute(
        "SELECT status FROM claims WHERE id=?",
        (paraphrase_id,),
    ).fetchone()
    con.close()
    assert row[0] != "archived" or stats["archived"] >= 1

    shadow_results = [r for r in stats["results"] if "dedupe" in r]
    assert shadow_results, "shadow mode should record would-archive pair details"
    sample = shadow_results[0]
    assert sample["dedupe"]["would_archive"] is True
    assert sample["dedupe"]["canonical_id"] == canonical_id
    assert sample["dedupe"]["score"] is not None
    assert sample["claim_id"] == paraphrase_id


def test_dedupe_synthetic_corpus_30_dupes(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MemoryService(tmp_db)
    service.init_db()

    base = "user prefers tool {} for activity {}"
    confirmed_ids: list[int] = []
    for i in range(30):
        cid = _seed(
            service,
            base.format(f"tool{i}", f"activity{i}"),
            f"pref_{i}",
            f"tool{i}",
        )
        _force_confirmed(tmp_db, cid)
        confirmed_ids.append(cid)

    paraphrase_template = "user prefers tool {} for activity {} regularly"
    for i in range(30):
        _seed(
            service,
            paraphrase_template.format(f"tool{i}", f"activity{i}"),
            f"pref_alt_{i}",
            f"tool{i}",
        )

    for i in range(70):
        _seed(
            service,
            f"user uses widget{i} for novel scenario{i} with unique parameters",
            f"widget_{i}",
            f"widget{i}",
        )

    def _fake_extract(*args, **kwargs):
        class _R:
            action = "archive"
            extractions: list = []

        return _R()

    monkeypatch.setattr(llm_steward, "extract_claim", _fake_extract)
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.7")

    stats = llm_steward.run_steward(
        db_path=str(tmp_db),
        api_key="",
        provider="gemini",
        limit=200,
        dry_run=False,
        auto_validate=False,
    )

    assert stats["dedupe_archived"] >= 25
    assert stats["dedupe_avg_jaccard"] is not None

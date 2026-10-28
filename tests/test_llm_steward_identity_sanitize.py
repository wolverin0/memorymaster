"""T-1865 / F10: model-generated claim fields written by the standalone steward.

Every branch of ``llm_steward.run_steward`` that persists LLM output (first
extraction, duplicate update, extra insert) must run the canonical sanitizer and
must stay inside the source claim's identity (tenant, visibility, author).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.llm_steward import ExtractionResult, run_steward

SECRET = "sk-" + ("cafebabe" * 5)  # openai_key-shaped
BENIGN = "The compactor merges summaries older than the freshness window."


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    path = tmp_path / "steward.db"
    MemoryService(str(path), workspace_root=tmp_path).init_db()
    return path


def _candidate(db: Path, text: str = BENIGN, *, scope="project:test", tenant_id=None,
               visibility="public", source_agent=None) -> int:
    svc = MemoryService(str(db), tenant_id=tenant_id)
    return svc.store.create_claim(
        text, [CitationInput(source="t")], scope=scope, tenant_id=tenant_id,
        visibility=visibility, source_agent=source_agent,
        idempotency_key=f"k-{text[:12]}-{tenant_id}-{visibility}-{source_agent}",
    ).id


def _confirmed(db: Path, subject: str, predicate: str, **identity) -> int:
    cid = _candidate(db, f"existing {subject} {predicate} claim text", **identity)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE claims SET status='confirmed', subject=?, predicate=?, object_value='old' WHERE id=?",
            (subject, predicate, cid),
        )
    return cid


def _run(db: Path, monkeypatch, extractions: list[dict]) -> dict:
    def fake_extract(provider, api_key, model, claim_id, text, base_url="",
                     key_rotator=None, use_llm_provider=False):
        return ExtractionResult(
            claim_id=claim_id, original_text=text, extractions=list(extractions),
            action="confirm", raw_response="[]",
        )

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)
    return run_steward(str(db), api_key="x", provider="gemini", limit=100, delay=0.0,
                       auto_validate=False)


def _rows(db: Path, sql: str, params=()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _dump(db: Path) -> str:
    return "\n".join(
        str(v) for r in _rows(db, "SELECT text, subject, predicate, object_value FROM claims")
        for v in r if v is not None
    )


def test_first_extraction_with_secret_is_stored_redacted_and_confirmed(db, monkeypatch):
    """Same as svc.ingest: the redacted field is persisted; the candidate does not stay
    pending (it would be re-sent to the LLM every cycle)."""
    cid = _candidate(db)
    stats = _run(db, monkeypatch, [
        {"subject": "compactor", "predicate": "uses", "object_value": f"token {SECRET}", "confidence": 0.8},
    ])
    assert SECRET not in _dump(db)
    row = _rows(db, "SELECT status, object_value FROM claims WHERE id=?", (cid,))[0]
    assert row["status"] == "confirmed" and "[REDACTED:" in row["object_value"]
    assert stats["claims_filtered_sensitive"] == 1


def test_infra_triple_with_a_private_ip_still_confirms_with_the_ip_redacted(db, monkeypatch):
    cid = _candidate(db)
    _run(db, monkeypatch, [
        {"subject": "server", "predicate": "ip_address", "object_value": "10.0.0.1", "confidence": 0.9},
    ])
    row = _rows(db, "SELECT status, object_value FROM claims WHERE id=?", (cid,))[0]
    assert row["status"] == "confirmed" and "10.0.0.1" not in row["object_value"]


def test_first_extraction_with_secret_in_subject_or_predicate(db, monkeypatch):
    _candidate(db)
    _run(db, monkeypatch, [
        {"subject": f"key {SECRET}", "predicate": f"holds {SECRET}", "object_value": "x", "confidence": 0.8},
    ])
    assert SECRET not in _dump(db)


def test_duplicate_update_with_secret_writes_the_redacted_value(db, monkeypatch):
    dup = _confirmed(db, "compactor", "uses")
    _candidate(db)
    stats = _run(db, monkeypatch, [
        {"subject": "compactor", "predicate": "uses", "object_value": f"token {SECRET}", "confidence": 0.9},
    ])
    assert SECRET not in _dump(db)
    assert "[REDACTED:" in _rows(db, "SELECT object_value FROM claims WHERE id=?", (dup,))[0]["object_value"]
    assert stats["claims_filtered_sensitive"] == 1


def test_extra_extraction_update_is_redacted_and_new_row_is_dropped(db, monkeypatch):
    dup = _confirmed(db, "extra-a", "uses")
    _candidate(db)
    stats = _run(db, monkeypatch, [
        {"subject": "first", "predicate": "p", "object_value": "ok", "confidence": 0.8},
        {"subject": "extra-a", "predicate": "uses", "object_value": f"t {SECRET}", "confidence": 0.8},
        {"subject": "extra-b", "predicate": f"p {SECRET}", "object_value": "ok", "confidence": 0.8},
    ])
    assert SECRET not in _dump(db)
    assert "[REDACTED:" in _rows(db, "SELECT object_value FROM claims WHERE id=?", (dup,))[0]["object_value"]
    assert _rows(db, "SELECT id FROM claims WHERE subject='extra-b'") == []  # a NEW row is default-deny
    assert stats["claims_filtered_sensitive"] == 2


def test_benign_extraction_still_confirms_and_inserts(db, monkeypatch):
    cid = _candidate(db)
    stats = _run(db, monkeypatch, [
        {"subject": "first", "predicate": "p", "object_value": "v1", "confidence": 0.8},
        {"subject": "extra-b", "predicate": "p", "object_value": "v2", "confidence": 0.8},
    ])
    assert stats["confirmed"] == 1 and stats["claims_extracted"] == 1
    assert _rows(db, "SELECT status FROM claims WHERE id=?", (cid,))[0]["status"] == "confirmed"
    assert len(_rows(db, "SELECT id FROM claims WHERE subject='extra-b'")) == 1


def test_extra_insert_keeps_source_tenant_visibility_and_author(db, monkeypatch):
    _candidate(db, tenant_id="t1", visibility="private", source_agent="alice")
    _run(db, monkeypatch, [
        {"subject": "first", "predicate": "p", "object_value": "v1", "confidence": 0.8},
        {"subject": "extra-b", "predicate": "p", "object_value": "v2", "confidence": 0.8},
    ])
    extra = _rows(db, "SELECT tenant_id, visibility, source_agent FROM claims WHERE subject='extra-b'")
    assert [tuple(r) for r in extra] == [("t1", "private", "alice")]


def test_duplicate_lookup_does_not_cross_visibility_or_tenant(db, monkeypatch):
    public_twin = _confirmed(db, "first", "p")  # same scope, public, no tenant
    tenant_twin = _confirmed(db, "first", "p", tenant_id="t2")
    cand = _candidate(db, tenant_id="t1", visibility="private", source_agent="alice")
    _run(db, monkeypatch, [{"subject": "first", "predicate": "p", "object_value": "NEW", "confidence": 0.9}])
    for twin in (public_twin, tenant_twin):
        assert _rows(db, "SELECT object_value FROM claims WHERE id=?", (twin,))[0]["object_value"] == "old"
    assert _rows(db, "SELECT status, object_value FROM claims WHERE id=?", (cand,))[0]["object_value"] == "NEW"


def test_duplicate_lookup_still_merges_within_one_identity(db, monkeypatch):
    twin = _confirmed(db, "first", "p", visibility="private", source_agent="alice")
    _candidate(db, visibility="private", source_agent="alice")
    _run(db, monkeypatch, [{"subject": "first", "predicate": "p", "object_value": "NEW", "confidence": 0.9}])
    assert _rows(db, "SELECT object_value FROM claims WHERE id=?", (twin,))[0]["object_value"] == "NEW"

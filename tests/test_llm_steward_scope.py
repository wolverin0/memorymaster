"""Regression tests for the llm_steward.run_steward `scope` filter.

WHY this matters: the steward's candidate query was `WHERE status='candidate'
ORDER BY id LIMIT ?` — scope-blind. A maintenance run intended for one project
(e.g. project:memorymaster) would curate the lowest-id candidates across ALL
scopes, mutating other projects' claims. The `scope` filter lets a bounded,
scoped cycle touch ONLY the requested scope. When scope is omitted, behaviour is
unchanged (all scopes, id order).

These tests anchor on the requirement (scope isolation), not the implementation:
they assert WHICH candidate ids the steward actually fed to the LLM.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


from memorymaster import llm_steward
from memorymaster.llm_steward import ExtractionResult, run_steward
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _service(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "memory.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, scope: str) -> int:
    claim = svc.ingest(
        text=text,
        citations=[CitationInput(source="t", locator="t", excerpt=text)],
        scope=scope,
        claim_type="fact",
        source_agent="test",
    )
    return claim.id


def _candidate_scopes(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT scope, count(*) FROM claims WHERE status='candidate' GROUP BY scope"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


def _seed_two_scopes(svc: MemoryService) -> dict[str, list[int]]:
    """Interleave ids across two scopes so an id-ordered scan would mix them."""
    ids = {"project:memorymaster": [], "project:other": []}
    for i in range(4):
        ids["project:memorymaster"].append(
            _ingest(svc, f"memorymaster candidate fact number {i}", "project:memorymaster")
        )
        ids["project:other"].append(
            _ingest(svc, f"other project candidate fact number {i}", "project:other")
        )
    return ids


def test_scope_filter_feeds_only_that_scope(tmp_path, monkeypatch):
    """run_steward(scope='project:memorymaster') must feed the LLM ONLY
    project:memorymaster candidates, never project:other — even though ids
    interleave so an id-ordered scan would otherwise mix them."""
    svc = _service(tmp_path, monkeypatch)
    ids = _seed_two_scopes(svc)
    db_path = svc.store.db_path

    seen_claim_ids: list[int] = []

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None):
        seen_claim_ids.append(claim_id)
        return ExtractionResult(claim_id=claim_id, extractions=[])  # no-op: extract nothing

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)

    run_steward(db_path, api_key="x", provider="gemini", limit=100, delay=0.0,
                auto_validate=False, scope="project:memorymaster")

    assert set(seen_claim_ids) == set(ids["project:memorymaster"]), (
        "steward must process exactly the project:memorymaster candidates"
    )
    assert not (set(seen_claim_ids) & set(ids["project:other"])), (
        "steward must NOT touch project:other candidates under a scoped run"
    )


def test_no_scope_processes_all_scopes(tmp_path, monkeypatch):
    """Backward-compat: when scope is omitted, the steward processes candidates
    from all scopes (legacy behaviour preserved)."""
    svc = _service(tmp_path, monkeypatch)
    ids = _seed_two_scopes(svc)
    db_path = svc.store.db_path

    seen_claim_ids: list[int] = []

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None):
        seen_claim_ids.append(claim_id)
        return ExtractionResult(claim_id=claim_id, extractions=[])

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)

    run_steward(db_path, api_key="x", provider="gemini", limit=100, delay=0.0,
                auto_validate=False)  # scope omitted

    all_ids = set(ids["project:memorymaster"]) | set(ids["project:other"])
    assert set(seen_claim_ids) == all_ids, "unscoped run must process all scopes"


def test_scope_dry_run_mutates_nothing(tmp_path, monkeypatch):
    """A scoped dry_run must not change any claim status in ANY scope —
    candidate counts per scope are identical before and after."""
    svc = _service(tmp_path, monkeypatch)
    _seed_two_scopes(svc)
    db_path = svc.store.db_path

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None):
        # Even if the LLM "extracted" something, dry_run must persist nothing.
        return ExtractionResult(claim_id=claim_id, extractions=[])

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)

    before = _candidate_scopes(db_path)
    run_steward(db_path, api_key="x", provider="gemini", limit=100, delay=0.0,
                dry_run=True, auto_validate=False, scope="project:memorymaster")
    after = _candidate_scopes(db_path)

    assert before == after == {"project:memorymaster": 4, "project:other": 4}, (
        "dry-run must leave every scope's candidate counts unchanged"
    )

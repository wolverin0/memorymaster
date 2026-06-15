"""Hermetic retrieval-regression gate (gbrain v0.40.1-inspired).

Ingests a fixed corpus, runs hybrid retrieval over a curated set of queries
with known-correct answers, and fails if top-1 accuracy or recall@5 drops
below the thresholds in the fixture. Fully offline + deterministic: the
queries are lexically distinctive so the gate holds whether or not the local
embedding model is present in the test environment.

This catches silent ranking regressions on every retrieval change — e.g. a
weight tweak or a new boost that quietly demotes the right answer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.core.config import reset_config
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

_FIXTURE = Path(__file__).parent / "fixtures" / "qrels_search.json"


@pytest.fixture
def qrels():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def svc(tmp_path, qrels):
    reset_config()
    s = MemoryService(tmp_path / "qrels.db", workspace_root=tmp_path)
    s.init_db()
    for i, text in enumerate(qrels["corpus"]):
        s.ingest(
            text=text,
            citations=[CitationInput(source="qrels", locator=f"c{i}")],
            source_agent="qrels-fixture",
        )
    yield s
    reset_config()


def _top_texts(svc, query, limit=5):
    rows = svc.query_rows(
        query_text=query,
        limit=limit,
        retrieval_mode="hybrid",
        include_candidates=True,
    )
    return [r["claim"].text for r in rows], rows


def test_retrieval_meets_qrels_thresholds(svc, qrels):
    top1_hits = 0
    recall5_hits = 0
    misses = []
    for item in qrels["qrels"]:
        texts, _ = _top_texts(svc, item["query"], limit=5)
        expect = item["expect"]
        if texts and expect in texts[0]:
            top1_hits += 1
        if any(expect in t for t in texts):
            recall5_hits += 1
        else:
            misses.append((item["query"], expect))

    n = len(qrels["qrels"])
    top1 = top1_hits / n
    recall5 = recall5_hits / n
    th = qrels["thresholds"]
    assert top1 >= th["top1_min"], (
        f"top-1 accuracy {top1:.2f} < {th['top1_min']} — retrieval regression. misses={misses}"
    )
    assert recall5 >= th["recall_at_5_min"], (
        f"recall@5 {recall5:.2f} < {th['recall_at_5_min']} — retrieval regression. misses={misses}"
    )


def test_hybrid_rows_carry_score_breakdown(svc, qrels):
    """Locks the #4 attribution wiring: hybrid query_rows surfaces a breakdown."""
    _, rows = _top_texts(svc, qrels["qrels"][0]["query"], limit=5)
    assert rows, "expected at least one result"
    assert rows[0].get("breakdown") is not None
    assert "relevance" in rows[0]["breakdown"]
    assert "boosts_applied" in rows[0]["breakdown"]

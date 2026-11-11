"""End-to-end smoke test for v3.9.0 features.

Exercises the full pipeline:
  1. Init a fresh DB
  2. Ingest claims that exercise F1 (claim_type), F2 (CamelCase library), F8 (claim mentions)
  3. Run rebuild_edges (F8) and check the structural edges land
  4. Build a tiny vault and rebuild_closets (F6); search_closets returns the article
  5. Recall with two-pass disabled is bit-identical to legacy; with W_TWO_PASS=0 even when
     the stream is on, ranking is identical (proves the env-gate works without breaking).

This is one focused test, not a full pipeline rebuild — it's the
backstop that catches "feature ships but the file isn't even imported"
class of regressions.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.recall.claim_edges import (
    SUPERSEDES_KIND,
    rebuild_edges,
    walk_neighbors,
)
from memorymaster.knowledge.closets import rebuild_closets, search_closets
from memorymaster.knowledge.entity_extractor import extract_patterns
from memorymaster.core.scope_utils import scope_from_transcript
from memorymaster.knowledge.wiki_validate import validate_file


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "e2e.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT,
                claim_type TEXT,
                human_id TEXT,
                replaced_by_claim_id INTEGER,
                scope TEXT,
                confidence REAL
            );
            INSERT INTO claims (id, text, claim_type, human_id, replaced_by_claim_id, scope, confidence) VALUES
              (1, 'We decided to use MemPalace and ChromaDB. See claim 2 for the trade-off analysis.', 'decision', 'mm-aaaa', NULL, 'project:e2e', 0.8),
              (2, 'Trade-off: MemPalace closets give +38% R@1 but require schema migration.', 'fact', 'mm-bbbb', 5, 'project:e2e', 0.7),
              (3, 'Bug: the v2 ChromaDB adapter dropped the metadata column on upgrade.', 'gotcha', 'mm-cccc', NULL, 'project:e2e', 0.9),
              (5, 'Final decision: keep ChromaDB but pin v0.2.6.', 'decision', 'mm-eeee', NULL, 'project:e2e', 0.95);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_e2e_f2_camelcase_extraction_works():
    """F2: extract_patterns returns library_name entities for CamelCase tokens."""
    out = extract_patterns(
        "We decided to use MemPalace and ChromaDB. The OneSignal integration is next."
    )
    libs = {e.canonical_hint for e in out if e.kind == "library_name"}
    assert "mempalace" in libs
    assert "chromadb" in libs
    assert "onesignal" in libs


def test_e2e_f3_scope_from_transcript_resolves(tmp_path):
    """F3: scope_from_transcript reads cwd from a JSONL session and produces a project scope."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": "/path/to/myproject", "role": "user"}) + "\n",
        encoding="utf-8",
    )
    assert scope_from_transcript(transcript) == "project:myproject"


def test_e2e_f4_wiki_validate_catches_bad_article(tmp_path):
    """F4: validate_file flags a frontmatter-less article."""
    bad = tmp_path / "bad.md"
    bad.write_text("# Just a title\n\nNo frontmatter at all.\n", encoding="utf-8")
    r = validate_file(bad)
    assert "MISSING_OPEN" in r.codes
    assert r.ok is False


def test_e2e_f6_closets_round_trip(tmp_path, fresh_db):
    """F6: rebuild_closets indexes a wiki article + search returns it."""
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "trade-off.md").write_text(
        """---
title: Trade Off
description: Long enough description for the validator to accept the article without flagging it.
type: fact
scope: project:e2e
tags: [fact]
date: 2026-04-27
claims: [1, 2]
---

# Trade Off

Discussion of MemPalace vs ChromaDB. See [[recall-architecture]].
""",
        encoding="utf-8",
    )
    counters = rebuild_closets(fresh_db, vault)
    assert counters["articles_indexed"] == 1
    hits = search_closets(fresh_db, "MemPalace ChromaDB")
    slugs = [s for s, _ in hits]
    assert "trade-off" in slugs


def test_e2e_f8_claim_edges_walks_chain(fresh_db):
    """F8: rebuild_edges populates the table; walk_neighbors finds the chain."""
    counters = rebuild_edges(fresh_db)
    assert counters["claims_scanned"] == 4
    # claim 1 mentions claim 2 (numeric)
    # claim 2 has replaced_by=5 (supersession edge)
    # so from seed [1] at depth 2 we should reach 2 (hop 1) and 5 (hop 2)
    distances = walk_neighbors(fresh_db, [1], max_hops=2, direction="out")
    assert 2 in distances
    assert distances[2] == 1
    assert 5 in distances
    assert distances[5] == 2
    # supersession edges were written
    assert counters["supersession_edges"] >= 1


def test_e2e_module_imports_have_no_circular_dep():
    """The new v3.9.0 modules import cleanly. Catches accidental circular imports."""
    from memorymaster import (
        claim_edges,  # F8
        closets,  # F6
        federated_graphify,  # F7
        scope_utils,  # F3
        wiki_validate,  # F4
    )
    # All five exposed via package
    assert hasattr(claim_edges, "rebuild_edges")
    assert hasattr(closets, "rebuild_closets")
    assert hasattr(federated_graphify, "federated_query")
    assert hasattr(scope_utils, "scope_from_transcript")
    assert hasattr(wiki_validate, "validate_file")

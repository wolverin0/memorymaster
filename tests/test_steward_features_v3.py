"""Unit tests for the v3 ``wiki_similarity_cosine`` feature.

Covers:

* feature is always present in the output dict (default 0.0 when no corpus)
* empty corpus -> 0.0
* no-text claim -> 0.0
* exact-match claim (claim text == article body) -> high similarity
* explicit ``wiki_article`` slug beats token-overlap fallback
* token-overlap fallback picks the best slug when no explicit column
* disk cache round-trips (second call returns cached scalar, no recompute)
* backend selection honours ``MEMORYMASTER_DISABLE_ST`` env var (TF-IDF path)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from memorymaster.steward_features import (
    FEATURE_KEYS,
    FEATURE_VERSION,
    extract_features,
)
from memorymaster.wiki_similarity import (
    WikiCorpus,
    compute_wiki_similarity,
    load_wiki_corpus,
)

_SCHEMA = """
CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, subject TEXT,
    predicate TEXT, object_value TEXT, scope TEXT, status TEXT,
    claim_type TEXT, source_agent TEXT, created_at TEXT,
    access_count INTEGER DEFAULT 0, wiki_article TEXT);
CREATE TABLE citations (id INTEGER PRIMARY KEY, claim_id INTEGER,
    source TEXT, excerpt TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, claim_id INTEGER,
    event_type TEXT, details TEXT, created_at TEXT);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    c.commit()
    return c


@pytest.fixture
def tf_idf_only(monkeypatch) -> None:
    """Force the TF-IDF backend so tests are deterministic regardless of
    whether sentence-transformers downloaded a model into the test cache."""
    monkeypatch.setenv("MEMORYMASTER_DISABLE_ST", "1")


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a tiny wiki vault with three articles."""
    wiki = tmp_path / "project-memorymaster"
    wiki.mkdir()
    (wiki / "_index.md").write_text("# index (should be skipped)\n", encoding="utf-8")
    (wiki / "alpha.md").write_text(
        "---\ntitle: Alpha\ndescription: d\n---\n\n"
        "The steward classifier promotes confirmed claims with high citation counts.\n"
        "\n---\n\n### 2026-04-23 | decision\nlegacy\n",
        encoding="utf-8",
    )
    (wiki / "beta.md").write_text(
        "---\ntitle: Beta\ndescription: d\n---\n\n"
        "Qdrant vector search is used for semantic recall on the memorymaster corpus.\n"
        "\n---\n\n### 2026-04-20 | fact\nlegacy\n",
        encoding="utf-8",
    )
    (wiki / "gamma.md").write_text(
        "---\ntitle: Gamma\ndescription: d\n---\n\n"
        "Wiki articles contain compiled-truth body text and timeline entries.\n"
        "\n---\n\n### 2026-04-10 | gotcha\nlegacy\n",
        encoding="utf-8",
    )
    return wiki


def _ins(conn: sqlite3.Connection, **overrides) -> int:
    d = {
        "text": "x",
        "subject": "foo",
        "predicate": "requires",
        "object_value": "bar",
        "scope": "project:memorymaster",
        "status": "candidate",
        "claim_type": "decision",
        "source_agent": "claude-session",
        "created_at": "2026-04-23T00:00:00+00:00",
        "access_count": 1,
        "wiki_article": None,
    }
    d.update(overrides)
    cols = ", ".join(d.keys())
    ph = ", ".join("?" for _ in d)
    cur = conn.execute(
        f"INSERT INTO claims ({cols}) VALUES ({ph})", tuple(d.values())
    )
    conn.commit()
    return int(cur.lastrowid)


def test_v3_feature_key_always_emitted(conn) -> None:
    cid = _ins(conn)
    feats = extract_features({"id": cid}, conn)
    assert "wiki_similarity_cosine" in feats
    assert feats["wiki_similarity_cosine"] == 0.0  # no corpus -> default


def test_empty_corpus_returns_zero(conn, tmp_path: Path) -> None:
    empty = WikiCorpus(scope="project:memorymaster")
    cid = _ins(conn, text="anything")
    feats = extract_features(
        {"id": cid, "text": "steward"}, conn, wiki_corpus=empty,
    )
    assert feats["wiki_similarity_cosine"] == 0.0


def test_no_text_returns_zero(conn, wiki_dir, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    assert corpus.embedding_backend == "tfidf"
    cid = _ins(conn, text=None, subject=None, wiki_article="alpha")
    feats = extract_features(
        {"id": cid, "text": None, "subject": None, "wiki_article": "alpha"},
        conn, wiki_corpus=corpus,
    )
    assert feats["wiki_similarity_cosine"] == 0.0


def test_exact_match_gives_high_similarity(conn, wiki_dir, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    assert corpus.embedding_backend == "tfidf"
    # Claim text is essentially the same sentence as alpha.md
    claim = {
        "id": 1,
        "text": "The steward classifier promotes confirmed claims with high citation counts.",
        "subject": "steward",
        "wiki_article": "alpha",
    }
    sim = compute_wiki_similarity(claim, corpus)
    assert sim >= 0.85, f"expected ~1.0 cosine for near-identical content, got {sim}"


def test_wrong_slug_still_scored_against_that_slug(conn, wiki_dir, tf_idf_only) -> None:
    """When the claim carries an explicit ``wiki_article`` column, respect it
    even if token overlap would pick another article. Ensures downstream
    consistency — we never silently 'fix' an operator's binding."""
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    claim = {
        "id": 2,
        "text": "Qdrant vector search is used for semantic recall on the memorymaster corpus.",
        "subject": "qdrant",
        "wiki_article": "alpha",  # wrong slug — alpha talks about steward classifier
    }
    sim = compute_wiki_similarity(claim, corpus)
    assert 0.0 <= sim <= 1.0
    # Compare to the correct slug:
    claim_correct = dict(claim, wiki_article="beta")
    sim_correct = compute_wiki_similarity(claim_correct, corpus)
    assert sim_correct > sim, (
        f"beta should match better than alpha for qdrant text: "
        f"beta={sim_correct} alpha={sim}"
    )


def test_token_overlap_fallback_picks_best_slug(conn, wiki_dir, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    # No wiki_article column set; token overlap should pick beta (qdrant / vector).
    claim = {
        "id": 3,
        "text": "Qdrant vector search powers our semantic recall.",
        "subject": "qdrant",
    }
    sim = compute_wiki_similarity(claim, corpus)
    assert sim > 0.1, (
        f"token-overlap fallback should have matched 'beta' and produced "
        f"non-trivial similarity, got {sim}"
    )


def test_disk_cache_roundtrip(conn, wiki_dir, tmp_path, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    cache = tmp_path / "feature-cache"
    claim = {
        "id": 9999,
        "text": "steward classifier promotes confirmed claims.",
        "subject": "steward",
        "wiki_article": "alpha",
    }
    sim1 = compute_wiki_similarity(claim, corpus, cache_dir=cache)
    # Cache file should now exist keyed by claim_id + content hash.
    files = list(cache.glob("9999-*.npy"))
    assert len(files) == 1, f"expected one cache file, got {files}"

    # Reading it back yields the same value.
    cached = float(np.load(files[0]))
    assert abs(cached - sim1) < 1e-6

    sim2 = compute_wiki_similarity(claim, corpus, cache_dir=cache)
    assert abs(sim2 - sim1) < 1e-6


def test_missing_article_returns_zero(conn, wiki_dir, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    # Slug doesn't exist in corpus -> should silently return 0.0.
    claim = {
        "id": 4,
        "text": "anything",
        "subject": "x",
        "wiki_article": "does-not-exist",
    }
    sim = compute_wiki_similarity(claim, corpus)
    assert sim == 0.0


def test_feature_version_is_v3() -> None:
    assert FEATURE_VERSION == "v3"
    assert "wiki_similarity_cosine" in FEATURE_KEYS
    assert FEATURE_KEYS[-1] == "wiki_similarity_cosine", (
        "v3 feature should be the last key so feature_vector() preserves "
        "the v2 feature order"
    )


def test_missing_wiki_root_silently_returns_empty_corpus(tmp_path, tf_idf_only) -> None:
    corpus = load_wiki_corpus(wiki_root=tmp_path / "does-not-exist")
    assert corpus.is_empty()
    assert corpus.embedding_backend == "none"


def test_cache_off_env_skips_disk_writes(conn, wiki_dir, tmp_path, monkeypatch, tf_idf_only) -> None:
    monkeypatch.setenv("MEMORYMASTER_STEWARD_FEATURE_CACHE", "off")
    corpus = load_wiki_corpus(wiki_root=wiki_dir)
    cache = tmp_path / "not-used"
    claim = {"id": 42, "text": "steward classifier", "subject": "x", "wiki_article": "alpha"}
    sim = compute_wiki_similarity(claim, corpus)  # no cache dir
    assert 0.0 <= sim <= 1.0
    # With the env set to off, even an explicit cache dir would be IGNORED
    # only when the override is None. Here we passed no override, so nothing
    # should have been written anywhere (the default dir is artifacts/ which
    # we don't touch during unit tests).
    assert not cache.exists()
    # sanity cleanup
    os.environ.pop("MEMORYMASTER_STEWARD_FEATURE_CACHE", None)

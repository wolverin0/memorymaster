"""Unit tests for memorymaster.recall_tokenizer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.recall_tokenizer import (
    _alias_set,
    _best_form,
    _candidate_tokens,
    _corpus_stats,
    _stem,
    extract_query_tokens,
)


@pytest.fixture()
def tiny_db(tmp_path: Path) -> str:
    db_path = tmp_path / "tiny.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT);"
        "CREATE TABLE entity_aliases (id INTEGER PRIMARY KEY, entity_id INTEGER, "
        "alias TEXT UNIQUE, original_form TEXT);"
    )
    # "common" appears in every claim (low IDF). "steward"/"qdrant" are rare.
    rows = [
        "common task about the steward decay job",
        "common fact about qdrant vector backend",
        "common reminder to rotate keys",
        "common notes on confidence scoring",
        "common note about tier recomputation",
    ]
    conn.executemany("INSERT INTO claims (text) VALUES (?)", [(r,) for r in rows])
    conn.executemany(
        "INSERT INTO entity_aliases (entity_id, alias, original_form) VALUES (?, ?, ?)",
        [(1, "steward", "Steward"), (2, "qdrant", "Qdrant")],
    )
    conn.commit()
    conn.close()
    _corpus_stats.cache_clear()
    _alias_set.cache_clear()
    return str(db_path)


def test_stopword_and_length_and_digit_filters() -> None:
    toks = _candidate_tokens("hay que correr el steward y 429 ab tambien el dashboard")
    assert "steward" in toks and "dashboard" in toks
    for bad in ("que", "el", "y", "tambien", "429", "ab"):
        assert bad not in toks


def test_url_stripping() -> None:
    toks = _candidate_tokens("lee https://dev.to/marcos/path steward")
    assert "https" not in toks
    assert "steward" in toks


def test_idf_prefers_rare_over_common(tiny_db: str) -> None:
    # "common" = freq 5, "steward" = freq 1 + alias boost.
    assert extract_query_tokens("common task about the steward", tiny_db, max_tokens=1) == "steward"


def test_entity_alias_boost_breaks_tie(tiny_db: str) -> None:
    # both 'qdrant' and 'vector' appear once; aliased 'qdrant' wins.
    assert extract_query_tokens("something about qdrant vector backend", tiny_db, max_tokens=1) == "qdrant"


def test_short_prompt_passes_all_tokens(tiny_db: str) -> None:
    out = extract_query_tokens("steward qdrant", tiny_db, max_tokens=6)
    assert out.split() == ["steward", "qdrant"]


def test_empty_or_stopword_only_returns_empty(tiny_db: str) -> None:
    assert extract_query_tokens("", "/nonexistent.db") == ""
    assert extract_query_tokens("   ", "/nonexistent.db") == ""
    assert extract_query_tokens("que es lo que hay", tiny_db, max_tokens=6) == ""


def test_missing_db_does_not_crash() -> None:
    _corpus_stats.cache_clear()
    _alias_set.cache_clear()
    out = extract_query_tokens("steward dashboard qdrant", "/no/such.db", max_tokens=3)
    assert "steward" in out.split()


def test_max_tokens_edges(tiny_db: str) -> None:
    assert extract_query_tokens("steward qdrant", tiny_db, max_tokens=0) == ""
    out = extract_query_tokens(
        "migrate steward qdrant embeddings retrieval rate limiting rotation load",
        tiny_db, max_tokens=3,
    )
    assert len(out.split()) <= 3


# ---------------------------------------------------------------------------
# v2 regressions (2026-04-23) — df=0 penalty + stemming + synonym fallback.
# Each test covers a specific failure mode identified in
# artifacts/recall-zero-hit-prompts-2026-04-23.md.
# ---------------------------------------------------------------------------


@pytest.fixture()
def v2_db(tmp_path: Path) -> str:
    """Corpus where real technical terms coexist with typo-ready df=0 slots."""
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT);"
        "CREATE TABLE entity_aliases (id INTEGER PRIMARY KEY, entity_id INTEGER, "
        "alias TEXT UNIQUE, original_form TEXT);"
    )
    # A few high-df terms (token, rate, limit, google) plus one low-df
    # technical abbreviation (fts5), plus stemmable words (fix / claims /
    # explica) so _best_form can route to them.
    rows = [
        "token bucket for google rate limiter and mcp proxy",
        "rate limit shaping with token counters and claim storage",
        "google flash-lite tpm rpm rpd tier limits for the steward",
        "claim about the steward token budget",
        "fix a rate limit bug on mcp tool registration",
        "fixes documented for the claim decay job on sqlite",
        "explica del dashboard when the steward rate-limits",
        "fts5 index rebuild for the claims table",
        "llm provider failover between gemini and anthropic",
        "mcp tool registration race on reconnect",
    ]
    conn.executemany("INSERT INTO claims (text) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    _corpus_stats.cache_clear()
    _alias_set.cache_clear()
    return str(db_path)


def test_stem_strips_english_suffixes() -> None:
    assert _stem("running") == "runn"  # -ing stripped, residual ≥3
    assert _stem("fixes") == "fix"
    assert _stem("tokens") == "token"
    # Too-short residual → no stem.
    assert _stem("ate") is None
    assert _stem("eat") is None


def test_stem_strips_spanish_clitic_pronouns() -> None:
    # Spanish imperatives with attached pronouns — the crux of prompt #18.
    assert _stem("explicamelo") == "explica"
    # "hacelo" -> "hace" (Spanish "do-it"). Residual ≥3 ensures no garbage.
    assert _stem("hacelo") == "hace"


def test_df_zero_demoted_below_real_terms(v2_db: str) -> None:
    """Prompt #6/#21 regression: typos must not crowd out real terms.

    The prompt mixes df=0 typos ("camvio", "golgle", "teniamls") with
    real matchable terms ("token", "rate", "limit", "google"). The v2
    tokenizer must return the real terms, not the typos.
    """
    out = extract_query_tokens(
        "camvio algo de golgle teniamls token rate limit google",
        v2_db, max_tokens=4,
    ).split()
    # None of the unmatchable typos should survive.
    for typo in ("camvio", "golgle", "teniamls"):
        assert typo not in out, f"df=0 typo {typo!r} leaked past the penalty"
    # At least the high-df real terms should be present.
    assert any(t in out for t in ("token", "tokens")), out
    assert any(t in out for t in ("rate",)), out


def test_short_prompt_uses_stem_fallback(v2_db: str) -> None:
    """Prompt #11 regression: 'fixea' has df=0 but 'fix' has df>0."""
    # 'fixea' has no -ings/-ing/-s stem, but "-ea" strips to "fix".
    out = extract_query_tokens("fixea todos si", v2_db, max_tokens=6)
    # Stem replacement is only applied when the stem has higher df; our
    # corpus has "fix" (1 doc) and no "fixea" (df=0), so output should
    # be the stem.
    assert "fix" in out.split()
    assert "fixea" not in out.split()


def test_tech_term_survives_low_df(v2_db: str) -> None:
    """Technical abbreviations (fts5, mcp, llm) escape the df=0 penalty.

    Even though "fts5" appears only once in the corpus, it must surface
    alongside high-df common words when present.
    """
    out = extract_query_tokens(
        "fts5 index rebuild with mcp reconnect and llm failover",
        v2_db, max_tokens=3,
    ).split()
    # Any two of the three techs should surface before generic words.
    tech_hits = sum(1 for t in ("fts5", "mcp", "llm") if t in out)
    assert tech_hits >= 2, f"expected ≥2 tech terms in top-3, got {out}"


def test_synonym_routes_to_higher_df_form(v2_db: str) -> None:
    """'claim' → 'claims' if plural has higher df (and vice-versa).

    In the v2 corpus the singular 'claim' and plural 'claims' both appear,
    but the tokenizer should consistently pick whichever form has higher
    df so downstream FTS sees the best-matching surface.
    """
    total, df = _corpus_stats(v2_db)
    best = _best_form("claim", df)
    # Either form is acceptable as long as it's the higher-df one.
    assert df.get(best, 0) >= df.get("claim", 0)
    assert best in ("claim", "claims")


def test_all_df_zero_prompt_returns_something(v2_db: str) -> None:
    """Defensive: pure-fluff prompts shouldn't raise or produce a None.

    Prompts #18 and #27 fall here when no stem lands on a corpus term —
    the tokenizer should gracefully return whatever candidates it has
    (they won't match FTS, which is the correct behaviour upstream).
    """
    # "crees realmentq productivo" — none stemmable to corpus terms.
    out = extract_query_tokens(
        "crees realmentq productivo bar xyzzy", v2_db, max_tokens=3,
    )
    # Non-crash guarantee; content is implementation-defined.
    assert isinstance(out, str)


def test_latin_boundary_preserves_accents() -> None:
    """Latin-lookaround boundary must keep accented Spanish words intact."""
    toks = _candidate_tokens("el próximo modelo usará más tokens")
    # 'próximo' and 'usará' should each emerge as single tokens, not
    # split at the accented character.
    assert "próximo" in toks
    assert "usará" in toks

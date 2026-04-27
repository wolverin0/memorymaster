"""Closets — search-side wiki-pointer boost (v3.9.0 F6, ported from MemPalace v3.3.0).

Concept
-------
A "closet" is a compact, BM25-friendly index entry that points to a wiki
article. The article body is for humans; the closet is for the searcher.
When a query matches a closet, the searcher knows which wiki article (and
hence which underlying claims) to expand. Closets are a BOOST signal, never
a gate — direct claim search runs first as the floor.

MemPalace measured **R@1 0.42 → 0.58 (+38%)** with regex-derived closets on
their 100-question harness. That's the bet we're porting.

Design
------
* Table ``closets`` (article_slug, terms, claim_ids JSON, updated_at).
* ``terms`` is a space-joined list of regex-derived tokens (entity surfaces,
  CamelCase libraries, code-fenced identifiers) extracted from the article
  body. Indexed via FTS5 ``closets_fts`` virtual table for BM25 lookups.
* ``rebuild_closets(db_path, vault_root)`` walks the wiki, parses each
  article's frontmatter (for claim_ids) and body (for term extraction),
  then upserts one closet row per article.
* ``search_closets(db_path, query, limit)`` returns ``[(slug, claim_ids)]``
  ranked by FTS5 BM25 score.

Integration with recall (NOT done in this module — opt-in env flag in
context_hook.py: ``MEMORYMASTER_RECALL_CLOSETS=1``).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ensure_closets_schema",
    "extract_closet_terms",
    "rebuild_closets",
    "search_closets",
]


# Term extractors — lightweight regex pass over an article body. We
# deliberately avoid heavy dependencies; this runs inside wiki-absorb.
_CAMEL_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:[A-Z][a-z]*)+\b")
_FENCED_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.\-]{2,})`")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_BARE_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_\-]{4,}\b")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS closets (
    article_slug TEXT PRIMARY KEY,
    terms TEXT NOT NULL DEFAULT '',
    claim_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS closets_fts USING fts5(
    article_slug UNINDEXED,
    terms,
    tokenize='unicode61'
);
"""


def ensure_closets_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema migration."""
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def extract_closet_terms(body: str) -> str:
    """Pull BM25-friendly tokens from a wiki article body.

    Returns a space-joined string suitable for FTS5 indexing. Tokens
    extracted (in order, dedup at the end):
      * CamelCase library names (e.g. MemPalace, ChromaDB).
      * Backtick-fenced inline code (e.g. claim_type, recall_hook).
      * Wikilinks (e.g. ``[[other-article]]`` → ``other-article``).
      * Bare words >= 5 chars (caps the noise floor while keeping enough
        signal for BM25 to rank).
    """
    if not body:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    # (regex, group_index) — group 0 for whole match, group 1 for capture
    extractors = [
        (_CAMEL_RE, 0),
        (_FENCED_RE, 1),
        (_WIKILINK_RE, 1),
        (_BARE_WORD_RE, 0),
    ]
    for rgx, group_idx in extractors:
        for m in rgx.finditer(body):
            term = m.group(group_idx).strip()
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return " ".join(out)


def _parse_article(path: Path) -> tuple[str, list[int], str]:
    """Return (slug, claim_ids, body). Defensive — empty body if read fails."""
    slug = path.stem
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return slug, [], ""
    if not text.startswith("---"):
        return slug, [], text
    rest = text[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        return slug, [], text
    fm_text = rest[:end]
    body = rest[end + 4:].lstrip("\n")
    claim_ids: list[int] = []
    for line in fm_text.splitlines():
        if line.startswith("claims:"):
            raw = line.partition(":")[2].strip()
            # Tolerate "[1, 2]" or "1, 2"
            for tok in re.findall(r"\d+", raw):
                try:
                    claim_ids.append(int(tok))
                except ValueError:
                    continue
            break
    return slug, claim_ids, body


def rebuild_closets(
    db_path: str | Path, vault_wiki_root: str | Path
) -> dict[str, int]:
    """Walk vault_wiki_root/**/*.md and upsert one closet per article.

    Returns counters: ``{"articles_indexed": N, "skipped": M}``.
    """
    root = Path(vault_wiki_root)
    counters = {"articles_indexed": 0, "skipped": 0}
    if not root.is_dir():
        return counters
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_closets_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        exempt = {"_index.md", "MEMORY.md", "README.md", "log.md"}
        for md in root.rglob("*.md"):
            if md.name in exempt:
                counters["skipped"] += 1
                continue
            slug, claim_ids, body = _parse_article(md)
            terms = extract_closet_terms(body)
            if not terms:
                counters["skipped"] += 1
                continue
            conn.execute(
                """
                INSERT INTO closets (article_slug, terms, claim_ids_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(article_slug) DO UPDATE SET
                    terms = excluded.terms,
                    claim_ids_json = excluded.claim_ids_json,
                    updated_at = excluded.updated_at
                """,
                (slug, terms, json.dumps(claim_ids), now),
            )
            # FTS5 mirror — delete + insert is the simplest upsert pattern.
            conn.execute("DELETE FROM closets_fts WHERE article_slug = ?", (slug,))
            conn.execute(
                "INSERT INTO closets_fts (article_slug, terms) VALUES (?, ?)",
                (slug, terms),
            )
            counters["articles_indexed"] += 1
        conn.commit()
    finally:
        conn.close()
    return counters


def search_closets(
    db_path: str | Path, query: str, *, limit: int = 5, with_scores: bool = False
) -> list[tuple[str, list[int]]] | list[tuple[str, list[int], float]]:
    """Return ``[(slug, claim_ids), ...]`` ranked by FTS5 BM25.

    When ``with_scores=True`` (v3.11 P1), returns ``[(slug, claim_ids, score)]``
    where ``score`` is the FTS5 BM25 score normalised to [0, 1] across this
    result set. The recall hook uses this to weight closet hits proportional
    to how well the query matched, instead of the v3.10 constant 1.0 that
    flooded the top-5 with article-membership noise.

    BM25 from SQLite's FTS5 returns NEGATIVE values where 0 = no match and
    more-negative = better match (`ORDER BY bm25(...) ASC` puts best first).
    We normalise: ``score = best_bm25 / row_bm25`` so the best match scores
    1.0 and weaker matches scale below.

    Defensive: if the table doesn't exist or the query is malformed, returns
    an empty list rather than raising.
    """
    if not query or not query.strip():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        tokens = [t for t in re.findall(r"[A-Za-z0-9_]{3,}", query) if t]
        if not tokens:
            return []
        fts_query = " OR ".join(tokens)
        try:
            rows = conn.execute(
                """
                SELECT c.article_slug, c.claim_ids_json, bm25(closets_fts) AS bm25_score
                FROM closets_fts ft
                JOIN closets c ON c.article_slug = ft.article_slug
                WHERE closets_fts MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []
        # FTS5 BM25 returns negative numbers; lower (more negative) = better.
        # The first row is the best. Normalise so best=1.0, others scale below.
        best = abs(rows[0][2]) if rows[0][2] else 1.0
        if best == 0.0:
            best = 1.0
        out: list[tuple] = []
        for slug, ids_json, bm25_score in rows:
            try:
                ids = json.loads(ids_json or "[]")
                if not isinstance(ids, list):
                    ids = []
            except (json.JSONDecodeError, ValueError):
                ids = []
            normalised = abs(bm25_score) / best if bm25_score else 0.0
            normalised = max(0.0, min(1.0, normalised))
            cleaned_ids = [int(i) for i in ids if isinstance(i, int)]
            if with_scores:
                out.append((str(slug), cleaned_ids, normalised))
            else:
                out.append((str(slug), cleaned_ids))
        return out
    finally:
        conn.close()

"""Wikilink suggestions from the entity graph.

This module is intentionally read-only: it uses the existing entity graph to
find claims related to entities mentioned in a paragraph, then maps those
claims to existing wiki article slugs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from memorymaster._storage_shared import connect_ro
from memorymaster.knowledge.entity_graph import EntityGraph

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
_DEFAULT_WIKI_ROOT = Path("obsidian-vault/wiki")


@dataclass(frozen=True)
class EntityTerm:
    entity_id: str
    name: str
    terms: tuple[str, ...]


@dataclass
class _Suggestion:
    slug: str
    best_depth: int
    matched_entities: set[str] = field(default_factory=set)
    claim_count: int = 0

    def to_dict(self) -> dict[str, object]:
        proximity = 1.0 / (1.0 + float(self.best_depth))
        entity_boost = max(0, len(self.matched_entities) - 1) * 0.05
        claim_boost = max(0, self.claim_count - 1) * 0.02
        score = min(1.0, proximity + entity_boost + claim_boost)
        return {
            "slug": self.slug,
            "score": round(score, 4),
            "matched_entities": sorted(self.matched_entities),
        }


def suggest_wikilinks(
    db_path: str | Path,
    text: str,
    *,
    wiki_root: str | Path | None = None,
    limit: int = 10,
    hops: int = 2,
) -> list[dict[str, object]]:
    """Return ranked wikilink slug suggestions for ``text``.

    Ranking is based on shortest entity-graph distance from entities matched
    in the input paragraph to claims bound to wiki articles.
    """
    clean_text = (text or "").strip()
    if not clean_text or limit <= 0 or hops < 0:
        return []

    db = Path(db_path)
    article_slugs = load_wiki_article_slugs(wiki_root or _DEFAULT_WIKI_ROOT)
    if not article_slugs:
        return []

    terms = _load_entity_terms(db)
    matched = _match_entity_terms(clean_text, terms)
    if not matched:
        return []

    seed_names = [term.name for term in matched]
    graph = EntityGraph(str(db))
    related_claim_ids = set(graph.find_related_claims(seed_names, hops=hops, limit=max(limit * 50, 100)))
    if not related_claim_ids:
        return []

    suggestions = _rank_claim_slugs(
        db,
        matched,
        related_claim_ids=related_claim_ids,
        article_slugs=article_slugs,
        hops=hops,
    )
    ranked = sorted(
        suggestions.values(),
        key=lambda item: (-item.to_dict()["score"], item.best_depth, item.slug),
    )
    return [item.to_dict() for item in ranked[:limit]]


def load_wiki_article_slugs(wiki_root: str | Path) -> set[str]:
    """Load existing wiki article slugs from markdown files under ``wiki_root``."""
    root = Path(wiki_root)
    if not root.exists():
        return set()
    paths = root.rglob("*.md") if root.is_dir() else [root]
    slugs: set[str] = set()
    for path in paths:
        if path.name.startswith("_") or path.suffix.lower() != ".md":
            continue
        slugs.add(path.stem)
    return slugs


def _load_entity_terms(db_path: Path) -> list[EntityTerm]:
    try:
        # Read-only; a missing DB keeps the empty-result contract.
        conn = connect_ro(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute("SELECT id, name, aliases FROM entities").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    terms: list[EntityTerm] = []
    for row in rows:
        aliases = _parse_aliases(row["aliases"])
        values = tuple(dict.fromkeys([row["name"], *aliases]))
        terms.append(EntityTerm(entity_id=str(row["id"]), name=str(row["name"]), terms=values))
    return terms


def _parse_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _match_entity_terms(text: str, terms: list[EntityTerm]) -> list[EntityTerm]:
    text_tokens = {token.lower() for token in _WORD_RE.findall(text)}
    if not text_tokens:
        return []
    matched: list[EntityTerm] = []
    for term in terms:
        if any(_term_matches(candidate, text_tokens) for candidate in term.terms):
            matched.append(term)
    return matched


def _term_matches(term: str, text_tokens: set[str]) -> bool:
    tokens = [token.lower() for token in _WORD_RE.findall(term)]
    return bool(tokens) and all(token in text_tokens for token in tokens)


def _rank_claim_slugs(
    db_path: Path,
    matched: list[EntityTerm],
    *,
    related_claim_ids: set[int],
    article_slugs: set[str],
    hops: int,
) -> dict[str, _Suggestion]:
    entity_depths = _reachable_entity_depths(db_path, matched, hops=hops)
    if not entity_depths:
        return {}

    conn = connect_ro(db_path)
    try:
        claim_links = _claim_links_for_entities(conn, set(entity_depths))
        claim_slugs = _claim_slugs(conn, set(claim_links), article_slugs)
    finally:
        conn.close()

    suggestions: dict[str, _Suggestion] = {}
    for claim_id, entity_ids in claim_links.items():
        if claim_id not in related_claim_ids or claim_id not in claim_slugs:
            continue
        slug = claim_slugs[claim_id]
        suggestion = suggestions.setdefault(slug, _Suggestion(slug=slug, best_depth=hops + 1))
        suggestion.claim_count += 1
        for entity_id in entity_ids:
            for seed_name, depth in entity_depths.get(entity_id, {}).items():
                suggestion.best_depth = min(suggestion.best_depth, depth)
                suggestion.matched_entities.add(seed_name)
    return suggestions


def _reachable_entity_depths(
    db_path: Path,
    seeds: list[EntityTerm],
    *,
    hops: int,
) -> dict[str, dict[str, int]]:
    adjacency = _load_adjacency(db_path)
    depths: dict[str, dict[str, int]] = {}
    for seed in seeds:
        frontier = [(seed.entity_id, 0)]
        seen = {seed.entity_id}
        while frontier:
            entity_id, depth = frontier.pop(0)
            current = depths.setdefault(entity_id, {})
            current[seed.name] = min(current.get(seed.name, depth), depth)
            if depth >= hops:
                continue
            for neighbor in adjacency.get(entity_id, set()):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                frontier.append((neighbor, depth + 1))
    return depths


def _load_adjacency(db_path: Path) -> dict[str, set[str]]:
    try:
        # Read-only; a missing DB keeps the empty-result contract.
        conn = connect_ro(db_path)
    except sqlite3.OperationalError:
        return {}
    try:
        rows = conn.execute("SELECT source_id, target_id FROM entity_edges").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()

    adjacency: dict[str, set[str]] = {}
    for row in rows:
        source = str(row["source_id"])
        target = str(row["target_id"])
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    return adjacency


def _claim_links_for_entities(conn: sqlite3.Connection, entity_ids: set[str]) -> dict[int, set[str]]:
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"""
        SELECT claim_id, entity_id
        FROM claim_entity_links
        WHERE entity_id IN ({placeholders})
        """,
        sorted(entity_ids),
    ).fetchall()
    links: dict[int, set[str]] = {}
    for row in rows:
        links.setdefault(int(row["claim_id"]), set()).add(str(row["entity_id"]))
    return links


def _claim_slugs(conn: sqlite3.Connection, claim_ids: set[int], article_slugs: set[str]) -> dict[int, str]:
    if not claim_ids:
        return {}
    placeholders = ",".join("?" for _ in claim_ids)
    rows = conn.execute(
        f"""
        SELECT id, wiki_article
        FROM claims
        WHERE id IN ({placeholders})
          AND wiki_article IS NOT NULL
          AND TRIM(wiki_article) != ''
          AND status != 'archived'
        """,
        sorted(claim_ids),
    ).fetchall()
    return {
        int(row["id"]): str(row["wiki_article"]).strip()
        for row in rows
        if str(row["wiki_article"]).strip() in article_slugs
    }

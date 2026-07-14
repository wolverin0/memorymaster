from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memorymaster.surfaces.cli import main
import pytest

from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraphNotReady
from memorymaster.knowledge.wiki_suggest import suggest_wikilinks


def _seed_graph(db_path: Path) -> None:
    MemoryService(db_path, workspace_root=db_path.parent).init_db()
    conn = sqlite3.connect(db_path)
    try:
        now = "2026-01-01T00:00:00Z"
        conn.executemany(
            "INSERT INTO entities "
            "(id, canonical_name, entity_type, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, 'global', ?, ?)",
            [
                (1, "claim", "concept", now, now),
                (2, "status", "concept", now, now),
                (3, "qdrant", "product", now, now),
            ],
        )
        conn.executemany(
            "INSERT INTO entity_aliases "
            "(entity_id, alias, variant_key, original_form, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(1, "claim", "claim", "claim", now), (1, "claims", "claims", "claims", now),
             (2, "status", "status", "status", now), (3, "qdrant", "qdrant", "qdrant", now)],
        )
        conn.executemany(
            "INSERT INTO entity_edges (source_id, target_id, relation, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 2, "related_to", 1, now),
                (2, 3, "related_to", 2, now),
            ],
        )
        conn.executemany(
            "INSERT INTO claims "
            "(id, text, scope, status, wiki_article, created_at, updated_at) "
            "VALUES (?, 'wiki graph claim', 'project:test', ?, ?, ?, ?)",
            [
                (1, "confirmed", "claims-lifecycle", now, now),
                (2, "confirmed", "status-governance", now, now),
                (3, "confirmed", "qdrant-sync", now, now),
                (4, "confirmed", "missing-article", now, now),
            ],
        )
        conn.executemany(
            "INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
            [
                (1, 1),
                (1, 2),
                (2, 2),
                (3, 3),
                (4, 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_wiki(root: Path) -> None:
    root.mkdir(parents=True)
    for slug in ("claims-lifecycle", "status-governance", "qdrant-sync"):
        (root / f"{slug}.md").write_text(f"# {slug}\n", encoding="utf-8")


def test_suggest_wikilinks_ranks_by_graph_proximity(tmp_path: Path) -> None:
    db_path = tmp_path / "memorymaster.db"
    wiki_root = tmp_path / "wiki"
    _seed_graph(db_path)
    _seed_wiki(wiki_root)

    suggestions = suggest_wikilinks(
        db_path,
        "The claims lifecycle should account for status changes.",
        wiki_root=wiki_root,
        limit=5,
        hops=2,
    )

    assert suggestions[0] == {
        "slug": "claims-lifecycle",
        "score": 1.0,
        "matched_entities": ["claim", "status"],
    }
    assert [item["slug"] for item in suggestions] == [
        "claims-lifecycle",
        "status-governance",
        "qdrant-sync",
    ]
    assert suggestions[2]["score"] < suggestions[0]["score"]


def test_wiki_suggest_links_cli_prints_json_list(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "memorymaster.db"
    wiki_root = tmp_path / "wiki"
    _seed_graph(db_path)
    _seed_wiki(wiki_root)

    rc = main([
        "--db",
        str(db_path),
        "wiki-suggest-links",
        "--text",
        "claim status",
        "--wiki-root",
        str(wiki_root),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["slug"] == "claims-lifecycle"


def test_suggest_wikilinks_reports_missing_graph(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    _seed_wiki(wiki_root)

    with pytest.raises(EntityGraphNotReady, match="init-db"):
        suggest_wikilinks(tmp_path / "empty.db", "claim status", wiki_root=wiki_root)
